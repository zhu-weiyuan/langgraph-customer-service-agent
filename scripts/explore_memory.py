# -*- coding: utf-8 -*-
"""探索长期记忆系统结构"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)

os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'

from agent.memory import get_connection

with get_connection() as conn:
    # Tables
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' "
        "ORDER BY table_name"
    ).fetchall()
    print("Tables:", [t['table_name'] for t in tables])
    
    for tbl in ['user_memories', 'user_profiles', 'user_preferences', 'conversation_history', 'sessions']:
        cols = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s "
            "ORDER BY ordinal_position", (tbl,)
        ).fetchall()
        print("\n%s columns:" % tbl)
        for c in cols:
            print("  %s (%s)" % (c['column_name'], c['data_type']))
    
    # Check for existing memory data
    for tbl in ['user_memories', 'user_profiles', 'user_preferences']:
        cnt = conn.execute("SELECT COUNT(*) FROM %s" % tbl).fetchone()
        print("\n%s row count: %s" % (tbl, cnt['count']))
        if cnt['count'] > 0:
            sample = conn.execute("SELECT * FROM %s LIMIT 2" % tbl).fetchall()
            for r in sample:
                print("  ", dict(r))

    # Inspect memory functions
    import inspect
    from agent import memory as mem_module
    mem_funcs = []
    for name in dir(mem_module):
        if 'memory' in name.lower() or 'interest' in name.lower() or 'profile' in name.lower():
            obj = getattr(mem_module, name)
            if callable(obj):
                sig = inspect.signature(obj) if hasattr(inspect, 'signature') else '?'
                mem_funcs.append((name, sig))
    print("\nRelevant memory functions:")
    for name, sig in sorted(mem_funcs):
        print("  %s%s" % (name, sig))
