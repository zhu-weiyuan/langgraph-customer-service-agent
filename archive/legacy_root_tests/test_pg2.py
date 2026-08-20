import os, asyncio
os.chdir(r'C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent')
from dotenv import load_dotenv
load_dotenv()
import asyncpg

async def test():
    conn = await asyncpg.connect(os.getenv('DATABASE_URL'))
    # Check embedding dimension
    emb = await conn.fetchval("SELECT embedding FROM knowledge_chunks LIMIT 1")
    if emb:
        print(f'Embedding type: {type(emb)}, length: {len(emb)}')
    # Test cosine similarity search
    test_emb = await conn.fetchval("SELECT embedding FROM knowledge_chunks LIMIT 1")
    if test_emb:
        rows = await conn.fetch("""
            SELECT chunk_key, title, 1 - (embedding <=> $1) as similarity
            FROM knowledge_chunks
            ORDER BY embedding <=> $1
            LIMIT 3
        """, test_emb)
        print('Self-search (should be ~1.0):')
        for r in rows:
            print(f'  {r["chunk_key"][:20]}: {r["similarity"]:.4f} - {r["title"]}')
    # Test with a query embedding
    import requests, json
    payload = {"model": "Qwen/Qwen3-Embedding-4B", "input": "快递几天到"}
    headers = {"Authorization": f"Bearer {os.getenv('EMBEDDING_API_KEY')}", "Content-Type": "application/json"}
    try:
        resp = requests.post(f"{os.getenv('EMBEDDING_BASE_URL')}/embeddings", json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            q_emb = data['data'][0]['embedding'][:1024]  # Matryoshka reduction
            import math
            norm = math.sqrt(sum(v*v for v in q_emb))
            q_emb = [v/norm for v in q_emb]
            rows = await conn.fetch("""
                SELECT chunk_key, title, 1 - (embedding <=> $1) as similarity
                FROM knowledge_chunks
                ORDER BY embedding <=> $1
                LIMIT 5
            """, q_emb)
            print('Query "快递几天到":')
            for r in rows:
                print(f'  {r["similarity"]:.4f} - {r["title"]}')
    except Exception as e:
        print(f'Embedding API error: {e}')
    await conn.close()

asyncio.run(test())