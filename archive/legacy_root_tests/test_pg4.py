import os, asyncio, requests, math
os.chdir(r'C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent')
from dotenv import load_dotenv
load_dotenv()
import asyncpg

async def test():
    conn = await asyncpg.connect(os.getenv('DATABASE_URL'))
    # Get a test embedding from the DB
    test_emb_str = await conn.fetchval("SELECT embedding FROM knowledge_chunks LIMIT 1")
    print(f'Test embedding (str): {test_emb_str[:50]}...')
    
    # Search using the string embedding directly
    rows = await conn.fetch("""
        SELECT chunk_key, title, 1 - (embedding <=> $1::vector) as similarity
        FROM knowledge_chunks
        ORDER BY embedding <=> $1::vector
        LIMIT 3
    """, test_emb_str)
    print('Self-search:')
    for r in rows:
        print(f'  {r["similarity"]:.4f} - {r["title"]}')
    
    # Test with query embedding
    payload = {"model": "Qwen/Qwen3-Embedding-4B", "input": "快递几天到"}
    headers = {"Authorization": f"Bearer {os.getenv('EMBEDDING_API_KEY')}", "Content-Type": "application/json"}
    resp = requests.post(f"{os.getenv('EMBEDDING_BASE_URL')}/embeddings", json=payload, headers=headers, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        q_emb = data['data'][0]['embedding'][:1024]
        norm = math.sqrt(sum(v*v for v in q_emb))
        q_emb = [v/norm for v in q_emb]
        q_emb_str = '[' + ','.join(str(v) for v in q_emb) + ']'
        print(f'Query embedding: {q_emb_str[:50]}...')
        
        rows = await conn.fetch("""
            SELECT chunk_key, title, 1 - (embedding <=> $1::vector) as similarity
            FROM knowledge_chunks
            ORDER BY embedding <=> $1::vector
            LIMIT 5
        """, q_emb_str)
        print('Query "快递几天到":')
        for r in rows:
            print(f'  {r["similarity"]:.4f} - {r["title"]}')
    await conn.close()

asyncio.run(test())