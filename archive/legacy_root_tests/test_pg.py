import os, asyncio
os.chdir(r'C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent')
from dotenv import load_dotenv
load_dotenv()
import asyncpg

async def test():
    conn = await asyncpg.connect(os.getenv('DATABASE_URL'))
    ver = await conn.fetchval('SELECT version()')
    ext = await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname='vector'")
    print(f'PG: {ver[:80]}')
    print(f'pgvector: {"enabled" if ext else "NOT ENABLED"}')
    if ext:
        exists = await conn.fetchval("SELECT 1 FROM information_schema.tables WHERE table_name='knowledge_chunks'")
        print(f'knowledge_chunks table: {"exists" if exists else "MISSING"}')
        if exists:
            cnt = await conn.fetchval('SELECT COUNT(*) FROM knowledge_chunks')
            print(f'Rows: {cnt}')
    await conn.close()

asyncio.run(test())