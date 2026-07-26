"""One-shot, idempotent import of legacy SQLite runtime data into PostgreSQL.

SQLite files are never modified. Run inside customer-service after applying 002:
    python scripts/migrate_sqlite_runtime_to_postgres.py
"""
from __future__ import annotations
import os, sqlite3
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
SQLITE_MEMORY = Path(os.getenv("USER_MEMORY_DB", ROOT / "data" / "user_memory.db"))
SQLITE_TRACE = Path(os.getenv("TRACE_DB_PATH", ROOT / "data" / "trace.db"))
DATABASE_URL = os.environ["DATABASE_URL"]

TABLES = [
    "tenants", "users", "conversations", "conversation_messages", "conversation_history",
    "user_profiles", "user_preferences", "tickets", "ratings", "conversation_feedback",
    "tool_calls", "legacy_history_migrations",
]

def import_db(pg, path: Path, tables: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    if not path.exists():
        return result
    source = sqlite3.connect(path)
    source.row_factory = sqlite3.Row
    for table in tables:
        exists = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            continue
        rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
        if not rows:
            result[table] = 0
            continue
        columns = list(rows[0].keys())
        quoted = ", ".join(f'"{c}"' for c in columns)
        values = ", ".join(["%s"] * len(columns))
        # Runtime IDs/unique keys make re-runs safe; legacy identity-only rows may
        # duplicate but do not affect canonical conversations or analytics semantics.
        statement = f'INSERT INTO "{table}" ({quoted}) VALUES ({values}) ON CONFLICT DO NOTHING'
        with pg.cursor() as cur:
            for row in rows:
                values = []
                for column in columns:
                    value = row[column]
                    # SQLite represents booleans as 0/1; PostgreSQL correctly
                    # stores these runtime fields as BOOLEAN.
                    if table == "conversation_history" and column == "resolved" and value is not None:
                        value = bool(value)
                    values.append(value)
                cur.execute(statement, tuple(values))
        result[table] = len(rows)
    source.close()
    return result

def main() -> None:
    sql = (ROOT / "migrations" / "002_runtime_postgres.sql").read_text(encoding="utf-8")
    with psycopg.connect(DATABASE_URL) as pg:
        with pg.cursor() as cur:
            cur.execute(sql)
        counts = import_db(pg, SQLITE_MEMORY, TABLES)
        trace_counts = import_db(pg, SQLITE_TRACE, ["traces"])
        # Imports preserve legacy numeric IDs; advance PostgreSQL identities so
        # new runtime writes cannot collide with imported rows.
        with pg.cursor() as cur:
            cur.execute("SELECT setval(pg_get_serial_sequence('conversation_history', 'id'), COALESCE((SELECT MAX(id) FROM conversation_history), 1), true)")
            cur.execute("SELECT setval(pg_get_serial_sequence('ratings', 'id'), COALESCE((SELECT MAX(id) FROM ratings), 1), true)")
        pg.commit()
    print({**counts, **trace_counts})

if __name__ == "__main__":
    main()
