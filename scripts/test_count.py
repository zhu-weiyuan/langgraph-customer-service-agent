import sys, psycopg
sys.stdout.reconfigure(encoding='utf-8')
conn = psycopg.connect(
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph",
    autocommit=True
)
# test count
c = conn.execute("SELECT COUNT(*) FROM conversation_history WHERE user_id = 'zwy'").fetchone()[0]
print("Total zwy records:", c)
c2 = conn.execute("SELECT COUNT(*) FROM conversation_history WHERE user_id = 'zwy' AND (bot_reply LIKE '感谢您的反馈%' OR bot_reply LIKE '抱歉%')").fetchone()[0]
print("Bad records:", c2)
conn.close()
