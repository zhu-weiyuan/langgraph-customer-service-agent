# -*- coding: utf-8 -*-
"""找出灌进去的废数据（固定模板回复）"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.runtime_db import connect

conn = connect()

# 1. 找"感谢您的反馈"开头的废数据
print("=" * 60)
print("1. '感谢您的反馈' 固定模板废数据")
print("=" * 60)
rows = conn.execute("""
    SELECT id, session_id, user_message, bot_reply, intent, timestamp
    FROM conversation_history
    WHERE bot_reply LIKE '感谢您的反馈%%' AND user_id = 'zwy'
    ORDER BY id
""").fetchall()
print(f"总计: {len(rows)} 条\n")
for r in rows[:10]:
    print(f'  [#{r["id"]}] {r["session_id"]}')
    print(f'    intent={r["intent"]}')
    print(f'    USER: {r["user_message"][:70]}')
    print(f'    BOT:  {r["bot_reply"][:70]}')
    print()
if len(rows) > 10:
    print(f'  ... 还有 {len(rows)-10} 条 ...\n')

# 2. 找"抱歉"开头的废数据（adversarial 模板）
print("=" * 60)
print("2. '抱歉' 固定模板废数据")
print("=" * 60)
rows2 = conn.execute("""
    SELECT id, session_id, user_message, bot_reply, intent, timestamp
    FROM conversation_history
    WHERE bot_reply LIKE '抱歉%%' AND user_id = 'zwy'
    ORDER BY id
""").fetchall()
print(f"总计: {len(rows2)} 条\n")
for r in rows2[:10]:
    print(f'  [#{r["id"]}] {r["session_id"]}')
    print(f'    intent={r["intent"]}')
    print(f'    USER: {r["user_message"][:70]}')
    print(f'    BOT:  {r["bot_reply"][:70]}')
    print()
if len(rows2) > 10:
    print(f'  ... 还有 {len(rows2)-10} 条 ...\n')

# 3. 汇总
total_bad = len(rows) + len(rows2)
print("=" * 60)
print(f"废数据总计: {total_bad} / 500 条 ({(total_bad/500)*100:.1f}%)")
print("=" * 60)

# 4. 按 intent 分布
stats = conn.execute("""
    SELECT intent, COUNT(*) as cnt
    FROM conversation_history
    WHERE (bot_reply LIKE '感谢您的反馈%%' OR bot_reply LIKE '抱歉%%')
      AND user_id = 'zwy'
    GROUP BY intent
    ORDER BY cnt DESC
""").fetchall()
print("\n按 intent 分布:")
for s in stats:
    print(f'  {s["intent"]}: {s["cnt"]}')

conn.close()
