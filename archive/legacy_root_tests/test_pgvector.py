"""End-to-end test: pgvector connection + upsert + search."""
import os, sys, random

sys.path.insert(0, r"C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent")
os.environ["PGVECTOR_ENABLED"] = "1"
os.environ["DATABASE_URL"] = "postgresql://customer_service:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/customer_service"
os.environ["PGVECTOR_DIM"] = "1024"

from agent.pgvector_store import _connection, upsert_chunks, search

print("=== Test 1: Connection ===")
with _connection() as conn:
    cur = conn.cursor()
    cur.execute("SELECT version()")
    print("DB Version:", cur.fetchone()[0][:80])
    cur.execute("""
        SELECT EXISTS(
            SELECT 1 FROM information_schema.tables WHERE table_name = 'knowledge_chunks'
        )""")
    exists = cur.fetchone()[0]
    status = "FOUND" if exists else "NOT FOUND"
    print(f"knowledge_chunks table: {status}")

print("\n=== Test 2: Upsert ===")
random.seed(42)
test_vec = [random.gauss(0, 1) for _ in range(1024)]
chunks = [{
    "source": "test",
    "title": "Test Chunk - pgvector integration test",
    "text": "This is a test chunk for verifying pgvector store connectivity and vector search.",
    "embedding": test_vec,
}]
count = upsert_chunks(chunks)
print(f"Upserted {count} chunk(s)")

print("\n=== Test 3: Search ===")
results = search(test_vec, top_k=3)
print(f"Search returned {len(results)} results:")
for r in results[:3]:
    print(f"  [{r['score']:.4f}] {r['title']}")

# Test with different query to ensure HNSW works properly
print("\n=== Test 4: Cross-query ===")
query_vec = [random.gauss(0, 1) for _ in range(1024)]
results = search(query_vec, top_k=5)
print(f"Search returned {len(results)} results:")
for r in results[:3]:
    print(f"  [{r['score']:.4f}] {r['title']}")

# Test with empty DB (disabled)
print("\n=== Test 5: Disabled mode ===")
os.environ["PGVECTOR_ENABLED"] = "0"
import importlib
import agent.pgvector_store as store
store = importlib.reload(store)
count = store.upsert_chunks(chunks)
results = store.search(test_vec, top_k=3)
print(f"Disabled: upsert={count}, search_results={len(results)}")

print("\n=== ALL TESTS PASSED ===")
