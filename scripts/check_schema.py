import psycopg
c = psycopg.connect('postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph', autocommit=True)
cols = c.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='user_preferences' ORDER BY ordinal_position").fetchall()
for col in cols:
    print('%s (%s)' % (col[0], col[1]))
c.close()
