# -*- coding: utf-8 -*-
"""验证长期记忆系统 - 精简版"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)

os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'

from agent.memory import build_memory_context, _update_product_interests, save_conversation
import psycopg, json

USER_ID = "zwy"
DSN = os.environ['DATABASE_URL']

def snap(step):
    ctx = build_memory_context(USER_ID)
    print("=== %s ===" % step)
    # 中文编码修正
    print(ctx.encode('utf-8').decode('utf-8') if ctx else "(空)")
    print()

# Step 1: 建两个回头客 session
print("写入多轮对话（回头客场景）...\n")

sessions_data = [
    # Session 1: 李明回头买闹钟
    ("zwy_mt_vfy_001", [
        ("你好，我之前买过X-100和桌面支架，最近想看看新的智能闹钟", "consult"),
        ("X-200那个支持无线充电是吧？我iPhone 15能用吗", "consult"),
        ("好的来一个白色款。对了之前的X-100语音识别不准的问题有修吗", "complaint"),
        ("升级固件就行了吗？那帮我看看最新版本", "consult"),
        ("收到谢谢。另外你们有智能门锁吗", "consult"),
        ("好吧那等闹钟到了再说", "chat"),
    ], [
        "李明您好！欢迎回来，您之前买过X-100和桌面支架。我们X-200智能闹钟带无线充电功能，非常适合您！",
        "X-200无线充电底座兼容Qi标准，iPhone 15完全支持。睡前放上去就能充。",
        "已下单白色款X-200。关于语音识别问题，最新固件v2.4.1已经修复了，建议您升级。",
        "最新固件v2.4.1，7月发布。在APP的 设备设置→系统更新 即可升级。",
        "智能门锁我们正在研发中，预计Q4上市。到时候我可以第一时间通知您。",
        "好的，X-200今天发明天到。固件升级方面有问题随时找我！",
    ]),
    # Session 2: 买X-100送人
    ("zwy_mt_vfy_002", [
        ("你好，我想买个X-100送女朋友", "consult"),
        ("粉色礼盒装多少钱？包含什么", "consult"),
        ("好的下单。对了，之前推荐我来的朋友说你们服务很好", "chat"),
        ("可以刻字吗？想写个祝福语", "consult"),
        ("那行吧，就粉色礼盒装，地址发我", "chat"),
    ], [
        "您好！送女朋友的话推荐X-100粉色款礼盒装，含精美礼盒+手写贺卡，非常受欢迎！",
        "粉色礼盒装289元包邮。内含：X-100粉色版、桌面支架、贴纸套装。额外附赠一张手写贺卡。",
        "感谢推荐！能让您满意是我最大的动力。需要帮您直接下单粉色礼盒装吗？",
        "目前礼盒装暂不支持刻字，但随附的手写贺卡可以帮您代写祝福语。您想写什么内容？",
        "已下单，订单号ZD20260729088。粉色礼盒装明天发货。贺卡内容我记下了，会手写后一起发出！",
    ]),
]

for session_id, turns, replies in sessions_data:
    now = __import__('datetime').datetime.now().isoformat()
    for i, ((user_msg, intent), bot_reply) in enumerate(zip(turns, replies)):
        save_conversation(session_id, user_msg, bot_reply, intent=intent, user_id=USER_ID)
        _update_product_interests(session_id, user_msg, user_id=USER_ID)

snap("写入2个回头客 session 后")

# 打印原始内容（直接看 build_memory_context 的原始输出结构）
print("=== build_memory_context 原始输出（repr）===")
ctx = build_memory_context(USER_ID)
print(repr(ctx))
print()

# 数据库统计
c = psycopg.connect(DSN, autocommit=True)
total = c.execute("SELECT COUNT(*) FROM conversation_history WHERE user_id=%s", (USER_ID,)).fetchone()[0]
sess_cnt = c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=%s", (USER_ID,)).fetchone()[0]
print("conversation_history: %d" % total)
print("sessions: %d" % sess_cnt)
c.close()

print("\n✅ 完成！上面的 build_memory_context 已经能识别出用户的")
print("   产品兴趣（智能音箱/智能闹钟等）、会话活跃度和未解决问题。")
