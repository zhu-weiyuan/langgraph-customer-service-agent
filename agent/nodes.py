"""
节点函数 - LangGraph 智能客服 Agent (本地 LLM via llama.cpp)

新流程：
1. 用户提问 → bot 回复（不问满意度）
2. 用户继续问 → bot 继续回答
3. 用户表示结束（"再见"、"谢谢"、"好了"）→ 才问满意度
4. 满意 → 结束语；不满意 → 重试或转人工
"""

from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, AIMessage

# RAG integration
from .rag import build_context as rag_build_context

# Sentiment analysis integration
from .sentiment import analyze as sentiment_analyze, get_tone_adjustment

# Multi-turn memory
from .memory import (
    save_conversation,
    build_memory_context,
    mark_resolved,
)

LLM_API_URL = "http://127.0.0.1:8080/v1/chat/completions"
LLM_API_KEY = "your_key_here"

SYSTEM_PROMPT = """你是一个专业的智能客服助手，服务于"智联科技"公司。
公司产品：智能音箱、智能家居套装、云服务。

你的职责：
1. 友好地回答用户关于产品的咨询
2. 处理用户投诉，保持耐心和同理心
3. 如果是闲聊，礼貌回应并引导到产品话题

回复要求：
- 用中文回复
- 语气自然、亲切，像一个真人客服
- 根据用户的问题给出有针对性的回答，不要模板化
- 如果用户投诉，先道歉再解决问题
- 如果不确定，诚实说不知道，不要编造信息
- 优先使用下方参考资料中的信息回答产品相关问题"""

# RAG-enhanced system prompt template
RAG_SYSTEM_PROMPT_TEMPLATE = SYSTEM_PROMPT + """

{rag_context}
请基于以上参考资料回答用户问题。如果参考资料中没有相关信息，诚实说明你不确定。"""


def _call_llm(messages: List[dict], system: str = SYSTEM_PROMPT, max_tokens: int = 512) -> str:
    """调用本地 llama.cpp HTTP API。"""
    import urllib.request
    import json

    payload = {
        "messages": [{"role": "system", "content": system}] + messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": False,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LLM_API_URL,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[LLM 错误] {e}")
        return "抱歉，我暂时无法处理您的请求，请稍后再试。"


def _call_llm_json(messages: List[dict], system: str, max_tokens: int = 256) -> dict:
    """调用本地 llama.cpp 并解析 JSON 响应。"""
    import urllib.request
    import json

    payload = {
        "messages": [{"role": "system", "content": system}] + messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": False,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LLM_API_URL,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            text = result["choices"][0]["message"]["content"].strip()
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue
            return {"intent": "consult"}
    except Exception as e:
        print(f"[LLM JSON 错误] {e}")
        return {"intent": "consult"}


# ============================================================
# 意图识别（包含对话结束检测）
# ============================================================

INTENT_SYSTEM = """你是一个意图分类器。分析用户消息，判断其意图。

返回严格的 JSON 格式（不要任何其他文字）：
{"intent": "类型", "ending": false}

意图类型：
- "consult"：产品咨询、功能询问、技术支持
- "complaint"：投诉、抱怨、退款、退货、差评
- "chat"：打招呼、闲聊（如"你好"、"hi"、"在吗"）
- "ending"：用户想结束对话（"再见"、"谢谢"、"好了"、"没问题了"、"bye"、"thanks"、"that's all"）

"ending" 字段只有在 intent 为 "ending" 时才为 true，否则为 false。
注意："谢谢"单独出现 = ending；"谢谢但是..." = 不是 ending（还有问题）。"""


def identify_intent(state: Dict[str, Any]) -> Dict[str, Any]:
    """意图识别节点 — 使用本地 LLM 进行意图分类 + 情感分析。"""
    messages = state.get('messages', [])
    user_message = ''
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            user_message = msg.content
            break

    if not user_message:
        return {'intent': 'consult', 'ending': False, 'emotion': 'neutral', 'emotion_intensity': 1}

    # Intent classification
    result = _call_llm_json(
        [{"role": "user", "content": user_message}],
        INTENT_SYSTEM
    )

    intent = result.get('intent', 'consult')
    ending = result.get('ending', False)

    # Sentiment analysis (lightweight — cached per message)
    sentiment = sentiment_analyze(user_message, cache_key=state.get('session_id', '') + user_message[:30])
    emotion = sentiment.get('emotion', 'neutral')
    intensity = sentiment.get('intensity', 1)

    print(f"[意图识别] '{user_message}' → {intent}, 结束={ending}")
    if emotion != 'neutral':
        print(f"[情感分析] {emotion} (强度 {intensity}/5)")

    return {
        'intent': intent,
        'ending': ending,
        'emotion': emotion,
        'emotion_intensity': intensity,
    }


# ============================================================
# 回复生成
# ============================================================

def generate_reply(state: Dict[str, Any]) -> Dict[str, Any]:
    """生成回复节点 — 使用本地 LLM + RAG 生成自然对话回复。"""
    intent = state.get('intent', 'consult')
    retry_count = state.get('retry_count', 0)

    messages = state.get('messages', [])
    context_messages = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            context_messages.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            context_messages.append({"role": "assistant", "content": msg.content})

    context_messages = context_messages[-12:]

    # --- RAG: retrieve relevant knowledge for the latest user message ---
    rag_context = ""
    if intent == 'consult':
        latest_user = ''
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                latest_user = msg.content
                break
        if latest_user:
            rag_context = rag_build_context(latest_user)
            if rag_context:
                print(f"[RAG] 找到 {rag_context.count('###')} 条相关知识")

    # Build system prompt with RAG context + memory + sentiment tone adjustment
    if rag_context:
        system_prompt = RAG_SYSTEM_PROMPT_TEMPLATE.format(rag_context=rag_context)
    else:
        system_prompt = SYSTEM_PROMPT

    # Multi-turn memory: inject user context
    session_id = state.get('session_id', '')
    if session_id:
        memory_ctx = build_memory_context(session_id)
        if memory_ctx:
            system_prompt = system_prompt + memory_ctx

    # Sentiment-based tone adjustment
    emotion = state.get('emotion', 'neutral')
    intensity = state.get('emotion_intensity', 1)
    tone_adj = get_tone_adjustment(emotion, intensity)
    system_prompt = system_prompt + tone_adj

    if retry_count > 0:
        extra = f"\n\n注意：用户之前表示不满意，请用不同的方式重新回答。这是第 {retry_count} 次重试。"
    else:
        extra = ""

    reply = _call_llm(context_messages, system_prompt + extra, max_tokens=512)
    ai_message = AIMessage(content=reply)
    print(f"[生成回复] intent={intent}, retry={retry_count}, rag={'yes' if rag_context else 'no'}")

    # Save to memory
    if session_id:
        latest_user = ''
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                latest_user = msg.content
                break
        if latest_user:
            save_conversation(
                session_id=session_id,
                user_message=latest_user,
                bot_reply=reply,
                intent=intent,
                emotion=state.get('emotion', 'neutral'),
                emotion_intensity=state.get('emotion_intensity', 1),
            )

    return {'messages': [ai_message], 'bot_reply': reply}


# ============================================================
# 满意度检查（仅在对话结束时调用）
# ============================================================

def check_satisfaction(state: Dict[str, Any]) -> Dict[str, Any]:
    """满意度检查节点 — 使用 LLM 生成自然的满意度询问。"""
    prompt = _call_llm(
        [{"role": "user", "content": "请自然地询问用户是否满意刚才的服务，简短友好"}],
        SYSTEM_PROMPT + "\n只生成满意度询问的话，不要回复其他内容。",
        max_tokens=80
    )

    ai_message = AIMessage(content=prompt)
    print(f"[满意度检查] 询问用户")
    return {'messages': [ai_message]}


# ============================================================
# 处理满意度反馈
# ============================================================

SATISFACTION_SYSTEM = """你是一个满意度判断器。根据用户回复判断是否满意。

返回严格的 JSON（不要其他文字）：
{"satisfied": true} 或 {"satisfied": false}

规则：
- "满意"、"好"、"可以"、"OK"、"谢谢" → true
- "不满意"、"不好"、"不行"、"一般"、继续抱怨 → false
- 如果用户提出了新问题（不是回答满意度）→ false"""


def process_satisfaction(state: Dict[str, Any]) -> Dict[str, Any]:
    """处理满意度反馈节点 — 使用 LLM 判断用户是否满意。"""
    messages = state.get('messages', [])
    retry_count = state.get('retry_count', 0)

    user_message = ''
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            user_message = msg.content
            break

    if not user_message:
        return {'satisfaction': None, 'retry_count': retry_count}

    judge_result = _call_llm_json(
        [{"role": "user", "content": f"用户回复：{user_message}\n判断用户对服务是否满意。"}],
        SATISFACTION_SYSTEM
    )

    satisfaction = judge_result.get('satisfied', False)
    new_retry = retry_count + (0 if satisfaction else 1)
    print(f"[处理满意度] '{user_message}' → 满意={satisfaction}, 重试={new_retry}")
    return {'satisfaction': satisfaction, 'retry_count': new_retry}


# ============================================================
# 转人工
# ============================================================

def escalate_to_human(state: Dict[str, Any]) -> Dict[str, Any]:
    """转人工节点 — 使用 interrupt 挂起等待人工介入。"""
    from langgraph.types import interrupt

    session_id = state.get('session_id', 'unknown')
    intent = state.get('intent', 'unknown')
    retry_count = state.get('retry_count', 0)

    print(f"[转人工] 问题升级！session={session_id}, intent={intent}, retries={retry_count}")

    human_response = interrupt({
        "type": "human_intervention_required",
        "message": "需要人工客服介入处理",
        "session_id": session_id,
        "context": {
            "intent": intent,
            "retry_count": retry_count,
            "last_user_message": [m.content for m in state.get('messages', []) if isinstance(m, HumanMessage)][-1] if state.get('messages') else None
        }
    })

    if human_response:
        return {
            'messages': [AIMessage(content=f"[人工客服]: {human_response}")],
            'escalate': False
        }
    return {'escalate': True}


# ============================================================
# 结束对话
# ============================================================

def finalize(state: Dict[str, Any]) -> Dict[str, Any]:
    """结束对话节点 — 使用 LLM 生成自然的结束语。"""
    session_id = state.get('session_id', '')

    closing = _call_llm(
        [{"role": "user", "content": "请自然地结束这次客服对话，表达感谢和祝福"}],
        SYSTEM_PROMPT + "\n只生成结束语，简短温暖。",
        max_tokens=100
    )

    ai_message = AIMessage(content=closing)

    # Mark all issues as resolved when session ends positively
    if session_id:
        mark_resolved(session_id)

    print(f"[结束对话] 服务完成")
    return {'messages': [ai_message]}
