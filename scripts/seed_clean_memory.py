# -*- coding: utf-8 -*-
"""写入一段包含正确中文关键词的对话，触发修复后的记忆系统"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)
os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'

from agent.memory import _update_product_interests, save_conversation, build_memory_context
import psycopg

DSN = os.environ['DATABASE_URL']

# Session 1: 智能音箱话题
SESSION1 = "zwy_mt_clean_test_001"
turns1 = [
    ("你好，我想看看你们的智能音箱", "consult"),
    ("X-100能连蓝牙吗？我想接电脑用", "consult"),
    ("好的，买个X-100。再问问桌面支架有货吗", "consult"),
]
replies1 = [
    "您好！我们目前有X-100和X-300 Pro两款智能音箱，您对哪款感兴趣？",
    "X-100支持蓝牙5.0和AUX输入，可以连电脑当外放使用。",
    "桌面支架有货，搭配X-100还可以减少桌面共振，改善音质。39元一个。",
]

# Session 2: 配件话题
SESSION2 = "zwy_mt_clean_test_002"
turns2 = [
    ("有没有给X-200的充电底座？", "consult"),
    ("好的，我要一个。另外你们有音箱的保护套吗", "consult"),
]
replies2 = [
    "有的！X-200专用的Type-C充电底座49元，支持快充。",
    "X-200保护套29元，硅胶材质，防摔防刮。",
]

# 写入 Session 1
now = __import__('datetime').datetime.now().isoformat()
c = psycopg.connect(DSN, autocommit=True)
for i, ((user_msg, intent), bot_reply) in enumerate(zip(turns1, replies1)):
    save_conversation(SESSION1, user_msg, bot_reply, intent=intent, user_id='zwy')
    _update_product_interests(SESSION1, user_msg, user_id='zwy')
    if i == 0:
        c.execute("""
            INSERT INTO sessions (session_id, user_id, title, created_at, last_active, message_count)
            VALUES (%s,'zwy',%s,%s,%s,1)
            ON CONFLICT(session_id) DO UPDATE SET last_active=EXCLUDED.last_active, message_count=sessions.message_count+1
        """, (SESSION1, turns1[0][0][:80], now, now))

# Write Session 2
for i, ((user_msg, intent), bot_reply) in enumerate(zip(turns2, replies2)):
    save_conversation(SESSION2, user_msg, bot_reply, intent=intent, user_id='zwy')
    _update_product_interests(SESSION2, user_msg, user_id='zwy')
    if i == 0:
        c.execute("""
            INSERT INTO sessions (session_id, user_id, title, created_at, last_active, message_count)
            VALUES (%s,'zwy',%s,%s,%s,1)
            ON CONFLICT(session_id) DO UPDATE SET last_active=EXCLUDED.last_active, message_count=sessions.message_count+1
        """, (SESSION2, turns1[0][0][:80], now, now))

c.close()

# 查看结果
print("=== build_memory_context ===")
print(build_memory_context('zwy'))

print("\n=== user_preferences ===")
c2 = psycopg.connect(DSN, autocommit=True)
for r in c2.execute("SELECT session_id, product_interests FROM user_preferences WHERE user_id='zwy'").fetchall():
    print("  [%s] %s" % (r[0][:30], r[1]))
c2.close()
