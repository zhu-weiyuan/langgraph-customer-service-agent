"""Migrate sessions from u: prefix to bare user_id, then apply the app_fastapi fix."""
import subprocess, sys

# Step 1: Check and migrate existing sessions inside container
cmd1 = [
    'docker', 'exec', 'langgraph-cs-agent', 'python3', '-c', '''
from agent import memory
conn = memory._get_connection()
rows = conn.execute("SELECT user_id, session_id, title FROM sessions WHERE user_id LIKE 'u:%'").fetchall()
print(f"Sessions with u: prefix: {len(rows)}")
for r in rows:
    bare = r["user_id"][2:]  # remove "u:" prefix
    old = r["user_id"]
    sid = r["session_id"]
    print(f"  Migrating: {old} -> {bare} (session={sid[:20]}...)")
    conn.execute("UPDATE sessions SET user_id = ? WHERE session_id = ?", (bare, sid))
conn.commit()
conn.close()
print("Migration complete")

# Verify
conn2 = memory._get_connection()
rows2 = conn2.execute("SELECT DISTINCT user_id FROM sessions").fetchall()
print(f"Users after migration: {[r['user_id'] for r in rows2]}")
conn2.close()
'''
]

# Step 2: Update app_fastapi.py in container  
# Need to apply the same fix inside the container
cmd2 = [
    'docker', 'exec', 'langgraph-cs-agent', 'python3', '-c', '''
content = open("/app/app_fastapi.py", encoding="utf-8").read()
old = '        header_uid = (request.headers.get("X-User-Id", "") or "").strip()\\n        if header_uid:\\n            request.state.user_id = f"u:{header_uid}"[:128]'
new = '        header_uid = (request.headers.get("X-User-Id", "") or "").strip()\\n        if header_uid:\\n            request.state.user_id = header_uid[:128]'
if old in content:
    content = content.replace(old, new, 1)
    open("/app/app_fastapi.py", "w", encoding="utf-8").write(content)
    print("OK: app_fastapi.py patched in container")
else:
    print("FAIL: pattern not found")
'''
]

# Run step 1
print("=== Step 1: Migrate sessions ===")
r1 = subprocess.run(cmd1, capture_output=True, text=True)
print(r1.stdout)
if r1.stderr:
    print("STDERR:", r1.stderr[:500])

# Run step 2
print("=== Step 2: Patch app_fastapi.py ===")
r2 = subprocess.run(cmd2, capture_output=True, text=True)
print(r2.stdout)
if r2.stderr:
    print("STDERR:", r2.stderr[:500])
