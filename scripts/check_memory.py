# -*- coding: utf-8 -*-
"""Check if build_memory_context works and create multi-turn test data"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)
os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'

# Check memory context
from agent.memory import build_memory_context as bmc, save_profile, _update_product_interests
import json

print("Testing build_memory_context for 'zwy'...")
ctx = bmc('zwy')
print("Memory context:", repr(ctx[:500]) if ctx else "(empty)")
print()

# Check user_preferences
from agent.memory import get_connection
with get_connection() as conn:
    prefs = conn.execute(
        "SELECT DISTINCT user_id, product_interests, known_issues FROM user_preferences WHERE user_id='zwy'"
    ).fetchall()
    print("User preferences for zwy:")
    for p in prefs:
        print("  product_interests:", p['product_interests'])
        print("  known_issues:", p['known_issues'])
    
    # Check conversation count
    cnt = conn.execute("SELECT COUNT(*) FROM conversation_history WHERE user_id='zwy'").fetchone()[0]
    print("\nTotal conversations for zwy:", cnt)
    
    # Check distinct session IDs
    sessions = conn.execute(
        "SELECT session_id, message_count FROM sessions WHERE user_id='zwy' ORDER BY session_id"
    ).fetchall()
    print("\nSessions (%d):" % len(sessions))
    for s in sessions[:10]:
        print("  %s (%d msgs)" % (s['session_id'], s['message_count']))
    if len(sessions) > 10:
        print("  ... and %d more" % (len(sessions) - 10))
