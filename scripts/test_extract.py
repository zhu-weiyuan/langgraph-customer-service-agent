# -*- coding: utf-8 -*-
"""Test memory extraction via chat API"""
import sys, json, urllib.request, time
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://localhost:7860'

def api(method, path, data=None, token=None):
    hdrs = {'Content-Type': 'application/json'}
    if token: hdrs['Authorization'] = f'Bearer {token}'
    body = json.dumps(data).encode() if data else None
    r = urllib.request.Request(BASE + path, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'_err': e.code, '_body': e.read().decode()[:200]}
    except Exception as e:
        return {'_err': str(e)}

# 1. Login
login = api('POST', '/api/auth/login', {'username':'zwy','password':'test123'})
token = login.get('access_token')
print('Login:', login.get('user_id'), token[:20]+'...' if token else 'FAIL')

# 2. Memories before
mem = api('GET', '/api/memory', token=token)
before = len(mem.get('memories',[]))
print(f'Memories before: {before}')

# 3. Send messages with memorable content
messages = [
    '我买了X-100智能音箱，但是连不上WiFi，而且有异响，想退货',
    '之前换新过一次还是有问题，我真的很生气，三个月坏三次了',
    '我朋友也推荐你们的产品，但我感觉质量不太好',
    '你们新的X-300 Pro看起来不错，我对那个很感兴趣',
    '如果能对接米家智能家居就更好了',
]

for i, msg in enumerate(messages):
    print('--- Message %d: %s... ---' % (i+1, msg[:40]))
    chat = api('POST', '/api/chat', {
        'session_id': 'zwy_mem_extract_test',
        'message': msg,
        'user_id': 'zwy',
    }, token=token)
    if '_err' in chat:
        print('  Error:', str(chat)[:100])
    else:
        reply = str(chat)[:150]
        print('  Reply:', reply)
    time.sleep(1)

# 4. Memories after
mem2 = api('GET', '/api/memory', token=token)
curr = mem2.get('memories', [])
after = len(curr)
print('\n=== Result: %d -> %d memories (+%d) ===' % (before, after, after-before))
for m in curr:
    pct = int(m['importance'] * 100)
    ki = {'preference':'P','issue':'I','fact':'F'}.get(m['kind'],'?')
    print('  [%s] (%2d%%) %s' % (ki, pct, m['content'][:70]))
