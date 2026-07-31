# -*- coding: utf-8 -*-
"""Read app_fastapi.py to find auth mechanism for memory API"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)

src = open(os.path.join(base, 'app_fastapi.py'), 'r', encoding='utf-8').read()
lines = src.split('\n')

print(f"File: {os.path.join(base, 'app_fastapi.py')} ({len(lines)} lines)")

# Find memory-related and auth-related lines
for i, line in enumerate(lines, 1):
    s = line.strip()
    if any(kw in s.lower() for kw in ['memory', 'auth', 'header', 'bearer', 'token', 'x-api', 'api_key', 'user_id']):
        if s:
            print(f"L{i}: {s[:180]}")

# Also show route definitions
print("\n=== Route definitions ===")
for i, line in enumerate(lines, 1):
    s = line.strip()
    if s.startswith('@router') or s.startswith('@app') or s.startswith('def memory') or s.startswith('async def memory'):
        print(f"L{i}: {s[:180]}")
