# -*- coding: utf-8 -*-
"""Login and test memory API"""
import sys, os, json, urllib.request
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://localhost:7860'

def try_req(path, data=None, headers={}, method=None):
    if data and isinstance(data, dict):
        data = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()[:200]}
    except Exception as e:
        return {"error": type(e).__name__, "detail": str(e)[:200]}

# Try register first (creates user + returns JWT)
print("=== 1. Register zwy ===")
reg = try_req('/api/auth/register', {"username":"zwy","password":"test123","tenant_id":"default"})
print(json.dumps(reg, ensure_ascii=False, indent=2)[:500])

token = reg.get('access_token') or reg.get('token')

if not token:
    print("\n=== 2. Already registered, try login ===")
    login = try_req('/api/auth/login', {"username":"zwy","password":"test123"})
    print(json.dumps(login, ensure_ascii=False, indent=2)[:500])
    token = login.get('access_token') or login.get('token')

if token:
    print(f"\n=== 3. Token: {token[:40]}... ===")
    
    # Now try /api/memory
    print("\n=== 4. GET /api/memory with Bearer token ===")
    mem = try_req('/api/memory?user_id=zwy', headers={'Authorization': f'Bearer {token}'})
    print(json.dumps(mem, ensure_ascii=False, indent=2)[:2000])
    
    # Also try without query param
    print("\n=== 5. GET /api/memory (no user_id param) ===")
    mem2 = try_req('/api/memory', headers={'Authorization': f'Bearer {token}'})
    print(json.dumps(mem2, ensure_ascii=False, indent=2)[:2000])
else:
    print("\nNo token obtained. Trying with X-User-Id header...")
    print("\n=== 3. GET /api/memory with X-User-Id ===")
    mem = try_req('/api/memory?user_id=zwy', headers={'X-User-Id': 'zwy'})
    print(json.dumps(mem, ensure_ascii=False, indent=2)[:2000])
