import sys, psycopg
sys.stdout.reconfigure(encoding='utf-8')
conn = psycopg.connect(
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph",
    autocommit=True
)

# 1. Check ALL for bad templates
bad = conn.execute("""
    SELECT COUNT(*) FROM conversation_history 
    WHERE user_id = 'zwy' AND (
        bot_reply LIKE '感谢您的反馈%' 
        OR bot_reply LIKE '抱歉%' 
        OR bot_reply LIKE '您好，您好%'
        OR bot_reply LIKE '您好，我来看看您的问题%'
        OR bot_reply LIKE '好的，我来看看%'
    )
""").fetchone()[0]
print("Still bad: %d" % bad)

# 2. Count distinct reply patterns
d = conn.execute("""
    SELECT COUNT(DISTINCT bot_reply) FROM conversation_history 
    WHERE user_id = 'zwy'
""").fetchone()[0]
print("Distinct replies: %d" % d)

# 3. Show all unique bot_reply variants (first 100 chars)
variants = conn.execute("""
    SELECT DISTINCT substring(bot_reply, 1, 60) as reply_start, COUNT(*) as cnt
    FROM conversation_history 
    WHERE user_id = 'zwy'
    GROUP BY substring(bot_reply, 1, 60)
    HAVING COUNT(*) > 3
    ORDER BY cnt DESC
    LIMIT 30
""").fetchall()
print("\nMost common reply patterns (>3 duplicates):")
for v in variants:
    print("  [%d] %s..." % (v[1], v[0][:50]))

# 4. Check specific adversarial/hfr records are present
cats = {"adversarial": 0, "high_fail_risk": 0}
for cat in cats:
    # Read golden set to find IDs
    pass

# 5. Check if emotion records exist
emotion_count = conn.execute("""
    SELECT COUNT(*) FROM conversation_history 
    WHERE user_id = 'zwy' AND user_message LIKE '%绝望%' OR user_message LIKE '%孤独%' OR user_message LIKE '%残疾%'
""").fetchone()[0]
print("\nEmotion records: %d" % emotion_count)

# Count by session_id prefix
# zwy_e2e_03xx = adversarial (100 records)
adv = conn.execute("""
    SELECT COUNT(*) FROM conversation_history 
    WHERE user_id = 'zwy' AND session_id >= 'zwy_e2e_0317' AND session_id < 'zwy_e2e_0417'
""").fetchone()[0]
# zwy_e2e_04xx = high_fail_risk (84 records)
hfr = conn.execute("""
    SELECT COUNT(*) FROM conversation_history 
    WHERE user_id = 'zwy' AND session_id >= 'zwy_e2e_0417' AND session_id < 'zwy_e2e_0501'
""").fetchone()[0]
# zwy_e2e_0501+ = new complex cases (21 records)
new = conn.execute("""
    SELECT COUNT(*) FROM conversation_history 
    WHERE user_id = 'zwy' AND session_id >= 'zwy_e2e_0501'
""").fetchone()[0]

print("\nCoverage check:")
print("  Adversarial (zwy_e2e_0317-0416): %d/100" % adv)
print("  HighFailRisk (zwy_e2e_0417-0500): %d/84" % hfr)
print("  NewComplex  (zwy_e2e_0501+):      %d/21" % new)

conn.close()
