# -*- coding: utf-8 -*-
"""Shared PostgreSQL runtime database helpers.

All live application state uses this connection layer. SQLite files are treated
only as one-time migration sources/backups and are never opened by the server.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]


def database_url() -> str:
    dsn = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN") or
           os.getenv("PG_DSN") or os.getenv("RAG_PG_DSN"))
    if not dsn:
        raise RuntimeError(
            "PostgreSQL is required: set DATABASE_URL or POSTGRES_DSN")
    return dsn


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(database_url(), autocommit=autocommit,
                           row_factory=dict_row)


@contextmanager
def connection(*, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = connect(autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def table_exists(conn: psycopg.Connection, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS name", (f"public.{table}",)).fetchone()
    return bool(row and row["name"])


def is_postgres_available() -> bool:
    """Check whether any PostgreSQL DSN environment variable is set."""
    return bool(os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN") or
                os.getenv("PG_DSN") or os.getenv("RAG_PG_DSN"))


def init_runtime_schema() -> None:
    """Apply idempotent PostgreSQL/pgvector runtime migrations.

    Safe to call without a configured PostgreSQL URL: the function becomes a
    no-op when no DSN environment variable is set. This enables hermetic unit
    tests that never need a running database.
    """
    if not is_postgres_available():
        return
    migration_files = [
        ROOT / "migrations" / "001_hybrid_rag.sql",
        ROOT / "migrations" / "002_runtime_postgres.sql",
        ROOT / "migrations" / "003_complete_postgres_runtime.sql",
    ]
    with connection() as conn:
        for path in migration_files:
            if path.exists():
                conn.execute(path.read_text(encoding="utf-8"))


def json_value(value: Any, default: Any) -> Any:
    """Normalize psycopg JSONB values and legacy JSON strings."""
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    import json
    try:
        return json.loads(value)
    except Exception:
        return default
