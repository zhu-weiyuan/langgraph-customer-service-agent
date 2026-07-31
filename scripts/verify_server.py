# -*- coding: utf-8 -*-
"""Check server after restart"""
import sys, json, urllib.request
BASE = 'http://localhost:7860'

def req(method, path, data=None, headers={}):
    body = json.dumps(data).encode() if data else None
    if data: headers['Content-Type'] = 'application/json'
    r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:200]}

# 1. Health check
print("=== 1. Server health ===")
h = req('GET', '/healthz')
print(f"  /healthz: {h}")

h2 = req('GET', '/api/health')
print(f"  /api/health: {str(h2)[:200]}")

# 2. Login
print("\n=== 2. Login ===")
login = req('POST', '/api/auth/login', {"username":"zwy","password":"test123"})
print(f"  user_id: {login.get('user_id')}")
token = login.get('access_token')
if token:
    print(f"  token: {token[:30]}...")
else:
    print(f"  error: {login.get('_error')} - {login.get('_body')}")
    # Try register
    reg = req('POST', '/api/auth/register', {"username":"zwy","password":"test123","tenant_id":"default"})
    print(f"  register: {reg.get('ok')} token={reg.get('access_token','')[:30] if reg.get('access_token') else 'None'}")
    token = reg.get('access_token')

# 3. Get memories
if token:
    print("\n=== 3. GET /api/memory ===")
    mem = req('GET', '/api/memory?user_id=zwy', headers={'Authorization': f'Bearer {token}'})
    items = mem.get('memories', [])
    print(f"  memories: {len(items)} items")
    for m in items:
        icon = {'preference':'❤️','issue':'⚠️','fact':'📌'}.get(m.get('kind',''),'📄')
        print(f"  {icon} [{m['kind']}] {m['content'][:60]} ({m['importance']*100:.0f}%)")
else:
    print("\n=== 3. Try register + memories ===")
    reg = req('POST', '/api/auth/register', {"username":"zwy","password":"test123","tenant_id":"default"})
    print(f"  register: {reg}")
    token = reg.get('access_token')
    if token:
        mem = req('GET', '/api/memory', headers={'Authorization': f'Bearer {token}'})
        print(f"  memories: {str(mem)[:300]}")

# 4. Test with X-User-Id
print("\n=== 4. Test with X-User-Id header ===")
mem2 = req('GET', '/api/memory', headers={'X-User-Id': 'zwy'})
print(f"  result: {str(mem2)[:200]}")
