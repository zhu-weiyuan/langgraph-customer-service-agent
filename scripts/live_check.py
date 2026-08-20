# -*- coding: utf-8 -*-
"""Live check the API endpoint and database"""
import sys, os, json, urllib.request
sys.stdout.reconfigure(encoding='utf-8')

# 1. Test API
print("=== API /api/memory?user_id=zwy ===")
try:
    url = 'http://localhost:7860/api/memory?user_id=zwy'
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    print(f"Returned {len(data)} items")
    for m in data:
        print(f"  [{m.get('kind','?')}] {m.get('content','')[:70]} ({m.get('importance',0)*100:.0f}%)")
except Exception as e:
    print(f"ERROR: {e}")

# 2. Test DB
print("\n=== Database ===")
os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'
import psycopg
c = psycopg.connect(os.environ['DATABASE_URL'], autocommit=True)
cnt = c.execute("SELECT COUNT(*) FROM user_memories WHERE user_id='zwy' AND is_deleted=FALSE").fetchone()[0]
print(f"user_memories for zwy: {cnt}")

if cnt > 0:
    rows = c.execute("""
        SELECT kind, importance, substring(content,1,60) as c
        FROM user_memories WHERE user_id='zwy' AND is_deleted=FALSE
        ORDER BY importance DESC LIMIT 5
    """).fetchall()
    for r in rows:
        print(f"  [{r[0]}] {r[2]} ({r[1]*100:.0f}%)")
c.close()

# 3. What endpoint does frontend actually call?
print("\n=== Frontend API guess ===")
# Check common patterns
for path in ['/api/memory', '/api/v1/memory', '/api/memories']:
    try:
        url = f'http://localhost:7860{path}?user_id=zwy'
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            d = json.loads(resp.read())
            print(f"  {path} -> {type(d).__name__}, len={len(d) if isinstance(d,(list,dict)) else '?'}")
    except urllib.error.HTTPError as e:
        print(f"  {path} -> HTTP {e.code}")
    except Exception as e:
        print(f"  {path} -> {type(e).__name__}: {e}")
