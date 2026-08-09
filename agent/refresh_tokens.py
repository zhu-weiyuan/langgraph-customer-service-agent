# -*- coding: utf-8 -*-
"""Refresh-token issuance, rotation, revocation, and PostgreSQL persistence.

Only token hashes are stored in PostgreSQL.  The raw refresh token is returned
exactly once to the caller that must place it in a HttpOnly browser cookie.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .auth import create_refresh_token, hash_refresh_token, refresh_token_ttl_seconds
from .runtime_db import connection, init_runtime_schema


@dataclass(frozen=True)
class RefreshGrant:
    """A newly issued raw refresh token and its authenticated identity."""

    token: str
    user_id: str
    tenant_id: str
    expires_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return secrets.token_urlsafe(24)


def _ip_hash(ip: str) -> str:
    """Store a non-reversible, deployment-scoped audit value instead of an IP."""
    pepper = os.getenv("REFRESH_TOKEN_IP_PEPPER") or os.getenv("JWT_SECRET", "")
    return hashlib.sha256(f"{pepper}:{ip or ''}".encode("utf-8")).hexdigest()


def _metadata(value: Optional[str], limit: int) -> str:
    return (value or "").strip()[:limit]


def issue_refresh_token(
    user_id: str,
    tenant_id: str = "default",
    *,
    user_agent: str = "",
    client_ip: str = "",
) -> RefreshGrant:
    """Create a browser session token and persist only its keyed hash."""
    init_runtime_schema()
    raw_token = create_refresh_token()
    token_id = _new_id()
    family_id = _new_id()
    expires_at = _utcnow() + timedelta(seconds=refresh_token_ttl_seconds())
    with connection() as conn:
        conn.execute(
            """INSERT INTO refresh_tokens
               (id, family_id, user_id, tenant_id, token_hash, expires_at,
                user_agent, ip_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                token_id,
                family_id,
                user_id,
                tenant_id or "default",
                hash_refresh_token(raw_token),
                expires_at,
                _metadata(user_agent, 512),
                _ip_hash(client_ip),
            ),
        )
    return RefreshGrant(raw_token, user_id, tenant_id or "default", expires_at)


def rotate_refresh_token(
    raw_token: str,
    *,
    user_agent: str = "",
    client_ip: str = "",
) -> Optional[RefreshGrant]:
    """Atomically rotate a valid token. Reuse revokes its whole token family.

    ``None`` deliberately does not distinguish malformed, expired, revoked, and
    replayed tokens so callers never reveal session-state details to attackers.
    """
    if not raw_token:
        return None
    init_runtime_schema()
    old_hash = hash_refresh_token(raw_token)
    now = _utcnow()
    new_raw = create_refresh_token()
    new_id = _new_id()
    with connection() as conn:
        row = conn.execute(
            """SELECT id, family_id, user_id, tenant_id, expires_at, revoked_at
               FROM refresh_tokens
               WHERE token_hash=%s
               FOR UPDATE""",
            (old_hash,),
        ).fetchone()
        if not row:
            return None

        expires_at = row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        invalid = bool(row["revoked_at"]) or expires_at <= now
        if invalid:
            # A revoked token presented again indicates possible replay.  End
            # the whole browser-session family, while keeping other devices live.
            conn.execute(
                """UPDATE refresh_tokens
                   SET revoked_at=COALESCE(revoked_at, NOW())
                   WHERE family_id=%s AND revoked_at IS NULL""",
                (row["family_id"],),
            )
            return None

        next_expiry = now + timedelta(seconds=refresh_token_ttl_seconds())
        conn.execute(
            """INSERT INTO refresh_tokens
               (id, family_id, user_id, tenant_id, token_hash, expires_at,
                user_agent, ip_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                new_id,
                row["family_id"],
                row["user_id"],
                row["tenant_id"],
                hash_refresh_token(new_raw),
                next_expiry,
                _metadata(user_agent, 512),
                _ip_hash(client_ip),
            ),
        )
        conn.execute(
            """UPDATE refresh_tokens
               SET revoked_at=NOW(), replaced_by=%s, last_used_at=NOW()
               WHERE id=%s""",
            (new_id, row["id"]),
        )
    return RefreshGrant(new_raw, row["user_id"], row["tenant_id"], next_expiry)


def revoke_refresh_token(raw_token: str) -> bool:
    """Revoke the current browser session; raw tokens are never logged/stored."""
    if not raw_token:
        return False
    init_runtime_schema()
    with connection() as conn:
        cur = conn.execute(
            """UPDATE refresh_tokens SET revoked_at=NOW()
               WHERE token_hash=%s AND revoked_at IS NULL""",
            (hash_refresh_token(raw_token),),
        )
        return cur.rowcount > 0


def revoke_user_tokens(user_id: str, tenant_id: str = "default") -> int:
    """Revoke every refresh session for an account (for password reset/admin use)."""
    init_runtime_schema()
    with connection() as conn:
        cur = conn.execute(
            """UPDATE refresh_tokens SET revoked_at=NOW()
               WHERE user_id=%s AND tenant_id=%s AND revoked_at IS NULL""",
            (user_id, tenant_id or "default"),
        )
        return cur.rowcount
