# -*- coding: utf-8 -*-
"""Full test: login, send a memo-worthy message, check memory extracted"""
import sys, json, urllib.request
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://localhost:7860'

def req(method, path, data=None, headers=None):
    hdrs = {'Content-Type': 'application/json'} if data else {}
    if headers: hdrs.update(headers)
    body = json.dumps(data).encode() if data else None
    r = urllib.request.Request(BASE + path, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:300]}

# 1. Login
print("=== 1. Login as zwy ===")
login = req('POST', '/api/auth/login', {"username":"zwy","password":"test123"})
token = login.get('access_token')
print(f"  user_id: {login.get('user_id')}, token: {token[:20] if token else 'NONE'}...")

if not token:
    print("  Registering...")
    reg = req('POST', '/api/auth/register', {"username":"zwy","password":"test123","tenant_id":"default"})
    token = reg.get('access_token')
    print(f"  Token: {token[:20] if token else 'NONE'}...")

headers = {'Authorization': f'Bearer {token}'}

# 2. Check current memories before sending message
print("\n=== 2. Memories BEFORE chat ===")
mem_before = req('GET', '/api/memory', headers=headers)
before_count = len(mem_before.get('memories', []))
print(f"  Count: {before_count}")

# 3. Send a test message with memorable content (issue)
print("\n=== 3. Send test message via chat ===")
chat = req('POST', '/api/chat', {
    "session_id": "zwy_mem_test_001",
    "human_input": "我的X-100坏了，连不上WiFi，很生气！已经第三次了，我要退货！",
    "user_id": "zwy"
}, headers=headers)
print(f"  Reply: {str(chat).get('reply','')[:100] if isinstance(chat,dict) else str(chat)[:200]}")
# Check if there was an error
if '_error' in chat:
    print(f"  Chat ERROR: {chat.get('_error')}: {chat.get('_body')}")

# 4. Send a preference message
print("\n=== 4. Send preference message ===")
chat2 = req('POST', '/api/chat', {
    "session_id": "zwy_mem_test_001",
    "human_input": "我很喜欢你们新的X-300 Pro，打算买一个",
    "user_id": "zwy"
}, headers=headers)
print(f"  Reply: {str(chat2)[:200]}")

# 5. Check memories after chat
print("\n=== 5. Memories AFTER chat ===")
mem_after = req('GET', '/api/memory', headers=headers)
after_count = len(mem_after.get('memories', []))
print(f"  Count: {after_count} (before: {before_count})")
new_count = after_count - before_count
if new_count > 0:
    print(f"  Added {new_count} new memories!")
    for m in mem_after['memories']:
        print(f"    [{m['kind']}] {m['content'][:60]} ({m['importance']*100:.0f}%)")
else:
    print("  No new memories extracted. Checking all current memories:")
    for m in mem_after.get('memories', []):
        print(f"    [{m['kind']}] {m['content'][:60]}")

# 6. Show what the frontend will display
print(f"\n=== 6. Frontend will display {after_count} memories ===")
print("Server running on http://localhost:7860")
print("User: zwy")
