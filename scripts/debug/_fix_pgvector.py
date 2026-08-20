import psycopg2, os

dsn = os.environ.get('DATABASE_URL', 'postgresql://customer_service:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/customer_service')
conn = psycopg2.connect(dsn)
cur = conn.cursor()

# Check existing tables
cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
print('Tables:', [r[0] for r in cur.fetchall()])

# Install pgvector extension
try:
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.commit()
    print('pgvector extension created successfully')
except Exception as e:
    print(f'Could not create pgvector: {e}')

conn.close()
