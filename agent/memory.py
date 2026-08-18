# -*- coding: utf-8 -*-
"""PostgreSQL-backed users, sessions, conversation history and user profile memory.

SQLite is no longer used by the live application. Legacy ``user_memory.db`` is
kept only as a migration source/backup.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from psycopg import IntegrityError
from psycopg.types.json import Jsonb

from .runtime_db import connect, init_runtime_schema, json_value

_schema_lock = threading.Lock()
_schema_ready = False
logger = logging.getLogger("agent.memory")


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if not _schema_ready:
            init_runtime_schema()
            _schema_ready = True


def _get_connection():
    """Return a PostgreSQL connection with dict-like rows."""
    _ensure_schema()
    return connect()


@contextlib.contextmanager
def get_connection():
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate(conn=None) -> Dict[str, Any]:
    """Apply the idempotent PostgreSQL runtime schema."""
    _ensure_schema()
    tables = ("user_profiles", "conversation_history", "user_preferences",
              "tickets", "sessions", "users", "user_memories")
    with get_connection() as pg:
        result: Dict[str, Any] = {}
        for table in tables:
            rows = pg.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                (table,)).fetchall()
            result[table] = [r["column_name"] for r in rows]
        return result


def _init_db() -> None:
    _ensure_schema()


# User identity ---------------------------------------------------------

def create_user(user_id: str, password: Optional[str] = None,
                display_name: Optional[str] = None,
                tenant_id: str = "default") -> Dict[str, Any]:
    from .auth import hash_password
    now = datetime.now().isoformat()
    pw_hash = hash_password(password) if password else None
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, created_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (id) DO NOTHING", (tenant_id, tenant_id))
        cur = conn.execute(
            """INSERT INTO users
               (id, tenant_id, external_id, user_id, display_name, language,
                password_hash, created_at, updated_at, last_login)
               VALUES (%s, %s, %s, %s, %s, 'zh', %s, %s, %s, %s)
               ON CONFLICT (tenant_id, external_id) DO NOTHING""",
            (user_id, tenant_id, user_id, user_id, display_name or user_id,
             pw_hash, now, now, now))
        return {"user_id": user_id, "created": cur.rowcount > 0}


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=%s OR external_id=%s "
            "ORDER BY CASE WHEN user_id=%s THEN 0 ELSE 1 END LIMIT 1",
            (user_id, user_id, user_id)).fetchone()
        return dict(row) if row else None


def authenticate_user(user_id: str, password: Optional[str] = None,
                      allow_register: bool = True) -> Dict[str, Any]:
    from .auth import verify_password
    existing = get_user(user_id)
    if existing is None:
        if not allow_register:
            return {"ok": False, "user_id": user_id, "registered": False,
                    "reason": "user not found"}
        create_user(user_id, password=password)
        return {"ok": True, "user_id": user_id, "registered": True,
                "reason": "registered"}
    stored = existing.get("password_hash")
    if stored and (password is None or not verify_password(password, stored)):
        return {"ok": False, "user_id": user_id, "registered": False,
                "reason": "invalid credentials"}
    _touch_login(user_id)
    return {"ok": True, "user_id": user_id, "registered": False,
            "reason": "authenticated"}


def _touch_login(user_id: str) -> None:
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET last_login=%s, updated_at=%s "
            "WHERE user_id=%s OR external_id=%s",
            (now, now, user_id, user_id))


# Sessions --------------------------------------------------------------

def get_session_owner(session_id: str) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM sessions WHERE session_id=%s",
            (session_id,)).fetchone()
        return row["user_id"] if row and row.get("user_id") else None


def touch_session(user_id: str, session_id: str,
                  title: Optional[str] = None) -> None:
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO sessions
               (session_id, user_id, title, created_at, last_active, message_count)
               VALUES (%s, %s, %s, %s, %s, 0)
               ON CONFLICT(session_id) DO UPDATE SET
                 user_id=CASE
                    WHEN sessions.user_id IS NULL OR sessions.user_id=''
                      THEN EXCLUDED.user_id
                    WHEN sessions.user_id LIKE 'anon-%%'
                         AND EXCLUDED.user_id NOT LIKE 'anon-%%'
                      THEN EXCLUDED.user_id
                    ELSE sessions.user_id
                  END,
                 title=CASE WHEN sessions.title IS NULL OR sessions.title=''
                            THEN EXCLUDED.title ELSE sessions.title END,
                 last_active=EXCLUDED.last_active""",
            (session_id, user_id, title or "新会话", now, now))


def list_user_sessions(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT session_id, title, created_at, last_active, message_count
               FROM sessions WHERE user_id=%s
               ORDER BY last_active DESC NULLS LAST LIMIT %s""",
            (user_id, int(limit))).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            # One persisted row contains a user bubble and an assistant bubble.
            # Keep the table schema untouched, but show the UI-facing count.
            try:
                d["message_count"] = int(d.get("message_count") or 0) * 2
            except (TypeError, ValueError):
                d["message_count"] = 0
            out.append(d)
        return out


# Profiles and conversation history -----------------------------------

def save_profile(session_id: str, name: Optional[str] = None,
                 preferred_name: Optional[str] = None,
                 language: str = "zh",
                 user_id: Optional[str] = None) -> None:
    user_id = user_id or session_id
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO user_profiles
               (session_id, user_id, name, preferred_name, language, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT(session_id) DO UPDATE SET
                 user_id=EXCLUDED.user_id,
                 name=COALESCE(EXCLUDED.name, user_profiles.name),
                 preferred_name=COALESCE(EXCLUDED.preferred_name, user_profiles.preferred_name),
                 language=EXCLUDED.language, updated_at=EXCLUDED.updated_at""",
            (session_id, user_id, name, preferred_name, language, now, now))


def get_profile(session_id: str) -> Dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM user_profiles WHERE session_id=%s", (session_id,)).fetchone()
        return dict(row) if row else {}


def get_conversation_messages(session_id: str,
                              limit: int = 100) -> List[Dict[str, str]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT user_message, bot_reply, timestamp
               FROM conversation_history WHERE session_id=%s
               ORDER BY id ASC LIMIT %s""", (session_id, int(limit))).fetchall()
    messages: List[Dict[str, str]] = []
    for row in rows:
        ts = row.get("timestamp")
        if row.get("user_message"):
            messages.append({"role": "user", "content": row["user_message"],
                             "createdAt": ts})
        if row.get("bot_reply"):
            messages.append({"role": "assistant", "content": row["bot_reply"],
                             "createdAt": ts})
    return messages


def timeline_langchain_messages(session_id: str, limit: int = 100) -> List[Any]:
    """Load persisted conversation messages and remove adjacent checkpoint duplicates.

    Used to rebuild multi-turn context as LangChain message history.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    rows = get_conversation_messages(session_id, limit=limit)
    result: List[Any] = []
    seen = set()
    previous = None
    for row in rows:
        role = row.get("role", "user")
        content = row.get("content", "")
        key = (role, content)
        if key == previous or key in seen:
            continue
        seen.add(key)
        previous = key
        result.append(HumanMessage(content=content) if role == "user"
                      else AIMessage(content=content))
    return result


def save_conversation(session_id: str, user_message: str, bot_reply: str,
                      intent: str = "consult", emotion: str = "neutral",
                      emotion_intensity: int = 1, resolved: bool = False,
                      user_id: Optional[str] = None) -> None:
    """Persist one completed turn and session metadata atomically in PostgreSQL."""
    now = datetime.now().isoformat()
    user_id = user_id or session_id
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO conversation_history
               (user_id, session_id, user_message, bot_reply, intent, emotion,
                emotion_intensity, resolved, timestamp)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (user_id, session_id, user_message, bot_reply, intent, emotion,
             emotion_intensity, bool(resolved), now))
        conn.execute(
            """INSERT INTO sessions
               (session_id, user_id, title, created_at, last_active, message_count)
               VALUES (%s, %s, %s, %s, %s, 1)
               ON CONFLICT(session_id) DO UPDATE SET
                 user_id=CASE
                    WHEN sessions.user_id IS NULL OR sessions.user_id=''
                      THEN EXCLUDED.user_id
                    WHEN sessions.user_id LIKE 'anon-%%'
                         AND EXCLUDED.user_id NOT LIKE 'anon-%%'
                      THEN EXCLUDED.user_id
                    ELSE sessions.user_id
                  END,
                 title=CASE WHEN sessions.title IS NULL OR sessions.title=''
                            THEN EXCLUDED.title ELSE sessions.title END,
                 last_active=EXCLUDED.last_active,
                 message_count=sessions.message_count+1""",
            (session_id, user_id, user_message[:80], now, now))
    with contextlib.suppress(Exception):
        _update_product_interests(session_id, user_message, user_id=user_id)


def get_user_context(user_id: str) -> Dict[str, Any]:
    interests: List[str] = []
    known_issues: List[str] = []
    with get_connection() as conn:
        profile = conn.execute(
            "SELECT name, preferred_name, language FROM user_profiles "
            "WHERE user_id=%s ORDER BY updated_at DESC NULLS LAST LIMIT 1",
            (user_id,)).fetchone()
        for row in conn.execute(
                "SELECT product_interests, known_issues FROM user_preferences "
                "WHERE user_id=%s", (user_id,)).fetchall():
            for item in json_value(row.get("product_interests"), []):
                if item not in interests:
                    interests.append(item)
            for item in json_value(row.get("known_issues"), []):
                if item not in known_issues:
                    known_issues.append(item)
        recent = conn.execute(
            "SELECT user_message, intent FROM conversation_history "
            "WHERE user_id=%s AND resolved=FALSE ORDER BY timestamp DESC LIMIT 3",
            (user_id,)).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM conversation_history WHERE user_id=%s",
            (user_id,)).fetchone()["cnt"]
        session_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM sessions WHERE user_id=%s",
            (user_id,)).fetchone()["cnt"]
    return {
        "user_id": user_id,
        "name": profile.get("name") if profile else None,
        "preferred_name": profile.get("preferred_name") if profile else None,
        "product_interests": interests,
        "known_issues": known_issues,
        "recent_unresolved": [dict(r) for r in recent],
        "total_conversations": total,
        "session_count": session_count,
    }


def build_memory_context(user_id: str) -> str:
    context = get_user_context(user_id)
    parts = ["\n## \u7528\u6237\u8bb0\u5fc6"]
    if context["name"]:
        parts.append(f"- \u59d3\u540d: {context['name']}")
    if context["preferred_name"]:
        parts.append(f"- \u79f0\u547c: {context['preferred_name']}")
    if context["product_interests"]:
        parts.append("- \u4ea7\u54c1\u5174\u8da3: " + ", ".join(context["product_interests"]))
    if context["session_count"] > 1:
        parts.append(f"- \u4f1a\u8bdd\u6570: {context['session_count']}")
    if context["recent_unresolved"]:
        parts.append("- \u5f85\u89e3\u51b3: " + " | ".join(
            str(x.get("user_message", "")) for x in context["recent_unresolved"]))
    return "\n".join(parts) if len(parts) > 1 else ""


def mark_resolved(session_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE conversation_history SET resolved=TRUE "
            "WHERE session_id=%s AND resolved=FALSE", (session_id,))


def save_ticket(ticket: Dict[str, Any], max_attempts: int = 5) -> None:
    """Persist a ticket, retrying only a primary-key collision with a clean ID."""
    base_ticket_id = str(ticket["ticket_id"])
    ticket_id = base_ticket_id
    last_error: Optional[IntegrityError] = None
    for attempt in range(1, max_attempts + 1):
        try:
            with get_connection() as conn:
                conn.execute(
                    """INSERT INTO tickets
                       (ticket_id, user_id, session_id, issue_category, description,
                        resolution, satisfaction, priority, emotion,
                        emotion_intensity, message_count, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (ticket_id, ticket.get("user_id", ticket.get("session_id", "")),
                     ticket.get("session_id", ""), ticket["issue_category"],
                     ticket["description"], ticket["resolution"],
                     ticket["satisfaction"], ticket["priority"],
                     ticket.get("emotion", "neutral"),
                     ticket.get("emotion_intensity", 1),
                     ticket.get("message_count", 0), ticket["created_at"]))
            ticket["ticket_id"] = ticket_id
            return
        except IntegrityError as exc:
            last_error = exc
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate not in (None, "23505"):
                raise
            if attempt >= max_attempts:
                break
            ticket_id = f"{base_ticket_id}-{uuid.uuid4().hex[:8]}"
            logger.warning("ticket id collision; retrying: attempt=%s ticket_id=%s",
                           attempt, ticket_id)
    raise RuntimeError(
        f"failed to save ticket after {max_attempts} attempts (last_ticket_id={ticket_id})"
    ) from last_error


def get_stats() -> Dict[str, Any]:
    with get_connection() as conn:
        users = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
        sessions = conn.execute("SELECT COUNT(*) AS cnt FROM sessions").fetchone()["cnt"]
        conversations = conn.execute(
            "SELECT COUNT(*) AS cnt FROM conversation_history").fetchone()["cnt"]
        unresolved = conn.execute(
            "SELECT COUNT(*) AS cnt FROM conversation_history WHERE resolved=FALSE").fetchone()["cnt"]
    return {"unique_users": users, "unique_sessions": sessions,
            "total_conversations": conversations,
            "unresolved_issues": unresolved, "db_backend": "postgresql"}


def _update_product_interests(session_id: str, message: str,
                              user_id: Optional[str] = None) -> None:
    user_id = user_id or session_id
    product_keywords = {
        "\u667a\u80fd\u97f3\u7bb1": ["\u97f3\u7bb1", "speaker", "\u97f3\u54cd", "\u667a\u80fd\u97f3\u7bb1"],
        "\u667a\u80fd\u5bb6\u5c45": ["\u667a\u80fd\u706f", "\u7a7a\u8c03", "\u95e8\u9501", "\u5bb6\u7535", "\u667a\u80fd\u95e8\u9501", "zigbee"],
        "\u914d\u4ef6": ["\u652f\u67b6", "\u914d\u4ef6", "\u5e95\u5ea7", "\u5145\u7535\u5668", "\u6570\u636e\u7ebf", "\u7535\u6c60\u5e95\u5ea7"],
    }
    detected = [p for p, kws in product_keywords.items()
                if any(k.lower() in message.lower() for k in kws)]
    if not detected:
        return
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT product_interests FROM user_preferences WHERE session_id=%s",
            (session_id,)).fetchall()
        interests: List[str] = []
        for row in rows:
            for item in json_value(row.get("product_interests"), []):
                if item not in interests:
                    interests.append(item)
        for item in detected:
            if item not in interests:
                interests.append(item)
        conn.execute("DELETE FROM user_preferences WHERE session_id=%s", (session_id,))
        conn.execute(
            """INSERT INTO user_preferences
               (user_id, session_id, product_interests, known_issues,
                communication_style, update_count)
               VALUES (%s, %s, %s, %s, NULL, 1)""",
            (user_id, session_id, Jsonb(interests), Jsonb([])))
