"""Fix runner.py: split full AIMessage content into token chunks for true SSE streaming."""
content = open('/app/agent/runner.py').read()

# Fix: when messages mode yields an AIMessage (complete response), split it into chunks
old = '''            if mode == "messages":
                # payload: (message_chunk, metadata)
                chunk = payload[0] if isinstance(payload, tuple) else payload
                text = getattr(chunk, "content", None)
                if isinstance(text, str) and text:
                    streamed_any_token = True
                    yield {"token": text}
                continue'''

new = '''            if mode == "messages":
                # payload: (message_chunk, metadata)
                chunk = payload[0] if isinstance(payload, tuple) else payload
                text = getattr(chunk, "content", None)
                if isinstance(text, str) and text:
                    streamed_any_token = True
                    # Split full AIMessage into smaller chunks for real-time SSE
                    for piece in chunk_text(text, size=8):
                        yield {"token": piece}
                continue'''

if old in content:
    content = content.replace(old, new, 1)
    open('/app/agent/runner.py', 'w').write(content)
    print('OK: token chunking applied (size=8 chars per token)')
else:
    print('FAIL: pattern not found in /app/agent/runner.py')
