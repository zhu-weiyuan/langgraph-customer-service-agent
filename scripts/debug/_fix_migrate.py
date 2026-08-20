"""Migrate u: prefixed sessions to bare user_id."""
from agent import memory as m
import sqlite3

conn = m._get_connection()
try:
    rows = conn.execute(
        "SELECT user_id, session_id, title FROM sessions WHERE user_id LIKE ?",
        ("u:%",)
    ).fetchall()
    print(f"Found {len(rows)} sessions with 'u:' prefix")
    for r in rows:
        bare = r["user_id"][2:]  # remove "u:"
        sid = r["session_id"]
        print(f"  {r['user_id']} -> {bare}  (session={sid[:30]}...)")
        conn.execute("UPDATE sessions SET user_id = ? WHERE session_id = ?", (bare, sid))
    conn.commit()
    print("Migration OK")
finally:
    conn.close()
