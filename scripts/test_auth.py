# -*- coding: utf-8 -*-
"""Test memory API with different auth methods"""
import sys, os, json, urllib.request
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)

BASE = 'http://localhost:7860'

def try_req(path, headers={}):
    req = urllib.request.Request(BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            return f"✅ {r.status} → {json.dumps(data, ensure_ascii=False, indent=2)[:300]}"
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:100]
        return f"❌ {e.code}: {body}"
    except Exception as e:
        return f"⚠️  {type(e).__name__}: {e}"

# 1. Try auth/me endpoint
print("=== 1. Try /api/auth/me (no auth) ===")
print(try_req('/api/auth/me'))

# 2. Try login
print("\n=== 2. Try /api/auth/login with zwy ===")
import psycopg
c = psycopg.connect(os.environ.get('DATABASE_URL','postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'), autocommit=True)
user = c.execute("SELECT * FROM users WHERE username='zwy'").fetchone()
if user:
    print(f"User exists: {dict(user)}")
    login = urllib.request.Request(BASE + '/api/auth/login',
        data=json.dumps({"username":"zwy","password":"?"}).encode(),
        headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(login, timeout=5) as r:
            print(f"Login: {r.read().decode()[:200]}")
    except urllib.error.HTTPError as e:
        print(f"Login fail: {e.code} {e.read().decode()[:200]}")
else:
    print("User 'zwy' not found in users table, trying register...")
    reg = urllib.request.Request(BASE + '/api/auth/register',
        data=json.dumps({"username":"zwy","password":"test123","tenant_id":"default"}).encode(),
        headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(reg, timeout=5) as r:
            d = json.loads(r.read())
            print(f"Register: {json.dumps(d, ensure_ascii=False, indent=2)[:300]}")
            if d.get('access_token'):
                tok = d['access_token']
                print(f"\n=== 3. Try /api/memory with Bearer token ===")
                print(try_req('/api/memory?user_id=zwy', {'Authorization': f'Bearer {tok}'}))
    except urllib.error.HTTPError as e:
        print(f"Register fail: {e.code} {e.read().decode()[:200]}")

# 4. Try with X-User-Id header
print("\n=== 4. Try /api/memory with X-User-Id header ===")
print(try_req('/api/memory', {'X-User-Id': 'zwy'}))

c.close()
