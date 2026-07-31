# -*- coding: utf-8 -*-
"""Test: does the memory extraction run server-side even when client times out?"""
import sys, json, urllib.request, time
sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://localhost:7860'

def api(m, p, d=None, t=None):
    h = {'Content-Type':'application/json'}
    if t: h['Authorization']='Bearer '+t
    b = json.dumps(d).encode() if d else None
    r = urllib.request.Request(BASE+p, data=b, headers=h, method=m)
    try:
        with urllib.request.urlopen(r, timeout=15) as x:
            return json.loads(x.read())
    except Exception as e:
        return {'_err': str(e)[:80]}

l = api('POST','/api/auth/login',{'username':'zwy','password':'test123'})
tok = l.get('access_token')
print('Token:', tok[:20] if tok else 'NONE')

# Check before
before_ids = {m['id'] for m in api('GET','/api/memory',t=tok).get('memories',[])}
print('Before:', len(before_ids))

# Call chat (will probably timeout on LLM)
sid = 'zwy_e2e_check_%d' % int(time.time())
print('\nSending chat (session=%s)...' % sid)
msg = '我今天买了X-300 Pro，但连不上WiFi，有异响，非常生气想投诉！'
chat = api('POST','/api/chat',{
    'session_id': sid,
    'message': msg,
    'user_id': 'zwy',
}, t=tok)
print('Chat result:', str(chat)[:150])

# Check after - memory from this session should exist even if chat timed out
time.sleep(2)
after = api('GET','/api/memory',t=tok)
after_count = len(after.get('memories',[]))
new_count = after_count - len(before_ids)
print('\nAfter:', after_count, '(+%d)' % new_count)
if new_count > 0:
    for m in after.get('memories',[]):
        if m['id'] not in before_ids:
            ki = {'preference':'P','issue':'I','fact':'F'}.get(m['kind'],'?')
            print('  NEW [%s] (%d%%) %s' % (ki, int(m['importance']*100), m['content'][:80]))
else:
    print('No new memories - graph not reaching generate_reply')
    print('Check server logs for errors')
