"""Idempotently import legacy SQLite runtime files into PostgreSQL.

The SQLite files are read-only migration sources. The live server never opens them.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

from agent.runtime_db import connect, init_runtime_schema  # noqa: E402


def rows(path: Path, table: str):
    if not path.exists():
        return []
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return [dict(r) for r in db.execute(f'SELECT * FROM "{table}"').fetchall()] if exists else []
    finally:
        db.close()


def jsonb(value: Any, default):
    if value is None or value == "":
        return Jsonb(default)
    if isinstance(value, (dict, list)):
        return Jsonb(value)
    try:
        return Jsonb(json.loads(value))
    except Exception:
        return Jsonb(default)


def generic_import(pg, path: Path, table: str, *, json_cols=(), bool_cols=(), conflict="DO NOTHING") -> int:
    source = rows(path, table)
    if not source:
        return 0
    cols = list(source[0])
    quoted_cols = ", ".join('"' + c + '"' for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders}) ON CONFLICT {conflict}' 
    for row in source:
        values = []
        for col in cols:
            value = row[col]
            if col in json_cols:
                value = jsonb(value, {} if col.endswith("_json") else [])
            elif col in bool_cols and value is not None:
                value = bool(value)
            values.append(value)
        pg.execute(sql, tuple(values))
    return len(source)


def import_core(pg, path: Path, counts: dict[str, int]):
    for table in ("tenants", "users", "conversations"):
        counts[table] = generic_import(pg, path, table)
    counts["conversation_messages"] = generic_import(pg, path, "conversation_messages", json_cols=("metadata_json",))
    counts["conversation_feedback"] = generic_import(pg, path, "conversation_feedback", json_cols=("value_json",))
    counts["tool_calls"] = generic_import(pg, path, "tool_calls", json_cols=("arguments_json", "result_json"))
    counts["legacy_history_migrations"] = generic_import(pg, path, "legacy_history_migrations")

    for row in rows(path, "sessions"):
        pg.execute("""INSERT INTO sessions(session_id,user_id,title,created_at,last_active,message_count)
                      VALUES (%s,%s,%s,%s,%s,%s)
                      ON CONFLICT(session_id) DO UPDATE SET
                        user_id=EXCLUDED.user_id,title=EXCLUDED.title,
                        created_at=EXCLUDED.created_at,last_active=EXCLUDED.last_active,
                        message_count=EXCLUDED.message_count""", tuple(row.get(k) for k in
                        ("session_id","user_id","title","created_at","last_active","message_count")))
    counts["sessions"] = len(rows(path, "sessions"))

    counts["conversation_history"] = generic_import(
        pg, path, "conversation_history", bool_cols=("resolved",),
        conflict="(id) DO UPDATE SET session_id=EXCLUDED.session_id,user_message=EXCLUDED.user_message,bot_reply=EXCLUDED.bot_reply,intent=EXCLUDED.intent,emotion=EXCLUDED.emotion,emotion_intensity=EXCLUDED.emotion_intensity,resolved=EXCLUDED.resolved,timestamp=EXCLUDED.timestamp,user_id=EXCLUDED.user_id")
    counts["user_profiles"] = generic_import(pg, path, "user_profiles", conflict="(session_id) DO UPDATE SET name=EXCLUDED.name,preferred_name=EXCLUDED.preferred_name,language=EXCLUDED.language,updated_at=EXCLUDED.updated_at,user_id=EXCLUDED.user_id")

    for row in rows(path, "user_preferences"):
        pg.execute("""DELETE FROM user_preferences WHERE session_id=%s""", (row["session_id"],))
        pg.execute("""INSERT INTO user_preferences(session_id,product_interests,known_issues,communication_style,update_count,user_id)
                      VALUES (%s,%s,%s,%s,%s,%s)""",
                   (row["session_id"], jsonb(row.get("product_interests"), []),
                    jsonb(row.get("known_issues"), []), row.get("communication_style"),
                    row.get("update_count") or 1, row.get("user_id")))
    counts["user_preferences"] = len(rows(path, "user_preferences"))

    counts["tickets"] = generic_import(pg, path, "tickets", conflict="(ticket_id) DO UPDATE SET session_id=EXCLUDED.session_id,description=EXCLUDED.description,resolution=EXCLUDED.resolution,satisfaction=EXCLUDED.satisfaction,priority=EXCLUDED.priority,user_id=EXCLUDED.user_id")
    counts["ratings"] = generic_import(pg, path, "ratings", conflict="(id) DO NOTHING")
    counts["reactions"] = generic_import(pg, path, "reactions", bool_cols=(), conflict="(id) DO NOTHING")
    counts["user_memories"] = generic_import(pg, path, "user_memories", bool_cols=("is_deleted",), conflict="(id) DO NOTHING")


def import_p4(pg, path: Path, counts: dict[str, int]):
    counts["bad_cases"] = generic_import(pg, path, "bad_cases", conflict="(id) DO NOTHING")
    for row in rows(path, "session_last_query"):
        pg.execute("""INSERT INTO session_last_query(session_id,query,ts) VALUES(%s,%s,%s)
                      ON CONFLICT(session_id) DO UPDATE SET query=EXCLUDED.query,ts=EXCLUDED.ts""",
                   (row["session_id"], row["query"], row["ts"]))
    counts["session_last_query"] = len(rows(path, "session_last_query"))

    template_map = {}
    for row in rows(path, "prompt_template"):
        found = pg.execute("""INSERT INTO prompt_template(name,kind,created_at) VALUES(%s,%s,%s)
                            ON CONFLICT(name) DO UPDATE SET kind=EXCLUDED.kind
                            RETURNING id""", (row["name"], row["kind"], row["created_at"])).fetchone()
        template_map[row["id"]] = found["id"]
    version_map = {}
    versions = rows(path, "prompt_version")
    for row in versions:
        tid = template_map[row["template_id"]]
        found = pg.execute("""INSERT INTO prompt_version(template_id,version_no,content,variables_schema,parent_version_id,status,change_reason,diff,created_at)
                            VALUES(%s,%s,%s,%s,NULL,%s,%s,%s,%s)
                            ON CONFLICT(template_id,version_no) DO UPDATE SET content=EXCLUDED.content
                            RETURNING id""", (tid,row["version_no"],row["content"],row["variables_schema"],row["status"],row["change_reason"],row["diff"],row["created_at"])).fetchone()
        version_map[row["id"]] = found["id"]
    for row in rows(path, "prompt_release"):
        pg.execute("""INSERT INTO prompt_release(template_id,version_id,env,tenant,percent,active,created_at)
                      SELECT %s,%s,%s,%s,%s,%s,%s
                      WHERE NOT EXISTS (SELECT 1 FROM prompt_release WHERE template_id=%s AND version_id=%s AND env=%s AND tenant IS NOT DISTINCT FROM %s AND percent=%s AND created_at=%s)""",
                   (template_map[row["template_id"]], version_map[row["version_id"]], row["env"], row["tenant"], row["percent"], row["active"], row["created_at"],
                    template_map[row["template_id"]], version_map[row["version_id"]], row["env"], row["tenant"], row["percent"], row["created_at"]))
    counts["prompt_template"] = len(template_map); counts["prompt_version"] = len(version_map)


def import_traces(pg, path: Path, counts: dict[str, int]):
    source = rows(path, "traces")
    for row in source:
        payload = jsonb(row.get("trace_json"), {})
        pg.execute("""INSERT INTO traces(request_id,user_id,input_text,total_latency_ms,completed_at,trace_json,session_id,tenant,scene,total_ms,cost,failed,low_score,created_at)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT(request_id) DO UPDATE SET trace_json=EXCLUDED.trace_json""",
                   (row.get("request_id"),row.get("user_id"),row.get("input_text"),row.get("total_latency_ms"),row.get("completed_at"),payload,row.get("session_id"),row.get("tenant"),row.get("scene"),row.get("total_ms"),row.get("cost"),row.get("failed"),row.get("low_score"),row.get("created_at")))
    counts[f"traces:{path.name}"] = len(source)


def reset_sequences(pg):
    for table in ("conversation_history","ratings","reactions","feedback","bad_cases","prompt_template","prompt_version","prompt_release","prompt_run","eval_report"):
        pg.execute(f"SELECT setval(pg_get_serial_sequence('{table}','id'), GREATEST(COALESCE((SELECT MAX(id) FROM {table}),1),1), true)")


def main():
    init_runtime_schema()
    counts = {}
    pg = connect()
    try:
        import_core(pg, ROOT / "user_memory.db", counts)
        import_p4(pg, ROOT / "data" / "p4_self_improve.db", counts)
        import_traces(pg, ROOT / "data" / "trace.db", counts)
        import_traces(pg, ROOT / "agent" / "trace.db", counts)
        reset_sequences(pg)
        pg.commit()
    except Exception:
        pg.rollback()
        raise
    finally:
        pg.close()
    print(counts)


if __name__ == "__main__":
    main()
