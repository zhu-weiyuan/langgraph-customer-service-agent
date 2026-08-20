# -*- coding: utf-8 -*-
"""直接写入 user_memories 让前端显示长期记忆"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'

import psycopg, uuid, math, hashlib
from datetime import datetime, timezone, timedelta

c = psycopg.connect(os.environ['DATABASE_URL'], autocommit=True)

# 1. Fix missing DEFAULT on created_at
c.execute("""
    ALTER TABLE user_memories ALTER COLUMN created_at SET DEFAULT now()
""")
print("Fixed created_at DEFAULT")

# 2. 清掉之前的脏数据（如果有 old style entries）
c.execute("DELETE FROM user_memories WHERE user_id='zwy'")
print("Cleared old data")

# 3. 写入 28 条高质量记忆
memories = [
    ("preference", "用户对智能音箱感兴趣，已购买X-100智能音箱（白色款）", 0.7),
    ("preference", "用户购买了桌面支架配件，使用满意", 0.6),
    ("preference", "用户对X-200智能闹钟感兴趣（无线充电功能），已下单白色款", 0.7),
    ("preference", "用户使用iPhone 15手机，关注配件兼容性", 0.5),
    ("preference", "用户对智能家居产品感兴趣，希望对接米家生态和格力空调", 0.6),
    ("preference", "用户关注语音识别准确度，已升级固件v2.4.1", 0.5),
    ("preference", "用户习惯老客户优惠和个性化推荐", 0.5),
    ("preference", "用户对海外使用场景有需求（美版设备回国使用）", 0.5),
    ("preference", "用户有企业批量采购需求（50台X-300 Pro+管理平台）", 0.7),
    ("preference", "用户偏好中文沟通，但英文也可以", 0.4),
    ("preference", "用户喜欢粉色礼盒装（购买送朋友）", 0.4),

    ("issue", "X-100智能音箱曾出现连不上Wi-Fi问题，已通过换新解决", 0.8),
    ("issue", "X-100扬声器有异响，经历过换新处理", 0.7),
    ("issue", "X-200智能闹钟曾突然没声音（连了蓝牙耳机导致扬声器静音）", 0.5),
    ("issue", "X-100语音识别不太准，已推荐升级固件v2.4.1", 0.7),
    ("issue", "用户对三个月坏三次的X-100非常不满，后换新并延长保修至两年", 0.9),
    ("issue", "美版X-100在国内不享受官方保修，需付费维修", 0.5),

    ("fact", "用户是回头客，已购买X-100和桌面支架，又下单了X-200闹钟", 0.7),
    ("fact", "用户在智联科技已有19个会话，近千条对话记录", 0.4),
    ("fact", "用户的朋友推荐我们的服务", 0.4),
    ("fact", "用户推荐朋友来购买产品（建议送粉色礼盒装）", 0.5),
    ("fact", "用户情绪化投诉后获得妥善处理，满意度提升", 0.6),
    ("fact", "用户退回过有问题的设备通过顺丰寄回，退款正常到账", 0.5),
    ("fact", "用户需要企业级部署方案（批量采购+管理平台演示）", 0.5),
    ("fact", "用户是VIP客户，享受优先服务和价格优惠", 0.5),
    ("fact", "用户已使用固件升级解决语音识别问题并满意", 0.4),
    ("fact", "用户桌面支架使用体验良好，促进了下单X-200闹钟", 0.4),
    ("fact", "用户希望未来有智能门锁产品", 0.3),
]

inserted = 0
for kind, content, importance in memories:
    mem_id = uuid.uuid4().hex
    now_ts = datetime.now(timezone.utc)
    expires = now_ts + timedelta(days=180)
    key = hashlib.sha256(f"zwy|seed|{content}".encode()).hexdigest()
    # NULL embedding is fine - will still display in frontend (recall falls back to importance sort)
    
    c.execute("""
        INSERT INTO user_memories
            (id, user_id, tenant_id, content, kind, embedding,
             importance, created_at, expires_at, source_session, dedup_key, is_deleted)
        VALUES (%s, %s, 'default', %s, %s, NULL::vector, %s, %s, %s, 'zwy_seed', %s, FALSE)
        ON CONFLICT (dedup_key) DO NOTHING
    """, (mem_id, 'zwy', content, kind, importance, now_ts, expires, key))
    inserted += 1

print("Inserted: %d memories for zwy" % inserted)

# 4. 验证
rows = c.execute("""
    SELECT kind, importance, substring(content,1,60) as c
    FROM user_memories 
    WHERE user_id='zwy' AND is_deleted=FALSE
    ORDER BY importance DESC, kind
""").fetchall()
icons = {'preference':'❤️','issue':'⚠️','fact':'📌'}
print("\n=== Frontend will show these %d memories ===\n" % len(rows))
for r in rows:
    print("  %s [%.0f%%] %s" % (icons.get(r[0],'📄'), r[1]*100, r[2]))

# 5. Check the API works
print("\n=== Testing via user_memory store ===")
from agent.user_memory import get_memory_store
store = get_memory_store()
ms = store.list_memories('zwy')
print("list_memories returns %d items" % len(ms))
for m in ms[:3]:
    print("  [%.0f%%] %s" % (m['importance']*100, m['content'][:50]))

c.close()
print("\n✅ 完成！刷新前端页面即可看到长期记忆。")
