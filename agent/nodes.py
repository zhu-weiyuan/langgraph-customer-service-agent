"""
节点函数 - LangGraph 智能客服 Agent

LangGraph 的核心概念：
- 节点（Node）是图中的处理单元，每个节点是一个可调用的函数
- 节点接收当前状态，返回要更新的状态字段（字典形式）
- 节点之间通过边（edges）连接，形成有向图
- conditional edges 允许根据条件动态选择下一个节点

Mock LLM 响应：
- 这里使用模拟数据代替真实 LLM 调用，方便测试和调试
- 实际项目中可以替换为 OpenAI、Anthropic 等 API 调用
"""

from typing import Dict, Any
from langchain_core.messages import HumanMessage, AIMessage
import random

# ============================================================
# Mock LLM 响应库（模拟真实 LLM 行为）
# ============================================================

INTENT_RESPONSES = {
    'consult': [
        "您好！请问您想了解哪方面的产品信息？我可以为您介绍功能、使用方法或技术支持。",
        "欢迎咨询！我们的主要产品包括智能音箱、智能家居套装和云服务。您对哪个感兴趣？",
        "好的，我来帮您解答。我们的产品支持语音控制、APP 远程操作和自动化场景设置。"
    ],
    'complaint': [
        "非常抱歉给您带来不好的体验。能否详细描述一下遇到的问题？我会尽快帮您解决。",
        "理解您的 frustration。请提供订单号或产品型号，我将为您查询处理方案。",
        "感谢您的反馈。我们非常重视用户意见，已记录您的问题并会优先处理。"
    ],
    'chat': [
        "哈哈，您真幽默！有什么我可以帮您的吗？",
        "聊天气氛不错 😄 不过如果您有产品问题，我随时可以帮忙哦！",
        "好的好的，闲聊也很开心～但别忘了我的主业是客服，有问题尽管问！"
    ]
}

RETRY_RESPONSES = [
    "让我换个方式解释一下。我们的产品设计简洁易用，首次使用建议：1) 下载 APP 2) 扫码配对设备 3) 语音说'你好小智'即可唤醒。这样清楚了吗？",
    "抱歉之前的回答不够清楚。我来补充说明：产品支持 WiFi 和蓝牙双连接，兼容 iOS 和 Android，7×24 小时在线客服。还有什么想了解的吗？",
    "我再详细说明一下。核心功能包括：语音助手、智能家居控制、音乐播放、天气查询。您最关心哪个功能？我可以深入介绍。"
]


def identify_intent(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    意图识别节点

    分析用户消息，判断是咨询、投诉还是闲聊。
    实际项目中这里会调用 LLM 或 NLP 模型进行意图分类。

    Args:
        state: 当前图状态

    Returns:
        更新后的状态（包含 intent 字段）
    """
    # 获取用户最后一条消息
    messages = state.get('messages', [])
    user_message = ''
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            user_message = msg.content.lower()
            break

    # Mock 意图识别逻辑（基于关键词）
    complaint_keywords = ['投诉', '问题', '故障', '不好', '差评', '退款', '退货', 'bug', '错误']
    chat_keywords = ['你好', 'hello', 'hi', '嗨', '哈哈', '笑话', '聊天', '无聊']

    intent = 'consult'  # 默认咨询

    for keyword in complaint_keywords:
        if keyword in user_message:
            intent = 'complaint'
            break

    if intent == 'consult':
        for keyword in chat_keywords:
            if keyword in user_message and len(user_message) < 10:
                intent = 'chat'
                break

    print(f"[意图识别] 用户消息: '{user_message}' → 意图: {intent}")

    return {'intent': intent}


def generate_reply(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    生成回复节点

    根据识别的意图，生成相应的客服回复。
    实际项目中这里会调用 LLM 生成个性化回复。

    Args:
        state: 当前图状态

    Returns:
        更新后的状态（包含 bot_reply 和 AI 消息）
    """
    intent = state.get('intent', 'consult')
    retry_count = state.get('retry_count', 0)

    # 如果是重试，使用专门的回复模板
    if retry_count > 0:
        reply = random.choice(RETRY_RESPONSES)
    else:
        reply = random.choice(INTENT_RESPONSES.get(intent, INTENT_RESPONSES['consult']))

    # 创建 AI 消息并添加到消息历史
    ai_message = AIMessage(content=reply)

    print(f"[生成回复] 意图: {intent}, 重试次数: {retry_count}")
    print(f"[生成回复] 回复内容: {reply[:50]}...")

    return {
        'messages': [ai_message],
        'bot_reply': reply
    }


def check_satisfaction(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    满意度检查节点

    询问用户是否满意，并等待用户回复。
    实际项目中这里会插入一个等待用户输入的步骤。

    Args:
        state: 当前图状态

    Returns:
        更新后的状态（提示用户评价）
    """
    satisfaction_prompt = "请问您对这次服务满意吗？请回复'满意'或'不满意'。"
    ai_message = AIMessage(content=satisfaction_prompt)

    print(f"[满意度检查] 询问用户满意度")

    return {
        'messages': [ai_message]
    }


def process_satisfaction(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    处理满意度反馈节点

    解析用户对满意度的回复，更新状态。

    Args:
        state: 当前图状态

    Returns:
        更新后的状态（包含 satisfaction 字段）
    """
    messages = state.get('messages', [])
    retry_count = state.get('retry_count', 0)

    # 获取用户最后一条消息
    user_message = ''
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            user_message = msg.content.lower()
            break

    # 判断满意度
    if '满意' in user_message or '好' in user_message or 'ok' in user_message:
        satisfaction = True
        print(f"[处理满意度] 用户满意！")
    else:
        satisfaction = False
        print(f"[处理满意度] 用户不满意，重试次数: {retry_count}")

    return {
        'satisfaction': satisfaction,
        'retry_count': retry_count + 1
    }


def escalate_to_human(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    转人工节点 - 使用 interrupt 挂起等待人工介入

    LangGraph 的 interrupt 机制：
    - 当需要人工介入时，可以使用 interrupt 暂停图的执行
    - 图会保持当前状态，等待外部恢复
    - 恢复时可以注入新的消息或状态更新
    - 适用于复杂问题、投诉处理、敏感操作等场景

    Args:
        state: 当前图状态

    Returns:
        永远不会正常返回（会被 interrupt 挂起）
    """
    from langgraph.types import interrupt, Command

    # 生成转人工提示
    escalate_message = (
        "🚨 已转接人工客服\n\n"
        "由于您的问题较为复杂，已为您转接人工客服。\n"
        "客服专员将在 1-3 分钟内接入对话。\n"
        "请稍候..."
    )

    ai_message = AIMessage(content=escalate_message)

    print(f"[转人工] 问题升级！会话将被挂起等待人工处理")
    print(f"[转人工] 会话 ID: {state.get('session_id', 'unknown')}")

    # interrupt 会暂停图的执行，返回当前状态
    # 外部可以通过 Command 恢复会话
    human_response = interrupt({
        "type": "human_intervention_required",
        "message": "需要人工客服介入处理",
        "session_id": state.get('session_id'),
        "context": {
            "intent": state.get('intent'),
            "retry_count": state.get('retry_count'),
            "last_user_message": [m.content for m in state.get('messages', []) if isinstance(m, HumanMessage)][-1] if state.get('messages') else None
        }
    })

    # 如果提供了人工回复，继续处理
    if human_response:
        human_message = AIMessage(content=f"[人工客服]: {human_response}")
        return {
            'messages': [human_message],
            'escalate': False
        }

    # 否则保持挂起状态
    return {'escalate': True}


def finalize(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    结束对话节点

    发送结束语，完成本次服务。

    Args:
        state: 当前图状态

    Returns:
        更新后的状态（包含结束语）
    """
    closing_message = (
        "感谢您的咨询！如果您还有其他问题，随时欢迎联系我们。\n"
        "祝您生活愉快！😊"
    )

    ai_message = AIMessage(content=closing_message)

    print(f"[结束对话] 服务完成")

    return {
        'messages': [ai_message]
    }
