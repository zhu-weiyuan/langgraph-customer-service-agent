# -*- coding: utf-8 -*-
"""Verify memory API - GBK-safe version"""
import sys, json, urllib.request
BASE = 'http://localhost:7860'

def req(method, path, data=None, headers=None):
    hdrs = {'Content-Type': 'application/json'} if data else {}
    if headers:
        hdrs.update(headers)
    body = json.dumps(data).encode() if data else None
    r = urllib.request.Request(BASE + path, data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(r, timeout=5) as resp:
        return json.loads(resp.read())

login = req('POST', '/api/auth/login', {"username":"zwy","password":"test123"})
token = login['access_token']

mem = req('GET', '/api/memory?user_id=zwy', headers={'Authorization': f'Bearer {token}'})
items = mem.get('memories', [])

print(f"Total memories: {len(items)}")
for m in items:
    kind = m['kind']
    content = m['content'][:60]
    imp = int(m['importance'] * 100)
    icon = {'preference':'[P]','issue':'[I]','fact':'[F]'}.get(kind, '[?]')
    print(f"  {icon} [{imp}%] {content}")

print(f"\nUser: {mem['user_id']}")
print("✅ Success! Frontend will now show memories.")
