import psycopg
c = psycopg.connect('postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph', autocommit=True)
# PKs
tabs = ['user_preferences', 'user_memories']
for t in tabs:
    pk = c.execute("""
        SELECT a.attname, format_type(a.atttypid, a.atttypmod) 
        FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
    """, (t,)).fetchall()
    print('%s PK: %s' % (t, [r[0] for r in pk]))
    # unique constraints
    uc = c.execute("""
        SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint 
        WHERE conrelid = %s::regclass AND contype='u'
    """, (t,)).fetchall()
    for r in uc:
        print('  UNIQUE: %s' % r[1])
c.close()
