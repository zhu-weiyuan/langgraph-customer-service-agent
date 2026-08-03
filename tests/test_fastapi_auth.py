import hashlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEYS", "bootstrap-key")
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    import app_fastapi
    return TestClient(app_fastapi.app)


def test_protected_fastapi_route_requires_auth(client):
    response = client.get("/api/session/user-any")
    assert response.status_code == 401


def test_token_exchange_and_cross_user_session_denial(client):
    response = client.post(
        "/api/auth/token",
        json={"api_key": "bootstrap-key", "subject": "alice", "tenant_id": "acme"},
    )
    assert response.status_code == 200
    token = response.json()["access_token"]
    bob_session = "user-" + hashlib.sha256(b"bob").hexdigest()[:24]

    response = client.get(
        f"/api/session/{bob_session}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_invalid_bootstrap_key_is_rejected(client):
    response = client.post(
        "/api/auth/token",
        json={"api_key": "wrong-key", "subject": "alice"},
    )
    assert response.status_code == 401
