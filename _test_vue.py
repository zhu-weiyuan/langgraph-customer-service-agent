"""Test the Vue frontend is served correctly."""
import urllib.request

# Check root page
resp = urllib.request.urlopen('http://localhost:7860/', timeout=5)
body = resp.read().decode('utf-8')
print(f'Root status: {resp.status}, Content-Type: {resp.headers.get("Content-Type")}')

if 'Aster Support' in body:
    print('OK: Vue frontend page detected (Aster Support)')
elif '智能客服' in body:
    print('OK: Legacy template is still served')
else:
    print(f'Body snippet: {body[:300]}')

# Check if script/assets references are correct
import re
scripts = re.findall(r'src="([^"]+)"', body)
for s in scripts:
    print(f'  Script reference: {s}')

# Check asset exists
for s in scripts:
    if s.startswith('/assets/'):
        try:
            resp = urllib.request.urlopen(f'http://localhost:7860{s}', timeout=5)
            print(f'  {s}: OK ({len(resp.read())} bytes)')
        except Exception as e:
            print(f'  {s}: FAIL - {e}')

# Check that /api/sessions works with X-User-Id
req = urllib.request.Request('http://localhost:7860/api/sessions')
req.add_header('X-User-Id', 'zwy')
resp = urllib.request.urlopen(req, timeout=5)
import json
data = json.loads(resp.read())
print(f'\nSessions for zwy: {len(data.get("sessions", []))} sessions')
for s in data['sessions']:
    print(f'  {s["session_id"][:20]}... title="{s.get("title","?")}" msgs={s.get("message_count",0)}')
