# -*- coding: utf-8 -*-
import urllib.request, json

req = urllib.request.Request(
    'http://localhost:7860/api/chat',
    data=json.dumps({'message': 'hello', 'session_id': 't1'}).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
resp = urllib.request.urlopen(req, timeout=120)
d = json.loads(resp.read())

with open('api_response.json', 'w', encoding='utf-8') as f:
    json.dump(d, f, ensure_ascii=False, indent=2)

print("Keys:", list(d.keys()))
if 'replies' in d:
    for i, r in enumerate(d['replies']):
        print(f"Reply {i}: type={r.get('type')}, content[:100]={r.get('content','')[:100]}")
else:
    print("No 'replies' key! Response:", json.dumps(d, ensure_ascii=False)[:300])
