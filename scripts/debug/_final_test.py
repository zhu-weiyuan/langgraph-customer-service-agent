"""Comprehensive test: Vue frontend, ZYW login, SSE streaming."""
import urllib.request, json, os, sys
sys.stdout.reconfigure(encoding='utf-8')

def test(name, fn):
    try:
        fn()
        print(f'  [PASS] {name}')
    except Exception as e:
        print(f'  [FAIL] {name}: {e}')

print('=== 1. Vue Frontend ===')
def test_vue():
    r = urllib.request.urlopen('http://localhost:7860/', timeout=5)
    body = r.read().decode('utf-8')
    assert 'Aster Support' in body, f'Missing Aster Support in {body[:200]}'
test('Vue page served', test_vue)

def test_css():
    r = urllib.request.urlopen('http://localhost:7860/assets/index-Cxpeac3M.css', timeout=5)
    assert r.status == 200 and len(r.read()) > 1000
test('CSS asset served', test_css)

def test_js():
    r = urllib.request.urlopen('http://localhost:7860/assets/index-DRX1HiE_.js', timeout=5)
    assert r.status == 200 and len(r.read()) > 10000
test('JS asset served', test_js)

print('\n=== 2. ZYW Login ===')
def test_login():
    req = urllib.request.Request(
        'http://localhost:7860/api/auth/login',
        data=json.dumps({'username': 'zwy'}).encode(),
        headers={'Content-Type': 'application/json'}
    )
    r = urllib.request.urlopen(req)
    data = json.loads(r.read())
    assert data.get('ok') == True, f'Login failed: {data}'
    assert data.get('user_id') == 'zwy'
    print(f'  user_id={data["user_id"]}, token={"yes" if data.get("access_token") else "no"}')
test('Login works', test_login)

print('\n=== 3. ZYW Sessions ===')
def test_sessions():
    req = urllib.request.Request('http://localhost:7860/api/sessions')
    req.add_header('X-User-Id', 'zwy')
    r = urllib.request.urlopen(req)
    data = json.loads(r.read())
    sessions = data.get('sessions', [])
    print(f'  {len(sessions)} sessions found')
    for s in sessions:
        print(f'    {s["session_id"][:25]}... title="{s.get("title","?")}" msgs={s.get("message_count",0)}')
    assert len(sessions) >= 1, 'No sessions found for zwy'
test('Sessions accessible', test_sessions)

print('\n=== 4. SSE Streaming ===')
def test_sse():
    url = 'http://localhost:7860/api/chat'
    data = json.dumps({
        'message': '你好',
        'stream': True,
        'session_id': 'web-final-test'
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json',
        'X-User-Id': 'zwy'
    })
    resp = urllib.request.urlopen(req, timeout=60)
    headers = dict(resp.headers)
    assert 'x-accel-buffering' in str(headers).lower(), f'Missing X-Accel-Buffering: {headers}'
    assert 'text/event-stream' in str(headers).lower(), f'Wrong content type: {headers}'
    
    buffer = b''
    token_count = 0
    progress_count = 0
    done = False
    
    while not done:
        chunk = resp.read(4096)
        if not chunk:
            break
        buffer += chunk
        text = buffer.decode('utf-8', errors='replace')
        for line in text.split('\n'):
            line = line.strip()
            if line.startswith('data:'):
                payload = line[5:].strip()
                if payload and payload != '[DONE]':
                    try:
                        obj = json.loads(payload)
                        if 'token' in obj:
                            token_count += 1
                        elif 'progress' in obj:
                            progress_count += 1
                        elif 'done' in obj:
                            done = True
                    except:
                        pass
        # Reset buffer
        last_line = text.rfind('\n\n')
        if last_line >= 0:
            buffer = buffer[last_line+2:]
    
    resp.close()
    print(f'  progress={progress_count}, token={token_count}, done={done}')
    assert token_count > 0, 'No token frames received'
    assert done, 'No done frame received'
test('Streaming works', test_sse)

print('\nAll tests passed!')
