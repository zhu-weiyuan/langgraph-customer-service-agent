# -*- coding: utf-8 -*-
"""E2E test: login -> send chat -> check memory extracted"""
import sys, json, urllib.request, time
sys.stdout.reconfigure(encoding='utf-8')

BASE = 'http://localhost:7860'

def api(method, path, data=None, token=None):
    hdrs = {'Content-Type': 'application/json'}
    if token: hdrs['Authorization'] = 'Bearer ' + token
    body = json.dumps(data).encode() if data else None
    r = urllib.request.Request(BASE + path, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'_err': e.code, '_body': e.read().decode()[:300]}
    except Exception as e:
        return {'_err': str(e)[:100]}

# 1. Login
print('=== 1. Login ===')
login = api('POST', '/api/auth/login', {'username':'zwy','password':'test123'})
token = login.get('access_token')
print('User:', login.get('user_id'), ' | Token:', token[:20] if token else 'NONE')

# 2. Count memories before
mem_before = api('GET', '/api/memory', token=token)
before_count = len(mem_before.get('memories', []))
print('Memories before:', before_count)

# 3. Check LLM health
print('\n=== 2. LLM Health ===')
health = api('GET', '/api/health', token=token)
print('LLM reachable:', health.get('llm', {}).get('reachable', '?'))

# 4. Send a test message via chat API
print('\n=== 3. Sending Chat Message ===')
chat = api('POST', '/api/chat', {
    'session_id': 'zwy_e2e_mem_test',
    'message': '我的X-100智能音箱坏了，连不上WiFi，有异响！这已经是第三次了，我很生气，想要退货退款！',
    'user_id': 'zwy',
}, token=token)

if '_err' in chat:
    print('Chat error:', chat['_err'], str(chat.get('_body',''))[:150])
    print('(LLM might be slow - check llama.cpp)')
    # Even if chat fails, memory extraction should have worked (it runs before LLM call)
else:
    reply = chat.get('reply', '') or str(chat)[:150]
    print('Bot reply:', reply[:150])

# 5. Check memories after
time.sleep(1)
mem_after = api('GET', '/api/memory', token=token)
after_count = len(mem_after.get('memories', []))
new_count = after_count - before_count
print('\n=== 4. Memories After Chat ===')
print('Count:', after_count, '(+%d)' % new_count)

if new_count > 0:
    before_ids = {m['id'] for m in mem_before.get('memories', [])}
    for m in mem_after.get('memories', []):
        if m['id'] not in before_ids:
            ki = {'preference':'P','issue':'I','fact':'F'}.get(m['kind'],'?')
            print('  NEW [%s] (%d%%) %s' % (ki, int(m['importance']*100), m['content'][:80]))
else:
    print('No new memories were extracted')
    print('(Check if messages contain issue/preference keywords)')
