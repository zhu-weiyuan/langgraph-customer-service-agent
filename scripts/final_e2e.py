# -*- coding: utf-8 -*-
"""Full E2E test - send chat, verify memory extraction"""
import sys, json, urllib.request, time
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://localhost:7860'

def api(method, path, data=None, token=None, timeout=30):
    hdrs = {'Content-Type': 'application/json'}
    if token: hdrs['Authorization'] = 'Bearer ' + token
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(BASE + path, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        return {'_err': e.code, '_body': body}
    except Exception as e:
        return {'_err': type(e).__name__, '_msg': str(e)[:100]}

# 1. Health check
print('=== Health ===')
h = api('GET', '/api/health', timeout=5)
llm_ok = h.get('llm', {}).get('reachable', False)
print('LLM reachable:', llm_ok, '| DB:', h.get('database', '?'))

# 2. Login
l = api('POST', '/api/auth/login', {'username':'zwy','password':'test123'}, timeout=5)
tok = l.get('access_token')
print('Token:', tok[:20] if tok else 'NONE')

# 3. Count memories before
before = api('GET', '/api/memory', token=tok, timeout=5)
before_count = len(before.get('memories', []))
before_ids = {m['id'] for m in before.get('memories', [])}
print('Memories before:', before_count)

# 4. Send chat with memorable content
print('\n--- Sending chat (with memorable content) ---')
msg = '我昨天买了X-100智能音箱，但是连不上WiFi还有异响，我很生气想退货！'
chat = api('POST', '/api/chat', {
    'session_id': 'zwy_e2e_final_1',
    'message': msg,
    'user_id': 'zwy',
}, token=tok, timeout=60)

if '_err' in chat:
    print('Chat timed out (expected if LLM slow):', chat['_err'])
else:
    reply = str(chat)[:200]
    print('Reply:', reply)

# 5. Check memories after
time.sleep(2)
after = api('GET', '/api/memory', token=tok, timeout=5)
after_count = len(after.get('memories', []))
new_count = after_count - before_count
print('\n=== Result ===')
print('Memories after:', after_count, '(+%d)' % new_count)

if new_count > 0:
    for m in after.get('memories', []):
        if m['id'] not in before_ids:
            ki = {'preference':'P','issue':'I','fact':'F'}.get(m['kind'],'?')
            print('  NEW [%s] (%d%%) %s' % (ki, int(m['importance']*100), m['content'][:80]))
else:
    print('No new memories from chat.')
    print('')
    print('Possible causes:')
    print('1. Graph failed before reaching generate_reply node')
    print('2. Session_id conflict (same session reused)')
    print('3. LLM client hung during import/init')
