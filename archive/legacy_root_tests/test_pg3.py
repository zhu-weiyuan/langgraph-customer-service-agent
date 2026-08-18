import os, asyncio
os.chdir(r'C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent')
from dotenv import load_dotenv
load_dotenv()
import asyncpg

async def test():
    conn = await asyncpg.connect(os.getenv('DATABASE_URL'))
    cols = await conn.fetch("""
        SELECT column_name, data_type, udt_name 
        FROM information_schema.columns 
        WHERE table_name = 'knowledge_chunks'
    """)
    for c in cols:
        print(f'{c["column_name"]}: {c["data_type"]} ({c["udt_name"]})')
    row = await conn.fetchrow('SELECT embedding FROM knowledge_chunks LIMIT 1')
    if row:
        emb = row['embedding']
        print(f'Type: {type(emb)}, starts with: {str(emb)[:50]}')
    await conn.close()

asyncio.run(test())