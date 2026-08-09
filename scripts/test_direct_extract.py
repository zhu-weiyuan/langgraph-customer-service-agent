# -*- coding: utf-8 -*-
"""Test memory extraction directly (no LLM needed)"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'
os.environ['RAG_BACKEND'] = 'pgvector'

from agent.user_memory import extract_from_message, get_memory_store
import hashlib, uuid, math
from datetime import datetime, timezone

USER = 'zwy'

# 1. Check current count
store = get_memory_store()
before = store.list_memories(USER)
print('Memories before: %d' % len(before))

# 2. Test extraction with various messages
test_messages = [
    # Issues - should be detected as 'issue'
    '我买X-100坏了，连不上WiFi，有异响，想退货',
    '三个月坏三次了，真的很生气，你们的质检怎么回事？',
    '投诉你们产品质量，我要找工商局！',
    # Preferences - should be detected as 'preference'
    '我很喜欢你们X-300 Pro，打算买一个',
    '希望能对接米家智能家居和格力空调',
    '我朋友用了觉得不错，推荐我来买',
    # Ordinary - should NOT be stored (no interesting pattern)
    '今天天气不错',
    '你好，我想问一下',
    # Hypothetical - should NOT be stored
    '我可能以后会买吧',
    '如果质量好我考虑推荐给别人',
]

results = []
for msg in test_messages:
    result = extract_from_message(
        user_id=USER,
        user_message=msg,
        source_session='zwy_mem_direct_test',
    )
    stored = result.get('stored', [])
    reason = result.get('reason', 'stored')
    results.append((msg[:50], len(stored), result.get('kind', '?'), reason))

# 3. Show results
print('\n=== Extraction Results ===')
for msg, count, kind, reason in results:
    if count > 0:
        print('  STORED [%s]: %s' % (kind, msg))
    else:
        print('  SKIPPED (%s): %s' % (reason, msg))

# 4. Check final count
after = store.list_memories(USER)
print('\nMemories after: %d (+%d)' % (len(after), len(after)-len(before)))

# Show the NEW ones
before_ids = {m['id'] for m in before}
for m in after:
    if m['id'] not in before_ids:
        ki = {'preference':'P','issue':'I','fact':'F'}.get(m['kind'],'?')
        print('  NEW [%s] (%d%%) %s' % (ki, int(m['importance']*100), m['content'][:80]))
