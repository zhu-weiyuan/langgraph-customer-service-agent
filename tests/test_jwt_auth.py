import pytest
from agent.auth import AuthMiddleware


class Handler:
    def __init__(self, headers):
        self.headers = headers
        self.path = "/api/chat"


def test_jwt_round_trip(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    token = AuthMiddleware.create_access_token("alice", "acme")
    handler = Handler({"Authorization": f"Bearer {token}"})
    assert AuthMiddleware.check_api_key(handler)
    assert handler.auth_subject == "alice"
    assert handler.auth_tenant_id == "acme"


def test_invalid_token_rejected(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    assert not AuthMiddleware.check_api_key(Handler({"Authorization": "Bearer invalid"}))


def test_legacy_api_key_works(monkeypatch):
    monkeypatch.setenv("API_KEYS", "legacy-key")
    handler = Handler({"X-API-Key": "legacy-key"})
    assert AuthMiddleware.check_api_key(handler)
    assert handler.auth_scheme == "api_key"


def test_example_jwt_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "replace-with-a-long-random-secret")
    with pytest.raises(ValueError, match="JWT_SECRET"):
        AuthMiddleware.create_access_token("alice")


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("JWT_ACCESS_TTL_SECONDS", "-1")
    token = AuthMiddleware.create_access_token("alice")
    assert not AuthMiddleware.check_api_key(Handler({"Authorization": f"Bearer {token}"}))
