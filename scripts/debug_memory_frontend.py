# -*- coding: utf-8 -*-
"""查前端记忆 API 和数据"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)
os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'

# 1. 查 main.py 记忆相关 API
print("=== main.py memory-related routes ===")
src = open(os.path.join(base, 'main.py'), 'r', encoding='utf-8').read()
for line in src.split('\n'):
    if 'memory' in line.lower():
        print('  ' + line.strip())

print("\n=== memory.py memory-related functions ===")
from agent import memory
for name in dir(memory):
    if 'memory' in name.lower() or 'get_user' in name.lower() or 'list_mem' in name.lower():
        obj = getattr(memory, name, None)
        if callable(obj):
            import inspect
            sig = inspect.signature(obj) if hasattr(inspect, 'signature') else '(?)'
            print('  %s%s' % (name, sig))

# 2. 查 user_memories 表
import psycopg
c = psycopg.connect(os.environ['DATABASE_URL'], autocommit=True)

cols = c.execute("""
    SELECT column_name, data_type, is_nullable 
    FROM information_schema.columns 
    WHERE table_name='user_memories' 
    ORDER BY ordinal_position
""").fetchall()
print("\n=== user_memories schema ===")
for col in cols:
    print('  %s (%s, nullable=%s)' % (col[0], col[1], col[2]))

# 查数据
rows = c.execute("SELECT * FROM user_memories WHERE user_id='zwy'").fetchall()
print('\n=== user_memories data for zwy (%d rows) ===' % len(rows))
for r in rows:
    d = dict(r)
    for k, v in d.items():
        print('  %s = %s' % (k, repr(v)[:200]))
    print('---')

c.close()

# 3. 查前端代码
print("\n=== Frontend memory-related code ===")
frontend_dir = os.path.join(base, 'frontend', 'src')
if os.path.exists(frontend_dir):
    for root, dirs, files in os.walk(frontend_dir):
        for f in files:
            if f.endswith(('.vue', '.ts', '.js')):
                fp = os.path.join(root, f)
                src = open(fp, 'r', encoding='utf-8', errors='replace').read()
                if 'memory' in src.lower() or '记忆' in src:
                    print('  ' + fp)
                    for line in src.split('\n'):
                        if 'memory' in line.lower() or '记忆' in line or 'memories' in line.lower() or '长期' in line:
                            print('    ' + line.strip()[:120])
