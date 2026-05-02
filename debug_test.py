# -*- coding: utf-8 -*-
import sys, io
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import urllib.request, json

def test(msg):
    req = urllib.request.Request(
        'http://127.0.0.1:7860/api/chat',
        data=json.dumps({"message": msg, "session_id": "flowtest"}).encode(),
        headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read().decode())
    print(f"\n--- Input: {msg} ---")
    print(f"Intent: {data['intent']}  |  Retries: {data['retry_count']}")
    for r in data['replies']:
        c = r['content'].replace('\n', ' ')[:150]
        print(f"[{r['type']}] {c}")

test("How do I use the smart speaker?")
test("Thanks, that's all!")
test("Satisfied")
