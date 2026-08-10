"""Tests for API authentication middleware."""

import os
from unittest.mock import MagicMock, patch


from agent.auth import AuthMiddleware


def test_is_public_endpoint_health():
    """Health endpoint should be public."""
    assert AuthMiddleware.is_public_endpoint("/api/health") == True
    assert AuthMiddleware.is_public_endpoint("/health") == True


def test_is_public_endpoint_metrics():
    """Metrics endpoint should be public."""
    assert AuthMiddleware.is_public_endpoint("/api/metrics") == True


def test_is_public_endpoint_static():
    """Static files should be public."""
    assert AuthMiddleware.is_public_endpoint("/static/app.js") == True
    assert AuthMiddleware.is_public_endpoint("/static/style.css") == True


def test_is_private_endpoint_chat():
    """Chat endpoint should require auth when API_KEYS is configured."""
    with patch.dict(os.environ, {"API_KEYS": "some-key"}):
        assert AuthMiddleware.is_public_endpoint("/api/chat") == False


def test_all_public_when_no_api_keys():
    """When API_KEYS is empty, all endpoints are public (local dev mode)."""
    with patch.dict(os.environ, {"API_KEYS": ""}, clear=True):
        assert AuthMiddleware.is_public_endpoint("/api/chat") == True
        assert AuthMiddleware.is_public_endpoint("/api/internal") == True


def test_check_api_key_valid_header():
    """Valid API key in Authorization header should pass."""
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer valid-key"}
    
    with patch.dict(os.environ, {"API_KEYS": "valid-key"}):
        assert AuthMiddleware.check_api_key(handler) == True


def test_check_api_key_valid_x_api_key():
    """Valid API key in X-API-Key header should pass."""
    handler = MagicMock()
    handler.headers = {"X-API-Key": "valid-key"}
    
    with patch.dict(os.environ, {"API_KEYS": "valid-key"}):
        assert AuthMiddleware.check_api_key(handler) == True


def test_check_api_key_invalid():
    """Invalid API key should fail."""
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer invalid-key"}
    handler.path = "/api/chat"
    
    with patch.dict(os.environ, {"API_KEYS": "valid-key"}):
        assert AuthMiddleware.check_api_key(handler) == False


def test_check_api_key_no_headers():
    """No auth headers should fail."""
    handler = MagicMock()
    handler.headers = {}
    handler.path = "/api/chat"
    
    with patch.dict(os.environ, {"API_KEYS": "valid-key"}):
        assert AuthMiddleware.check_api_key(handler) == False


def test_multiple_api_keys():
    """Multiple API keys (comma-separated) should work."""
    handler = MagicMock()
    handler.headers = {"X-API-Key": "key2"}
    
    with patch.dict(os.environ, {"API_KEYS": "key1,key2,key3"}):
        assert AuthMiddleware.check_api_key(handler) == True


def test_validate_key_constant_time_comparison():
    """_validate_key should use hmac.compare_digest (constant-time) not plain ==."""
    import hmac as _hmac
    import agent.auth as auth_mod

    # Patch hmac.compare_digest to track calls
    originalCompare = _hmac.compare_digest
    calls = []

    def spyCompare(a, b):
        calls.append((a, b))
        return originalCompare(a, b)

    with patch.dict(os.environ, {"API_KEYS": "secret-key-abc"}):
        with patch.object(_hmac, "compare_digest", side_effect=spyCompare):
            # Valid key should call compare_digest
            calls.clear()
            assert AuthMiddleware._validate_key("secret-key-abc") == True
            assert len(calls) == 1
            assert calls[0] == ("secret-key-abc", "secret-key-abc")

            # Invalid key should also call compare_digest (no early return)
            calls.clear()
            assert AuthMiddleware._validate_key("wrong-key") == False
            assert len(calls) == 1  # Still compared, not short-circuited


def test_validate_key_empty_api_keys():
    """When API_KEYS is empty, no key should validate."""
    with patch.dict(os.environ, {"API_KEYS": ""}, clear=True):
        assert AuthMiddleware._validate_key("any-key") == False


def test_validate_key_whitespace_handling():
    """Whitespace around keys in API_KEYS should be stripped."""
    with patch.dict(os.environ, {"API_KEYS": " key1 , key2 "}):
        assert AuthMiddleware._validate_key("key1") == True
        assert AuthMiddleware._validate_key("key2") == True
        assert AuthMiddleware._validate_key(" key1 ") == False


# ── JWT secret strength validation ────────────────────────────────────


def test_weak_secret_rejected_in_production():
    """Short JWT_SECRET should be rejected when APP_ENV=production."""
    from agent.auth import _jwt_secret
    with patch.dict(os.environ, {"JWT_SECRET": "short", "APP_ENV": "production"}):
        try:
            _jwt_secret()
            raise AssertionError("Expected ValueError for weak secret in production")
        except ValueError as e:
            assert "at least 32 bytes" in str(e)


def test_weak_secret_allowed_in_development():
    """Short JWT_SECRET should be allowed when APP_ENV is not production."""
    from agent.auth import _jwt_secret
    with patch.dict(os.environ, {"JWT_SECRET": "short", "APP_ENV": "development"}):
        assert _jwt_secret() == "short"


def test_empty_secret_returns_empty():
    """Empty JWT_SECRET should return empty string (checked downstream)."""
    from agent.auth import _jwt_secret
    with patch.dict(os.environ, {"JWT_SECRET": "", "APP_ENV": "production"}):
        assert _jwt_secret() == ""


def test_long_secret_accepted_in_production():
    """Secret >= 32 bytes should be accepted in production."""
    from agent.auth import _jwt_secret
    long_secret = "a" * 32
    with patch.dict(os.environ, {"JWT_SECRET": long_secret, "APP_ENV": "production"}):
        assert _jwt_secret() == long_secret


def test_placeholder_rejected_in_production():
    """Known placeholder secrets should be rejected in production."""
    from agent.auth import _jwt_secret
    with patch.dict(os.environ, {"JWT_SECRET": "change-me", "APP_ENV": "production"}):
        try:
            _jwt_secret()
            raise AssertionError("Expected ValueError for placeholder secret")
        except ValueError as e:
            assert "placeholder" in str(e).lower()


def test_create_token_weak_secret_rejected():
    """create_access_token should fail with weak secret in production."""
    from agent.auth import create_access_token
    with patch.dict(os.environ, {"JWT_SECRET": "a", "APP_ENV": "production"}):
        try:
            create_access_token(subject="user1")
            raise AssertionError("Expected ValueError for weak secret")
        except ValueError as e:
            assert "at least 32 bytes" in str(e)


# ── Auth endpoint rate limiter ────────────────────────────────────────


def test_auth_rate_limiter_blocks_after_max_attempts():
    """IP should be blocked after max_attempts failures."""
    from app_fastapi import _AuthRateLimiter
    limiter = _AuthRateLimiter(max_attempts=3, window_seconds=60.0)
    ip = "192.168.1.100"

    # First 2 failures -- not blocked
    limiter.record_failure(ip)
    assert limiter.is_blocked(ip) is None
    limiter.record_failure(ip)
    assert limiter.is_blocked(ip) is None

    # 3rd failure -- blocked
    limiter.record_failure(ip)
    retry_after = limiter.is_blocked(ip)
    assert retry_after is not None
    assert retry_after > 0


def test_auth_rate_limiter_different_ips_independent():
    """Rate limit is per-IP; different IPs are independent."""
    from app_fastapi import _AuthRateLimiter
    limiter = _AuthRateLimiter(max_attempts=2, window_seconds=60.0)

    limiter.record_failure("10.0.0.1")
    limiter.record_failure("10.0.0.1")
    assert limiter.is_blocked("10.0.0.1") is not None
    assert limiter.is_blocked("10.0.0.2") is None  # different IP


def test_auth_rate_limiter_clear_resets():
    """clear() should reset failure history for an IP."""
    from app_fastapi import _AuthRateLimiter
    limiter = _AuthRateLimiter(max_attempts=2, window_seconds=60.0)
    ip = "10.0.0.1"

    limiter.record_failure(ip)
    limiter.record_failure(ip)
    assert limiter.is_blocked(ip) is not None

    limiter.clear(ip)
    assert limiter.is_blocked(ip) is None


def test_auth_rate_limiter_window_expiry():
    """Expired entries should be cleaned up."""
    import time as _time
    from app_fastapi import _AuthRateLimiter
    limiter = _AuthRateLimiter(max_attempts=2, window_seconds=0.1)  # 100ms window
    ip = "10.0.0.1"

    limiter.record_failure(ip)
    limiter.record_failure(ip)
    assert limiter.is_blocked(ip) is not None

    _time.sleep(0.15)  # wait for window to expire
    assert limiter.is_blocked(ip) is None


def test_auth_rate_limiter_custom_config():
    """Auth rate limiter respects env config."""
    from app_fastapi import _AuthRateLimiter
    limiter = _AuthRateLimiter(max_attempts=1, window_seconds=30.0)
    ip = "10.0.0.1"

    limiter.record_failure(ip)
    retry_after = limiter.is_blocked(ip)
    assert retry_after is not None
    assert retry_after <= 30.0
