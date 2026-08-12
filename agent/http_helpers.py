# -*- coding: utf-8 -*-
"""HTTP/SSE helpers backed by PostgreSQL for the live FastAPI service."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

IDEMPOTENCY_HEADER = "X-Idempotency-Key"
MAX_IDEMPOTENCY_KEY_LEN = 128


def sse_format(payload: Dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"


def sse_error(message: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"error": str(message)}
    payload.update(extra)
    return sse_format(payload)


def idempotency_key_from_headers(headers: Mapping[str, str]) -> Optional[str]:
    value = None
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = getter(IDEMPOTENCY_HEADER)
    if value is None:
        for key in headers:
            if str(key).lower() == IDEMPOTENCY_HEADER.lower():
                value = headers[key]
                break
    if value is None:
        return None
    value = str(value).strip()
    if not value or len(value) > MAX_IDEMPOTENCY_KEY_LEN:
        return None
    return value


<<<<<<< HEAD
def _is_sqlite(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "sqlite3"


def _dict_rows(cursor) -> List[Dict[str, Any]]:
    columns = [str(col[0]) for col in (cursor.description or [])]
    return [dict(zip(columns, row)) if not isinstance(row, Mapping)
            else dict(row) for row in cursor.fetchall()]


def _table_exists(conn, name: str) -> bool:
    """Check tables for both the live PostgreSQL connection and test SQLite."""
    if _is_sqlite(conn):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return bool(row)
    row = conn.execute("SELECT to_regclass(%s) AS name",
                       (f"public.{name}",)).fetchone()
    if isinstance(row, Mapping):
        return bool(row.get("name"))
    return bool(row and row[0])
=======
def _table_exists(conn, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS name", (f"public.{name}",)).fetchone()
    return bool(row and row.get("name"))
>>>>>>> origin/master


def query_sessions(conn, search: str = "", limit: int = 50) -> Dict[str, Any]:
    if not _table_exists(conn, "conversation_history"):
        return {"sessions": []}
<<<<<<< HEAD
    if _is_sqlite(conn):
        where = ""
        params: List[Any] = []
        if search:
            where = "WHERE user_message LIKE ? OR bot_reply LIKE ?"
            like = f"%{search}%"
            params.extend([like, like])
        params.append(int(limit))
        cur = conn.execute(
            f"""SELECT session_id, COUNT(*) AS msg_count,
                       MAX(timestamp) AS last_at,
                       GROUP_CONCAT(DISTINCT COALESCE(intent, '')) AS intents
                  FROM conversation_history {where}
                 GROUP BY session_id
                 ORDER BY last_at DESC
                 LIMIT ?""",
            tuple(params),
        )
        rows = _dict_rows(cur)
        result = []
        for row in rows:
            last = _dict_rows(conn.execute(
                """SELECT user_message, intent, emotion, emotion_intensity, timestamp
                     FROM conversation_history WHERE session_id=?
                     ORDER BY timestamp DESC, id DESC LIMIT 1""",
                (row["session_id"],),
            ))
            last = last[0] if last else {}
            result.append({
                "session_id": row["session_id"],
                "message_count": row["msg_count"],
                "last_activity": last.get("timestamp") or row["last_at"] or "",
                "intents": [x for x in (row.get("intents") or "").split(",") if x],
                "preview": (last.get("user_message") or "")[:60],
                "intent": last.get("intent"),
                "emotion": last.get("emotion"),
                "emotion_intensity": last.get("emotion_intensity") or 1,
            })
        return {"sessions": result}

=======
>>>>>>> origin/master
    where = ""
    params: List[Any] = []
    if search:
        where = "WHERE user_message ILIKE %s OR bot_reply ILIKE %s"
        like = f"%{search}%"
        params.extend([like, like])
    params.append(int(limit))
    rows = conn.execute(
        f"""SELECT session_id, COUNT(*) AS msg_count,
                   MAX(timestamp) AS last_at,
                   STRING_AGG(DISTINCT COALESCE(intent, ''), ',') AS intents
              FROM conversation_history {where}
             GROUP BY session_id
             ORDER BY last_at DESC NULLS LAST
             LIMIT %s""",
        tuple(params),
    ).fetchall()
    result = []
    for row in rows:
        last = conn.execute(
            """SELECT user_message, intent, emotion, emotion_intensity, timestamp
                 FROM conversation_history WHERE session_id=%s
                 ORDER BY timestamp DESC NULLS LAST, id DESC LIMIT 1""",
            (row["session_id"],),
        ).fetchone() or {}
        result.append({
            "session_id": row["session_id"],
            "message_count": row["msg_count"],
            "last_activity": last.get("timestamp") or row["last_at"] or "",
            "intents": [x for x in (row.get("intents") or "").split(",") if x],
            "preview": (last.get("user_message") or "")[:60],
            "intent": last.get("intent"),
            "emotion": last.get("emotion"),
            "emotion_intensity": last.get("emotion_intensity") or 1,
        })
    return {"sessions": result}


def query_session_detail(conn, session_id: str) -> Dict[str, Any]:
    messages: List[Dict[str, Any]] = []
    intent, emotion = "unknown", "neutral"
    if _table_exists(conn, "conversation_history"):
<<<<<<< HEAD
        placeholder = "?" if _is_sqlite(conn) else "%s"
        rows = _dict_rows(conn.execute(
            f"""SELECT user_message, bot_reply, intent, emotion, timestamp
                 FROM conversation_history WHERE session_id={placeholder}
                 ORDER BY id ASC""",
            (session_id,),
        ))
=======
        rows = conn.execute(
            """SELECT user_message, bot_reply, intent, emotion, timestamp
                 FROM conversation_history WHERE session_id=%s ORDER BY id ASC""",
            (session_id,),
        ).fetchall()
>>>>>>> origin/master
        for row in rows:
            if row.get("user_message"):
                messages.append({"role": "user", "content": row["user_message"],
                                 "createdAt": row.get("timestamp")})
            if row.get("bot_reply"):
                messages.append({"role": "assistant", "content": row["bot_reply"],
                                 "createdAt": row.get("timestamp")})
            intent = row.get("intent") or intent
            emotion = row.get("emotion") or emotion
    return {"session_id": session_id, "messages": messages,
            "message_count": len(messages), "intent": intent,
            "emotion": emotion, "retry_count": 0}


def query_analytics(conn) -> Dict[str, Any]:
    result = {"total_conversations": 0, "avg_reply_length": 0,
              "ratings": {"total": 0, "average": 0},
              "tickets": {"total": 0, "by_priority": {}},
              "intents": {}, "emotions": {}}
    if not _table_exists(conn, "conversation_history"):
        return result
<<<<<<< HEAD
    if _is_sqlite(conn):
        row = _dict_rows(conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(AVG(LENGTH(COALESCE(bot_reply,''))),0) AS avg_len FROM conversation_history"))[0]
        result["total_conversations"] = row["total"]
        result["avg_reply_length"] = round(float(row["avg_len"] or 0), 2)
        if _table_exists(conn, "ratings"):
            row = _dict_rows(conn.execute(
                "SELECT COUNT(*) AS total, COALESCE(AVG(stars),0) AS average FROM ratings"))[0]
            result["ratings"] = {"total": row["total"], "average": round(float(row["average"] or 0), 2)}
        if _table_exists(conn, "tickets"):
            total = _dict_rows(conn.execute("SELECT COUNT(*) AS total FROM tickets"))[0]["total"]
            rows = _dict_rows(conn.execute(
                "SELECT COALESCE(priority,'unknown') AS key, COUNT(*) AS n FROM tickets GROUP BY COALESCE(priority,'unknown')"))
            result["tickets"] = {"total": total, "by_priority": {r["key"]: r["n"] for r in rows}}
        rows = _dict_rows(conn.execute(
            "SELECT COALESCE(intent,'unknown') AS key, COUNT(*) AS n FROM conversation_history GROUP BY COALESCE(intent,'unknown') ORDER BY n DESC LIMIT 8"))
        result["intents"] = {r["key"]: r["n"] for r in rows}
        rows = _dict_rows(conn.execute(
            "SELECT COALESCE(emotion,'unknown') AS key, COUNT(*) AS n FROM conversation_history GROUP BY COALESCE(emotion,'unknown') ORDER BY n DESC LIMIT 8"))
        result["emotions"] = {r["key"]: r["n"] for r in rows}
        return result

=======
>>>>>>> origin/master
    row = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(AVG(LENGTH(COALESCE(bot_reply,''))),0) AS avg_len FROM conversation_history"
    ).fetchone()
    result["total_conversations"] = row["total"]
    result["avg_reply_length"] = round(float(row["avg_len"] or 0), 2)
    if _table_exists(conn, "ratings"):
        row = conn.execute("SELECT COUNT(*) AS total, COALESCE(AVG(stars),0) AS average FROM ratings").fetchone()
        result["ratings"] = {"total": row["total"], "average": round(float(row["average"] or 0), 2)}
    if _table_exists(conn, "tickets"):
        total = conn.execute("SELECT COUNT(*) AS total FROM tickets").fetchone()["total"]
        rows = conn.execute("SELECT COALESCE(priority,'unknown') AS key, COUNT(*) AS n FROM tickets GROUP BY COALESCE(priority,'unknown')").fetchall()
        result["tickets"] = {"total": total, "by_priority": {r["key"]: r["n"] for r in rows}}
    result["intents"] = {r["key"]: r["n"] for r in conn.execute(
        "SELECT COALESCE(intent,'unknown') AS key, COUNT(*) AS n FROM conversation_history GROUP BY COALESCE(intent,'unknown') ORDER BY n DESC LIMIT 8").fetchall()}
    result["emotions"] = {r["key"]: r["n"] for r in conn.execute(
        "SELECT COALESCE(emotion,'unknown') AS key, COUNT(*) AS n FROM conversation_history GROUP BY COALESCE(emotion,'unknown') ORDER BY n DESC LIMIT 8").fetchall()}
    return result


RATINGS_DDL = ""
RATINGS_INSERT = "INSERT INTO ratings (session_id,message_index,stars,rated_at) VALUES (%s,%s,%s,%s)"
REACTIONS_DDL = ""
REACTIONS_INSERT = "INSERT INTO reactions (session_id,message_id,emoji,active,reacted_at) VALUES (%s,%s,%s,%s,%s)"
FEEDBACK_DDL = ""
FEEDBACK_INSERT = "INSERT INTO feedback (session_id,query,answer,rating,comment,created_at) VALUES (%s,%s,%s,%s,%s,%s)"


def admin_auth_status(jwt_configured: bool,
                      claims: Optional[Mapping[str, Any]]) -> Tuple[bool, int, str]:
    """管理端授权判定（纯函数）。

    - JWT 未配置（无 JWT_SECRET）→ (False, 403, ...)：管理端永远拒绝。
    - 无 claims（未带/无效 Bearer JWT）→ (False, 401, ...)
    - claims.scope != 'admin'（支持 "a b c" 空格分隔）→ (False, 403, ...)
    - 通过 → (True, 200, "ok")
    """
    if not jwt_configured:
        return False, 403, "admin API disabled: JWT_SECRET is not configured"
    if not claims:
        return False, 401, "admin API requires a valid JWT bearer token"
    scope = str(claims.get("scope", "") or "")
    scopes = set(scope.replace(",", " ").split())
    if "admin" not in scopes:
        return False, 403, "admin scope required"
    return True, 200, "ok"


def _version_summary(pv: Any) -> Dict[str, Any]:
    return {
        "version_no": getattr(pv, "version_no", None),
        "status": getattr(pv, "status", None),
        "change_reason": getattr(pv, "change_reason", ""),
        "created_at": str(getattr(pv, "created_at", "")),
    }


def admin_prompt_action(registry: Any, action: str,
                        payload: Optional[Mapping[str, Any]] = None,
                        name: str = "system_prompt") -> Tuple[int, Dict[str, Any]]:
    """/api/admin/prompts 状态机分发（registry 注入，可 mock）。

    action ∈ {list, approve, promote, rollback}
    - list     → 200 {"name", "versions":[...], "active": {...}}
    - approve  → payload {"version": int, "percent": int=10}
                 set_status(approved) + release(percent) 灰度
    - promote  → promote_full（当前灰度 → 100%）
    - rollback → rollback（回滚到上一全量版本）
    返回 (status_code, body)。业务异常 → 400/404。
    """
    payload = dict(payload or {})
    name = str(payload.get("name", name) or name)
    try:
        if action == "list":
            versions = [_version_summary(v) for v in registry.list_versions(name)]
            try:
                active: Optional[Dict[str, Any]] = _version_summary(
                    registry.get_active(name))
            except Exception:
                active = None
            return 200, {"name": name, "versions": versions, "active": active}

        if action == "approve":
            if "version" not in payload:
                return 400, {"error": "field 'version' is required"}
            version_no = int(payload["version"])
            percent = int(payload.get("percent", 10))
            if not (0 < percent <= 100):
                return 400, {"error": "percent must be in (0, 100]"}
            pv = registry.get_version(name, version_no)
            registry.set_status(pv.version_id, "approved")
            rel = registry.release(name, version_no, percent)
            return 200, {"ok": True, "action": "approve", "release": rel}

        if action == "promote":
            rel = registry.promote_full(name)
            return 200, {"ok": True, "action": "promote", "release": rel}

        if action == "rollback":
            rel = registry.rollback(name)
            return 200, {"ok": True, "action": "rollback", "release": rel}

        return 404, {"error": f"unknown admin action: {action}"}
    except KeyError as exc:
        return 404, {"error": str(exc)}
    except (ValueError, TypeError) as exc:
        return 400, {"error": str(exc)}


__all__ = [
    "IDEMPOTENCY_HEADER", "sse_format", "sse_error",
    "idempotency_key_from_headers", "query_sessions", "query_session_detail",
    "query_analytics", "RATINGS_DDL", "RATINGS_INSERT", "REACTIONS_DDL",
    "REACTIONS_INSERT", "FEEDBACK_DDL", "FEEDBACK_INSERT",
    "admin_auth_status", "admin_prompt_action",
]
