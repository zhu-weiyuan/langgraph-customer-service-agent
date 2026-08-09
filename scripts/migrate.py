# -*- coding: utf-8 -*-
"""
Minimal SQL migration runner for PostgreSQL.

Usage:
    python scripts/migrate.py                 # apply pending migrations
    python scripts/migrate.py --baseline      # mark all current files as applied (no execution)
    python scripts/migrate.py --dry-run       # list what would run, execute nothing
    python scripts/migrate.py --dsn postgresql://user:pass@host:5432/db

How it works:
  - Every file in migrations/*.sql is a migration, tracked by full filename
    in the `schema_migrations` table (so 001_hybrid_rag.sql and
    001_pgvector_knowledge.sql coexist without collision).
  - New environments: run `python scripts/migrate.py` once — applies everything.
  - Existing environments (manually migrated): run `--baseline` once, then
    future migrations apply incrementally.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def _dsn(args) -> str:
    if args.dsn:
        return args.dsn
    for key in ("DATABASE_URL", "POSTGRES_DSN", "PG_DSN", "RAG_PG_DSN"):
        val = os.getenv(key)
        if val:
            return val
    raise SystemExit("No DSN found. Set DATABASE_URL/POSTGRES_DSN/PG_DSN or pass --dsn.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", action="store_true", help="mark existing migrations as applied without running them")
    ap.add_argument("--dry-run", action="store_true", help="show plan only")
    ap.add_argument("--dsn", default=None, help="PostgreSQL DSN (overrides .env)")
    args = ap.parse_args()

    import psycopg

    dsn = _dsn(args)
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise SystemExit("No .sql files under migrations/")

    if args.dry_run:
        print(f"[dry-run] DSN host ok, {len(files)} migration files found:")
        for f in files:
            print(f"  - {f.name}")
        print("Connect with --dsn to see pending status.")
        return

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

        if args.baseline:
            with conn.cursor() as cur:
                for f in files:
                    if f.name not in applied:
                        cur.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING",
                            (f.name,),
                        )
                        print(f"[baseline] {f.name}")
            conn.commit()
            print(f"Baseline done: {len(files)} migrations recorded.")
            return

        pending = [f for f in files if f.name not in applied]
        if not pending:
            print("No pending migrations — schema is up to date.")
            return

        for f in pending:
            sql = f.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                print(f"[apply] {f.name} ...", end="", flush=True)
                try:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING",
                        (f.name,),
                    )
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    print(f" FAILED: {exc}")
                    print(f"  -> fix the SQL or mark it applied with --baseline; file: {f}")
                    raise SystemExit(1)
            conn.commit()
            print(" ok")

        print(f"Applied {len(pending)} migration(s).")


if __name__ == "__main__":
    main()
