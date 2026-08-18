import hashlib

import pytest
from fastapi.testclient import TestClient



@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEYS", "bootstrap-key")
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    import app_fastapi
    return TestClient(app_fastapi.app)


@pytest.mark.integration
def test_protected_fastapi_route_requires_auth(client):
    response = client.get("/api/session/user-any")
    assert response.status_code == 401


@pytest.mark.integration
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


@pytest.mark.integration
def test_invalid_bootstrap_key_is_rejected(client):
    response = client.post(
        "/api/auth/token",
        json={"api_key": "wrong-key", "subject": "alice"},
    )
    assert response.status_code == 401


@pytest.mark.integration
def test_production_rejects_browser_controlled_user_header(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("API_KEYS", "")
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("AUTH_ALLOW_HEADER_FALLBACK", "1")  # must still be ignored
    import app_fastapi

    with TestClient(app_fastapi.app) as client:
        response = client.get("/api/sessions", headers={"X-User-Id": "victim"})
    assert response.status_code == 401


@pytest.mark.integration
def test_anonymous_client_id_does_not_share_session_owner(monkeypatch):
    monkeypatch.setenv("API_KEYS", "")
    monkeypatch.setenv("AUTH_ALLOW_HEADER_FALLBACK", "0")
    monkeypatch.setenv("APP_ENV", "development")
    import app_fastapi

    owners = {"anon-session": "anon-user-a"}
    monkeypatch.setattr("agent.memory.get_session_owner", owners.get)
    first = type("Request", (), {})()
    first.state = type("State", (), {
        "auth_scheme": "anonymous", "auth_subject": "anon-user-a", "user_id": "anon-user-a",
    })()
    second = type("Request", (), {})()
    second.state = type("State", (), {
        "auth_scheme": "anonymous", "auth_subject": "anon-user-b", "user_id": "anon-user-b",
    })()
    assert app_fastapi._owns_session(first, "anon-session")
    assert not app_fastapi._owns_session(second, "anon-session")
