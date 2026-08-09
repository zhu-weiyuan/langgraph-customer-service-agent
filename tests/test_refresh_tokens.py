"""Unit tests for refresh-token cryptography and configuration."""

import pytest

from agent.auth import (
    create_refresh_token,
    hash_refresh_token,
    refresh_token_ttl_seconds,
)


def test_refresh_token_is_opaque_high_entropy_and_unique(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret-" + "x" * 32)
    first = create_refresh_token()
    second = create_refresh_token()

    assert first != second
    assert len(first) >= 60
    assert first.count(".") == 0  # not a JWT-shaped credential


def test_refresh_token_hash_is_keyed_and_deterministic(monkeypatch):
    monkeypatch.setenv("REFRESH_TOKEN_PEPPER", "pepper-" + "x" * 32)
    raw = create_refresh_token()
    digest = hash_refresh_token(raw)

    assert digest == hash_refresh_token(raw)
    assert digest != raw
    assert len(digest) == 64
    assert digest != hash_refresh_token(raw + "-tampered")


def test_refresh_tokens_fail_closed_without_server_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("REFRESH_TOKEN_PEPPER", raising=False)

    with pytest.raises(ValueError, match="JWT_SECRET|REFRESH_TOKEN_PEPPER"):
        create_refresh_token()


def test_refresh_ttl_has_safe_minimum_and_default(monkeypatch):
    monkeypatch.setenv("JWT_REFRESH_TTL_SECONDS", "1")
    assert refresh_token_ttl_seconds() == 60

    monkeypatch.setenv("JWT_REFRESH_TTL_SECONDS", "not-a-number")
    assert refresh_token_ttl_seconds() == 60 * 60 * 24 * 14

    monkeypatch.setenv("JWT_REFRESH_TTL_SECONDS", "3600")
    assert refresh_token_ttl_seconds() == 3600
