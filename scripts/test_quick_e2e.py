# -*- coding: utf-8 -*-
"""Quick memory extraction test - lightweight, no LLM needed"""
import sys, json, urllib.request, time
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://localhost:7860'

def api(method, path, data=None, token=None):
    hdrs = {'Content-Type': 'application/json'}
    if token: hdrs['Authorization'] = 'Bearer ' + token
    body = json.dumps(data).encode() if data else None
    r = urllib.request.Request(BASE + path, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'_err': e.code, '_body': e.read().decode()[:200]}
    except Exception as e:
        return {'_err': str(e)[:100]}

# Login
login = api('POST', '/api/auth/login', {'username':'zwy','password':'test123'})
tok = login.get('access_token')
print('Token:', tok[:20] if tok else 'NONE')

# Before
before = len(api('GET', '/api/memory', token=tok).get('memories', []))
print('Before:', before)

# Test LLM health
h = api('GET', '/api/health', token=tok)
print('LLM:', h.get('llm',{}))

# Send chat (may fail if LLM too slow, but memory extraction runs before LLM call)
print('\nSending chat (memory extraction runs BEFORE LLM)...')
chat = api('POST', '/api/chat', {
    'session_id': 'zwy_e2e_01',
    'message': '我的X-100坏了，连不上WiFi，想退货，很生气',
    'user_id': 'zwy',
}, token=tok)

if '_err' in chat:
    print('Chat status:', chat['_err'], str(chat.get('_body',''))[:100])
    if chat['_err'] == 503:
        print('  (LLM timeout expected - memory extraction already ran!)')
else:
    print('Reply:', str(chat).get('reply','')[:100] if isinstance(chat, dict) else str(chat)[:100])

# After
time.sleep(1)
time.sleep(2)
after_mem = api('GET', '/api/memory', token=tok)
after = len(after_mem.get('memories', []))
print('\nAfter:', after, '(+%d)' % (after - before))

if after > before:
    before_ids = {m['id'] for m in before}
    curr = api('GET', '/api/memory', token=tok).get('memories', [])
    for m in curr:
        if m['id'] not in before_ids:
            print('  NEW: [%s] (%d%%) %s' % (m['kind'], int(m['importance']*100), m['content'][:80]))
else:
    print('No new memories extracted')
