# -*- coding: utf-8 -*-
"""
直接生成模拟对话灌数据库（不调 LLM，快！）
一天 1000 条都搞定
用户名：zwy
"""
import sys, os, json, time, uuid
from datetime import datetime
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.memory import save_conversation

USER_ID = "zwy"

# 预置回复模板（根据 intent 类型生成合理回复）
REPLY_TEMPLATES = {
    "chat": [
        "你好！我是智联科技的智能客服助手，很高兴为您服务。请问有什么可以帮助您的吗？",
        "您好！欢迎来到智联科技。请问今天有什么需要帮您解决的吗？",
        "您好呀！有什么可以帮您的？产品咨询、技术问题、售后服务都可以问我哦。",
        "早上好！祝您有个愉快的一天。有什么需要我帮忙的吗？",
        "您好！我是小智，您的专属客服助手。请问有什么我可以帮您的？",
    ],
    "greeting": [
        "您好！欢迎来到智联科技客服中心。请问有什么可以帮您？",
        "你好！我是智能助手，有产品问题或者售后需求都可以告诉我。",
        "Hi！很高兴见到你。请问今天想咨询什么？",
    ],
    "consult": [
        "好的，我来帮您解答这个问题。根据我们的产品信息，X-100 智能音箱支持语音助手功能，可以通过语音指令控制播放音乐、查询天气、设置闹钟等。具体来说，它的语音识别准确率很高，支持远场拾音，您在 5 米范围内都可以正常使用。",
        "感谢您的咨询！关于您问到的这个问题，我为您整理了以下信息：\n1. 该功能在 X-200 和 X-300 Pro 上均支持\n2. 设置步骤：打开 APP -> 设备设置 -> 开启功能\n3. 如有特殊需求可以联系我们的技术支持团队",
        "好的，我来查询一下。根据我们的产品规格，这款产品的相关参数如下：\n- 支持蓝牙 5.0 连接\n- 兼容 iOS 和 Android 设备\n- 连接范围可达 10 米\n- 支持多设备同时连接",
        "您问到的这个问题我来解答一下。我们的产品提供 1 年质保服务，如果在质保期内出现非人为损坏的质量问题，可以享受免费维修或换货服务。需要我帮您查询具体的保修政策吗？",
        "明白了，让我为您详细说明一下流程：\n1. 首先登录您的账号\n2. 进入订单详情页\n3. 点击申请售后/退货\n4. 填写退货原因并提交\n5. 等待审核（一般 1-3 个工作日）\n6. 审核通过后安排上门取件",
        "您好，关于这个问题我需要先确认一些信息。请问您的设备型号是什么？什么时候购买的？遇到的具体情况能详细说一下吗？这样我可以更准确地帮您分析。",
        "好的，这个问题的解决方案如下：\n1. 尝试重启设备\n2. 检查网络连接是否正常\n3. 确保固件版本是最新的\n4. 如果问题依旧，建议联系我们的技术团队做进一步排查",
        "关于价格方面，目前 X-100 售价为 299 元，X-200 为 499 元，X-300 Pro 为 899 元。近期我们有不少优惠活动，如果您感兴趣我可以帮您看看有没有适合您的优惠方案。",
        "明白了！我来帮您查一下物流信息。根据系统显示，您的订单目前处于已发货状态，预计 2-3 个工作日内送达。物流单号是 SF1234567890，您可以随时查询最新状态。",
        "感谢您对智联科技的支持！您提到的这个问题我们会高度重视。让我先帮您核实一下相关信息，稍后给您一个准确的答复。",
        "好的，关于发票的问题我来为您说明。我们支持开具电子发票和纸质发票两种形式。电子发票会在订单完成后自动发送到您的注册邮箱，纸质发票会随货一起发出。如果您需要修改发票信息，可以在订单详情中申请重开。",
    ],
    "complaint": [
        "非常抱歉给您带来了不好的体验！我完全理解您的心情。请您不要着急，我来帮您跟进这个问题。请问您方便提供一下订单号吗？我会优先为您处理。",
        "很抱歉给您带来困扰。我已经记录了您的反馈，会立即升级给我们的客服主管处理。我承诺在 24 小时内会有专人联系您解决问题。请问您方便接听电话吗？",
        "对不起，让您失望了。您的意见对我们非常重要，我已经把情况记录下来并加急处理。为了更快地帮您解决问题，您可以告诉我具体的订单信息和遇到的情况吗？",
        "非常抱歉！我理解您的不满。根据您描述的情况，我会为您申请优先处理通道。请您稍等片刻，我马上为您核实解决方案。",
        "很抱歉给您带来不便。这个问题我来帮您处理，先确认一下：是购买的产品出现了质量问题，还是物流服务方面的问题呢？我会根据具体情况为您提供最合适的解决方案。",
    ],
    "ending": [
        "不客气！祝您生活愉快，有任何问题随时联系我们。再见！",
        "很高兴能帮到您！如果以后还有任何问题，随时找我就好。祝您使用愉快！",
        "好的，祝您生活愉快！期待再次为您服务。再见！",
        "谢谢您的信任！记得给我们的服务点个赞哦。有任何需要随时联系我！",
        "很高兴为您服务！祝您和家人生活愉快，再见！",
        "好的，已为您处理完毕。感谢您选择智联科技，我们一直在努力为您提供更好的产品和服务。再见！",
    ],
}

# 边缘/对抗情况简易回复
FALLBACK_REPLIES = [
    "好的，我来看看您的问题。请稍等一下，我帮您查询相关信息。",
    "收到您的消息了。请问您能提供更多细节吗？这样我能更准确地帮您分析。",
    "您好，我注意到您发送的消息比较特殊。请问您可以进一步描述一下具体问题吗？",
    "感谢您的反馈！我会根据您提供的信息尽力帮您寻找解决方案。",
    "您的问题我已经收到。让我来帮您梳理一下。",
    "好的，已收到。请稍等，我正在为您查询相关信息。",
]

def generate_reply(case):
    """根据用例生成合理的模拟回复"""
    intent = case.get("expected_intent", "chat")
    category = case.get("category", "normal")
    
    # 对抗数据/高失败风险给安全回复
    if category == "adversarial":
        return "抱歉，您的问题超出了我的服务范围。我是智能客服助手，主要负责产品咨询和技术支持。如果您有其他产品相关的问题，我很乐意帮助您。"
    if category == "high_fail_risk":
        return "感谢您的反馈。这个问题比较复杂，我会记录下来并转交给我们的专业团队处理。他们会尽快与您联系。请问您方便留下联系方式吗？"
    
    # 根据 intent 选模板
    templates = REPLY_TEMPLATES.get(intent, REPLY_TEMPLATES.get("consult", []))
    return templates[hash(case["id"]) % len(templates)]


def main():
    # 加载 golden set
    golden_path = "tests/data/golden_set_500_zwy.json"
    with open(golden_path, 'r', encoding='utf-8') as f:
        test_cases = json.load(f)
    
    total = len(test_cases)
    t0 = time.time()
    done = 0
    
    print("=" * 60)
    print(f"灌数据到 PostgreSQL - 用户：{USER_ID}")
    print(f"总条数：{total}")
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    for i, case in enumerate(test_cases):
        input_text = case['input']
        case_id = case.get('id', f'case_{i+1}')
        expected_intent = case.get('expected_intent', 'consult')
        category = case.get('category', 'normal')
        
        session_id = f"zwy_e2e_{i+1:04d}"
        bot_reply = generate_reply(case)
        
        # 情感信息
        if expected_intent == "complaint":
            emotion, intensity = "negative", 3
        elif expected_intent == "chat":
            emotion, intensity = "positive", 1
        elif category == "adversarial":
            emotion, intensity = "neutral", 1
        else:
            emotion, intensity = "neutral", 1
        
        try:
            save_conversation(
                session_id=session_id,
                user_message=input_text,
                bot_reply=bot_reply,
                intent=expected_intent,
                emotion=emotion,
                emotion_intensity=intensity,
                resolved=True,
                user_id=USER_ID,
            )
            done += 1
        except Exception as e:
            print(f"  [ERR] {case_id}: {str(e)[:60]}")
        
        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            print(f"  [{i+1}/{total}] {done} 条已写入 | {elapsed:.1f}s")
    
    elapsed = time.time() - t0
    print("-" * 60)
    print(f"[DONE] 成功写入 {done} 条对话记录到 PostgreSQL")
    print(f"耗时：{elapsed:.1f}s（平均 {(elapsed/done):.3f}s/条）" if done else "")
    print(f"用户：{USER_ID}")
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 验证
    print("\n[VERIFY] 查一下就几条记录验证：")
    from agent.runtime_db import connect
    conn = connect()
    rows = conn.execute(
        "SELECT COUNT(*) as cnt FROM conversation_history WHERE user_id=%s",
        (USER_ID,)).fetchone()
    print(f"  conversation_history 表：{rows['cnt']} 条")
    rows = conn.execute(
        "SELECT COUNT(*) as cnt FROM sessions WHERE user_id=%s",
        (USER_ID,)).fetchone()
    print(f"  sessions 表：{rows['cnt']} 条")
    
    # 展示前 3 条
    print("\n样本展示（前 3 条）：")
    rows = conn.execute(
        "SELECT user_message, bot_reply, intent, timestamp FROM conversation_history WHERE user_id=%s ORDER BY id ASC LIMIT 3",
        (USER_ID,)).fetchall()
    for r in rows:
        print(f"  USER:   {r['user_message'][:60]}...")
        print(f"  BOT:    {r['bot_reply'][:60]}...")
        print(f"  INTENT: {r['intent']} | TIME: {r['timestamp']}")
        print()
    conn.close()

if __name__ == "__main__":
    main()