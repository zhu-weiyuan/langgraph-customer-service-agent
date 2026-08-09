"""Test SSE streaming with the new token chunking."""
import urllib.request, json, sys, time

url = 'http://localhost:7860/api/chat'
data = json.dumps({
    'message': '你好，请简单介绍一下你自己',
    'stream': True,
    'session_id': 'web-test-sse-fix'
}).encode()
req = urllib.request.Request(url, data=data, headers={
    'Content-Type': 'application/json',
    'X-User-Id': 'test-sse'
})

start = time.perf_counter()
resp = urllib.request.urlopen(req, timeout=120)
print(f'SSE Status: {resp.status}')
print(f'Headers: {dict(resp.headers)}\n')

buffer = b''
frame_types = {'progress': 0, 'token': 0, 'done': 0, 'error': 0}
total_chars = 0
token_texts = []

try:
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buffer += chunk
        # Parse SSE frames from buffer
        while True:
            line_end = buffer.find(b'\n')
            if line_end < 0:
                break
            line = buffer[:line_end].decode('utf-8').strip()
            buffer = buffer[line_end+1:]
            if line.startswith('data:'):
                payload = line[5:].strip()
                if payload and payload != '[DONE]':
                    try:
                        obj = json.loads(payload)
                        if 'token' in obj:
                            frame_types['token'] += 1
                            t = obj['token']
                            total_chars += len(t)
                            token_texts.append(t)
                            print(f'  TOKEN[{frame_types["token"]}]: {t}', flush=True)
                        elif 'progress' in obj:
                            frame_types['progress'] += 1
                            print(f'  PROGRESS: {obj["progress"]}', flush=True)
                        elif 'done' in obj:
                            frame_types['done'] += 1
                            print(f'  DONE: intent={obj.get("intent","")}', flush=True)
                        elif 'error' in obj:
                            frame_types['error'] += 1
                            print(f'  ERROR: {obj["error"]}', flush=True)
                    except json.JSONDecodeError:
                        pass
except Exception as e:
    print(f'\nRead error: {e}', flush=True)
finally:
    resp.close()

elapsed = time.perf_counter() - start
print(f'\n=== Results ===')
print(f'Time: {elapsed:.2f}s')
print(f'Frames: progress={frame_types["progress"]}, token={frame_types["token"]}, done={frame_types["done"]}, error={frame_types["error"]}')
print(f'Total chars received in tokens: {total_chars}')
if frame_types['token'] > 1:
    avg_chunk = total_chars / frame_types['token']
    print(f'Average token size: {avg_chunk:.1f} chars')
if token_texts:
    full = ''.join(token_texts)
    print(f'Full response: {full[:200]}')
    print(f'Tokens: {token_texts}')
