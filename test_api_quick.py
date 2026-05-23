# -*- coding: utf-8 -*-
"""Quick API test for langgraph-customer-service-agent.

Tests the /api/chat endpoint with a simple message and validates the response.
Runs against localhost:7860 — ensure the server is running first.
"""
import sys
import io
import urllib.request
import json

# Ensure UTF-8 output on Windows (prevents UnicodeEncodeError with emoji)
# Skip when running under pytest — its CaptureFixture doesn't have .buffer
if sys.platform == 'win32' and '__pytest' not in sys.modules:
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# Test 1: Health check
print("[Test 1] Health check...")
try:
    resp = urllib.request.urlopen('http://localhost:7860/api/health', timeout=5)
    health = json.loads(resp.read())
    print(f"  Service: {health.get('service', 'unknown')}")
    print(f"  LLM reachable: {health.get('llm', {}).get('reachable', False)}")
    print(f"  DB conversations: {health.get('database', {}).get('conversations', 'N/A')}")
    print(f"  KB documents: {health.get('knowledge_base', {},).get('documents', 'N/A')}")
except Exception as e:
    print(f"  SKIP (server not running): {e}")

# Test 2: Chat endpoint
print("\n[Test 2] Chat endpoint...")
try:
    req = urllib.request.Request(
        'http://localhost:7860/api/chat',
        data=json.dumps({'message': 'hello', 'session_id': 'test_quick'}).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    resp = urllib.request.urlopen(req, timeout=120)
    d = json.loads(resp.read())

    with open('api_response.json', 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

    print(f"  Keys: {list(d.keys())}")
    if 'replies' in d:
        for i, r in enumerate(d['replies']):
            content = r.get('content', '')[:100]
            print(f"  Reply {i}: type={r.get('type')}, content[:100]={content}")
    else:
        print(f"  No 'replies' key! Response: {json.dumps(d, ensure_ascii=False)[:300]}")
except Exception as e:
    print(f"  ERROR: {e}")

# Test 3: Analytics endpoint
print("\n[Test 3] Analytics endpoint...")
try:
    resp = urllib.request.urlopen('http://localhost:7860/api/analytics', timeout=5)
    analytics = json.loads(resp.read())
    print(f"  Total conversations: {analytics.get('total_conversations', 'N/A')}")
    print(f"  Intents: {analytics.get('intents', {})}")
    print(f"  Emotions: {analytics.get('emotions', {})}")
    print(f"  Ratings: {analytics.get('ratings', {})}")
except Exception as e:
    print(f"  ERROR: {e}")

print("\n[Done] All quick tests completed.")
