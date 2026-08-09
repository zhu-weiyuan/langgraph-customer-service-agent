import psycopg2, os
dsn = os.environ.get('DATABASE_URL', '') or 'postgresql://customer_service:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/customer_service'
conn = psycopg2.connect(dsn)
cur = conn.cursor()
cur.execute("SELECT version()")
print('PG:', cur.fetchone()[0][:80])
try:
    cur.execute("SELECT extname FROM pg_extension WHERE extname='pgvector'")
    row = cur.fetchone()
    print('pgvector:', 'installed' if row else 'NOT installed')
except Exception as e:
    print('pgvector check error:', e)
conn.close()
