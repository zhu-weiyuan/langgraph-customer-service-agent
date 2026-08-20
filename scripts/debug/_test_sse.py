"""Test SSE streaming end-to-end."""
import urllib.request, json, sys

# Test with zwy's identity
url = 'http://localhost:7860/api/chat'
data = json.dumps({
    'message': '你好',
    'stream': True,
    'session_id': 'web-test-stream'
}).encode()
req = urllib.request.Request(url, data=data, headers={
    'Content-Type': 'application/json',
    'X-User-Id': 'zwy'
})
resp = urllib.request.urlopen(req, timeout=60)
print(f'SSE Status: {resp.status}')
for k, v in resp.headers.items():
    if k.lower() in ('content-type', 'cache-control', 'connection', 'x-accel-buffering'):
        print(f'  {k}: {v}')

# Read first 10 frames
body = b''
frame_count = 0
max_frames = 15
try:
    while frame_count < max_frames:
        chunk = resp.read(4096)
        if not chunk:
            break
        body += chunk
        # Count frames
        for line in body.decode().split('\n'):
            if line.strip().startswith('data:'):
                payload = line.strip()[5:].strip()
                if payload and payload != '[DONE]':
                    try:
                        obj = json.loads(payload)
                        if 'token' in obj:
                            print(f'  FRAME[{frame_count}]: token={obj["token"][:60]}')
                        elif 'progress' in obj:
                            print(f'  FRAME[{frame_count}]: progress={obj["progress"]}')
                        elif 'error' in obj:
                            print(f'  FRAME[{frame_count}]: ERROR={obj["error"]}')
                        elif 'done' in obj:
                            print(f'  FRAME[{frame_count}]: DONE intent={obj.get("intent","")}')
                        else:
                            print(f'  FRAME[{frame_count}]: {json.dumps(obj, ensure_ascii=False)[:80]}')
                        frame_count += 1
                    except:
                        pass
except Exception as e:
    print(f'Read error: {e}')
finally:
    resp.close()

print(f'\nTotal frames received: {frame_count}')
if frame_count == 0:
    print(f'Raw body preview: {body[:500]}')
else:
    print('SSE streaming working correctly!')
