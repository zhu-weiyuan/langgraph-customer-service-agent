# -*- coding: utf-8 -*-
"""Check server process env vars"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')

# Read app_fastapi.py to find the _pg_store method usage pattern
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)

src = open(os.path.join(base, 'agent', 'user_memory.py'), 'r', encoding='utf-8').read()

# Check _pg_store and list_memories to see connection management
in_method = False
for i, line in enumerate(src.split('\n'), 1):
    s = line.strip()
    if 'def _pg_store' in s:
        in_method = True
    if in_method:
        print(f'L{i}: {s[:150]}')
    if in_method and ('def ' in s and '_pg_store' not in s) and i > 10:
        in_method = False

# Also check list_memories
print("\n=== list_memories ===")
in_method = False
for i, line in enumerate(src.split('\n'), 1):
    s = line.strip()
    if 'def list_memories' in s:
        in_method = True
    if in_method:
        print(f'L{i}: {s[:150]}')
    if in_method and ('def ' in s and 'list_memories' not in s) and i > 5:
        in_method = False
