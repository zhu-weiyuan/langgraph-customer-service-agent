"""Tests for API authentication middleware."""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, r"C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent")

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
