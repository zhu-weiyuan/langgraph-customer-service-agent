# -*- coding: utf-8 -*-
import sys, psycopg
sys.stdout.reconfigure(encoding='utf-8')

conn = psycopg.connect('postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph')
cur = conn.cursor()

cur.execute("SELECT count(*), min(created_at), max(created_at) FROM sessions WHERE user_id = 'zwy'")
cnt, cmin, cmax = cur.fetchone()
print('Sessions for zwy:', cnt, '| From:', str(cmin)[:19], 'To:', str(cmax)[:19])

cur.execute("SELECT count(*) FROM conversation_history WHERE user_id = 'zwy'")
cnt2 = cur.fetchone()[0]
print('Conversations for zwy:', cnt2)

cur.execute("SELECT count(*) FROM user_memories WHERE user_id = 'zwy'")
cnt3 = cur.fetchone()[0]
print('User memories for zwy:', cnt3)

cur.execute("SELECT session_id, created_at, message_count FROM sessions WHERE user_id = 'zwy' ORDER BY created_at DESC LIMIT 5")
rows = cur.fetchall()
print()
print('Recent sessions:')
for r in rows:
    print('  %s... | %s | %d msgs' % (str(r[0])[:30], str(r[1])[:19], r[2] if r[2] else 0))
