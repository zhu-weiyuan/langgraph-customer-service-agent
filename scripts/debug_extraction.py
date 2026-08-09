# -*- coding: utf-8 -*-
"""Debug: test if the server's _generate_reply_inner is actually calling extract_from_message"""
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
    except urllib.error.HTTPError as e:
        return {'_err': e.code, '_body': e.read().decode()[:200]}
    except Exception as e:
        return {'_err': str(e)[:80]}

# Login
l = api('POST','/api/auth/login',{'username':'zwy','password':'test123'})
tok = l.get('access_token')
print('Token:', tok[:20] if tok else 'NONE')

# Step 1: Get current memory count
before = api('GET', '/api/memory', t=tok)
bcount = len(before.get('memories',[]))
print('Memories before:', bcount)

# Step 2: Direct DB check - is the extract_from_message function callable?
import os
os.environ['DATABASE_URL']='postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'
os.environ['RAG_BACKEND']='pgvector'
sys.path.insert(0, '.')
from agent.user_memory import extract_from_message
r = extract_from_message('zwy', '我的X-100坏了，连不上WiFi，有异响，我想退货！', 'debug_test')
print('Direct extraction:', r)
time.sleep(1)

# Step 3: Check if memory was added
after = api('GET', '/api/memory', t=tok)
acount = len(after.get('memories',[]))
print('Memories after direct extraction:', acount, '(+%d)' % (acount-bcount))

# Step 4: Now try to trigger via chat API (with quick timeout)
print('\nCalling /api/chat (will timeout on LLM)...')
chat = api('POST', '/api/chat', {
    'session_id': 'zwy_debug_001',
    'message': '我坏了连不上异响退货',
    'user_id': 'zwy',
}, t=tok)
print('Chat result:', str(chat)[:200] if isinstance(chat, dict) else str(chat))
