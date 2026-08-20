import urllib.request, json

# 1) Check sessions with ZYW identity
req = urllib.request.Request('http://localhost:7860/api/sessions')
req.add_header('X-User-Id', 'zwy')
resp = urllib.request.urlopen(req)
data = json.loads(resp.read())
print('=== ZYW Sessions ===')
for s in data.get('sessions', []):
    print(f'  session_id={s["session_id"][:30]}...')
    print(f'    title={s.get("title","?")}  msgs={s.get("message_count",0)}')
    print(f'    created={s.get("created_at","")}')

# 2) Check login API works
req2 = urllib.request.Request(
    'http://localhost:7860/api/auth/login',
    data=json.dumps({'username': 'zwy'}).encode(),
    headers={'Content-Type': 'application/json'}
)
resp2 = urllib.request.urlopen(req2)
data2 = json.loads(resp2.read())
print(f'\n=== ZYW Login ===')
print(f'  user_id={data2.get("user_id")}')
print(f'  access_token={data2.get("access_token","")[:20]}...')
print(f'  ok={data2.get("ok")}')

# 3) Login then fetch sessions with token
req3 = urllib.request.Request('http://localhost:7860/api/sessions')
req3.add_header('Authorization', f'Bearer {data2.get("access_token")}')
resp3 = urllib.request.urlopen(req3)
data3 = json.loads(resp3.read())
print(f'\n=== Sessions via auth token ===')
for s in data3.get('sessions', []):
    print(f'  session_id={s["session_id"][:30]}... title={s.get("title","?")}')
