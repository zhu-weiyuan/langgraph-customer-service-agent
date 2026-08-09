# -*- coding: utf-8 -*-
"""Shared PostgreSQL runtime database helpers.

All live application state uses this connection layer. SQLite files are treated
only as one-time migration sources/backups and are never opened by the server.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

try:
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - minimal test environments
    ConnectionPool = None  # type: ignore[assignment,misc]

ROOT = Path(__file__).resolve().parents[1]
_pool_lock = threading.Lock()
_pools: dict[bool, Any] = {}


def database_url() -> str:
    dsn = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN") or
           os.getenv("PG_DSN") or os.getenv("RAG_PG_DSN"))
    if not dsn:
        raise RuntimeError(
            "PostgreSQL is required: set DATABASE_URL or POSTGRES_DSN")
    return dsn


def _pool_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _pool_timeout() -> float:
    try:
        return max(0.1, float(os.getenv("PG_POOL_TIMEOUT_SECONDS", "5")))
    except (TypeError, ValueError):
        return 5.0


def _get_pool(*, autocommit: bool, connect_timeout: int) -> Any:
    """Return a process-local pool, creating it lazily.

    The pool is deliberately kept behind the existing ``connect()`` API so
    older synchronous modules continue to work.  With psycopg 3.3's
    ``close_returns`` behavior, callers can keep their existing ``conn.close``
    cleanup and the checked-out connection is returned to the pool.
    """
    if ConnectionPool is None:
        return None
    with _pool_lock:
        pool = _pools.get(autocommit)
        if pool is not None:
            return pool

        min_size = _pool_int("PG_POOL_MIN_SIZE", 2)
        max_size = max(min_size, _pool_int("PG_POOL_MAX_SIZE", 20))
        pool_name = "autocommit" if autocommit else "transactional"
        pool = ConnectionPool(
            conninfo=database_url(),
            kwargs={
                "autocommit": autocommit,
                "row_factory": dict_row,
                "connect_timeout": connect_timeout,
            },
            min_size=min_size,
            max_size=max_size,
            timeout=_pool_timeout(),
            close_returns=True,
            open=False,
            name=f"runtime-db-{pool_name}",
        )
        try:
            pool.open(wait=True, timeout=_pool_timeout())
        except Exception:
            pool.close()
            raise
        _pools[autocommit] = pool
        return pool


def connect(*, autocommit: bool = False,
            connect_timeout: int | None = None) -> psycopg.Connection:
    """Open a PostgreSQL connection with a bounded connect timeout.

    When *connect_timeout* is ``None`` the value is read from the
    ``PG_CONNECT_TIMEOUT`` environment variable, falling back to **5
    seconds**.  A bounded timeout prevents both test suites and production
    startup from hanging indefinitely when PostgreSQL is unreachable.
    """
    if connect_timeout is None:
        connect_timeout = int(
            os.getenv("PG_CONNECT_TIMEOUT", "5"))
    pool = _get_pool(autocommit=autocommit, connect_timeout=connect_timeout)
    if pool is not None:
        return pool.getconn(timeout=_pool_timeout())
    return psycopg.connect(
        database_url(), autocommit=autocommit,
        row_factory=dict_row,
        connect_timeout=connect_timeout)


def close_pools() -> None:
    """Close all runtime pools during process shutdown."""
    with _pool_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:
            pass


def pool_stats() -> dict[str, Any]:
    """Expose small, non-sensitive pool diagnostics for local operations."""
    result: dict[str, Any] = {}
    with _pool_lock:
        for autocommit, pool in _pools.items():
            try:
                result["autocommit" if autocommit else "transactional"] = {
                    "open": not bool(getattr(pool, "closed", False)),
                    "size": int(getattr(pool, "_nconns", 0)),
                    "max_size": int(getattr(pool, "_max_size", 0)),
                    "waiting": len(getattr(pool, "_waiting", ())),
                }
            except Exception:
                result["autocommit" if autocommit else "transactional"] = {
                    "open": True,
                }
    return result


@contextmanager
def connection(*, autocommit: bool = False,
             connect_timeout: int | None = None) -> Iterator[psycopg.Connection]:
    conn = connect(autocommit=autocommit, connect_timeout=connect_timeout)
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
        ROOT / "migrations" / "004_refresh_tokens.sql",
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
