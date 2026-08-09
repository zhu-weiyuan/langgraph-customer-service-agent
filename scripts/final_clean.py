import sys, psycopg
sys.stdout.reconfigure(encoding='utf-8')
conn = psycopg.connect(
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph",
    autocommit=True
)

# Check what's remaining
bad = conn.execute("SELECT id, user_message, bot_reply FROM conversation_history WHERE user_id = 'zwy' AND (bot_reply LIKE '感谢您的反馈%' OR bot_reply LIKE '抱歉%')").fetchall()
print("Remaining bad records:", len(bad))
for b in bad[:5]:
    print("  id=%s | %s | %s" % (b[0], str(b[1])[:50], str(b[2])[:50]))

# Delete them
r = conn.execute("DELETE FROM conversation_history WHERE user_id = 'zwy' AND (bot_reply LIKE '感谢您的反馈%' OR bot_reply LIKE '抱歉%')")
print("Deleted:", r.rowcount)

conn.execute("DELETE FROM sessions WHERE user_id = 'zwy'")

remaining = conn.execute("SELECT COUNT(*) FROM conversation_history WHERE user_id = 'zwy'").fetchone()[0]
print("Remaining total:", remaining)
conn.close()
