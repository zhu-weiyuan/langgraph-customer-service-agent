"""Initialize the PostgreSQL runtime schema without requiring the app process."""
import os
from pathlib import Path
import psycopg

root = Path(__file__).resolve().parents[1]
sql = (root / "migrations" / "002_runtime_postgres.sql").read_text(encoding="utf-8")
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
print("runtime PostgreSQL schema initialized")
