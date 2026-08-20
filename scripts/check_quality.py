import sys, psycopg
sys.stdout.reconfigure(encoding='utf-8')
conn = psycopg.connect(
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph",
    autocommit=True
)

# Check a sample of adversarial records
print("=== Sample adversarial records ===")
rows = conn.execute("""
    SELECT session_id, user_message, bot_reply 
    FROM conversation_history 
    WHERE user_id = 'zwy' AND session_id LIKE 'zwy_e2e_03%%'
    LIMIT 5
""").fetchall()
for r in rows:
    print("  SESSION: %s" % r[0])
    print("  USER:    %s" % r[1][:60])
    print("  BOT:     %s" % r[2][:80])
    print()

# Check high_fail_risk
print("=== Sample high_fail_risk records ===")
rows = conn.execute("""
    SELECT session_id, user_message, bot_reply 
    FROM conversation_history 
    WHERE user_id = 'zwy' AND session_id LIKE 'zwy_e2e_04%%'
    LIMIT 5
""").fetchall()
for r in rows:
    print("  SESSION: %s" % r[0])
    print("  USER:    %s" % r[1][:60])
    print("  BOT:     %s" % r[2][:80])
    print()

# Check what session IDs exist in the zwy_e2e range
stats = conn.execute("""
    SELECT substring(session_id, 8, 2) as prefix, 
           COUNT(*) as cnt,
           COUNT(DISTINCT bot_reply) as distinct_replies
    FROM conversation_history 
    WHERE user_id = 'zwy' AND session_id LIKE 'zwy_e2e_%%'
    GROUP BY substring(session_id, 8, 2)
    ORDER BY prefix
""").fetchall()
print("=== Session ID prefix breakdown ===")
for s in stats:
    print("  zwy_e2e_%sxx: %d records, %d distinct replies" % (s[0], s[1], s[2]))

total = conn.execute("SELECT COUNT(*) FROM conversation_history WHERE user_id = 'zwy'").fetchone()[0]
print("\nTotal: %d" % total[0])
conn.close()
