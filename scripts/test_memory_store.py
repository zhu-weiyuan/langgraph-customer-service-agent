# -*- coding: utf-8 -*-
"""Fix and test memory store connection"""
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)

# Check what get_memory_store does
os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'
os.environ['RAG_BACKEND'] = 'pgvector'

from agent.user_memory import get_memory_store, MemoryStore

# Test 1: Direct DB connection
import psycopg
conn = psycopg.connect(os.environ['DATABASE_URL'], autocommit=True)
cnt = conn.execute("SELECT COUNT(*) FROM user_memories WHERE user_id='zwy' AND is_deleted=FALSE").fetchone()[0]
print(f"Direct DB: {cnt} memories for zwy")
conn.close()

# Test 2: get_memory_store
print("\nTesting get_memory_store...")
try:
    store = get_memory_store()
    print(f"Store type: {type(store).__name__}")
    items = store.list_memories('zwy')
    print(f"list_memories: {len(items)} items")
    for m in items:
        print(f"  [{m['kind']}] {m['content'][:60]} ({m['importance']*100:.0f}%)")
except Exception as e:
    print(f"Error: {e}")

# Test 3: DB pool
print("\nTesting direct psycopg pool...")
import psycopg_pool
pool = psycopg_pool.ConnectionPool(os.environ['DATABASE_URL'], min_size=1, max_size=5)
with pool.connection() as conn:
    cnt = conn.execute("SELECT 1").fetchone()
    print(f"Pool OK: {cnt}")
pool.close()
print("Pool closed")
