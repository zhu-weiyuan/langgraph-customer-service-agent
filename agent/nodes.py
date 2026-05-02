"""
节点函数 - LangGraph 智能客服 Agent (Real LLM via local llama.cpp)

New flow:
1. User asks question → bot replies (NO satisfaction check)
2. User continues asking → bot keeps answering
3. User signals ending ("bye", "thanks", "good") → THEN ask satisfaction
4. Satisfied → closing; Not satisfied → retry or escalate
"""

from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, AIMessage

LLM_API_URL = "http://127.0.0.1:8080/v1/chat/completions"
LLM_API_KEY = "your_key_here"

SYSTEM_PROMPT = """You are a professional customer service assistant for "SmartLink Tech".
Company products: smart speakers, smart home kits, cloud services.

Your duties:
1. Friendly answers to product questions
2. Handle complaints with patience and empathy
3. For casual chat, respond politely and guide to product topics

Response rules:
- Reply in Chinese (中文)
- Natural, warm tone like a real human agent
- Give targeted answers based on user's specific question
- If user complains, apologize first then solve
- If unsure, honestly say you don't know
- Keep responses concise and helpful"""


def _call_llm(messages: List[dict], system: str = SYSTEM_PROMPT, max_tokens: int = 512) -> str:
    """Call local llama.cpp via HTTP API."""
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
        print(f"[LLM Error] {e}")
        return "Sorry, I'm having trouble processing your request. Please try again later."


def _call_llm_json(messages: List[dict], system: str, max_tokens: int = 256) -> dict:
    """Call local llama.cpp and parse JSON response."""
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
        print(f"[LLM JSON Error] {e}")
        return {"intent": "consult"}


# ============================================================
# Intent identification (includes end-of-conversation detection)
# ============================================================

INTENT_SYSTEM = """You are an intent classifier. Analyze the user message and classify it.

Return strict JSON only:
{"intent": "type", "ending": false}

Intent types:
- "consult": product questions, feature inquiries, tech support
- "complaint": complaints, grievances, refunds, returns, bad reviews
- "chat": greetings, casual chat ("hello", "hi", "how are you")
- "ending": user wants to end the conversation ("bye", "thanks", "good", "that's all", "再见", "谢谢", "好了", "没问题了")

The "ending" field is true ONLY if intent is "ending". Otherwise false.
Be careful: "谢谢" alone = ending. "谢谢但是..." = not ending (still has a question)."""


def identify_intent(state: Dict[str, Any]) -> Dict[str, Any]:
    """Classify user intent using local LLM."""
    messages = state.get('messages', [])
    user_message = ''
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            user_message = msg.content
            break

    if not user_message:
        return {'intent': 'consult', 'ending': False}

    result = _call_llm_json(
        [{"role": "user", "content": user_message}],
        INTENT_SYSTEM
    )

    intent = result.get('intent', 'consult')
    ending = result.get('ending', False)
    print(f"[Intent] '{user_message}' -> {intent}, ending={ending}")
    return {'intent': intent, 'ending': ending}


# ============================================================
# Reply generation
# ============================================================

def generate_reply(state: Dict[str, Any]) -> Dict[str, Any]:
    """Generate natural conversational reply using local LLM."""
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

    if retry_count > 0:
        extra = f"\n\nImportant: The user was not satisfied with your previous answer. Please rephrase and try a different approach. This is retry #{retry_count}."
    else:
        extra = ""

    reply = _call_llm(context_messages, SYSTEM_PROMPT + extra, max_tokens=512)
    ai_message = AIMessage(content=reply)
    print(f"[Reply] intent={intent}, retry={retry_count}")
    return {'messages': [ai_message], 'bot_reply': reply}


# ============================================================
# Satisfaction check (only called at end of conversation)
# ============================================================

def check_satisfaction(state: Dict[str, Any]) -> Dict[str, Any]:
    """Ask user for satisfaction feedback at conversation end."""
    prompt = _call_llm(
        [{"role": "user", "content": "Please ask the user if they are satisfied with this service in a natural, friendly way. Keep it brief."}],
        SYSTEM_PROMPT + "\nOnly generate the satisfaction question.",
        max_tokens=80
    )

    ai_message = AIMessage(content=prompt)
    print(f"[Satisfaction Check] Asking user")
    return {'messages': [ai_message]}


# ============================================================
# Process satisfaction feedback
# ============================================================

SATISFACTION_SYSTEM = """You are a satisfaction judge. Based on the user's reply, determine if they are satisfied.

Return strict JSON only:
{"satisfied": true} or {"satisfied": false}

Rules:
- "满意", "好", "可以", "OK", "谢谢", "great", "good", "fine" -> true
- "不满意", "不好", "不行", "一般", continued complaints -> false
- If user asks a NEW question (not answering satisfaction) -> false"""


def process_satisfaction(state: Dict[str, Any]) -> Dict[str, Any]:
    """Use LLM to judge if user is satisfied."""
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
        [{"role": "user", "content": f"User reply: {user_message}\nDetermine if they are satisfied with the service."}],
        SATISFACTION_SYSTEM
    )

    satisfaction = judge_result.get('satisfied', False)
    new_retry = retry_count + (0 if satisfaction else 1)
    print(f"[Process Satisfaction] '{user_message}' -> satisfied={satisfaction}, retries={new_retry}")
    return {'satisfaction': satisfaction, 'retry_count': new_retry}


# ============================================================
# Escalate to human
# ============================================================

def escalate_to_human(state: Dict[str, Any]) -> Dict[str, Any]:
    """Escalate to human agent using interrupt."""
    from langgraph.types import interrupt

    session_id = state.get('session_id', 'unknown')
    intent = state.get('intent', 'unknown')
    retry_count = state.get('retry_count', 0)

    print(f"[Escalate] Upgrading! session={session_id}, intent={intent}, retries={retry_count}")

    human_response = interrupt({
        "type": "human_intervention_required",
        "message": "Human agent intervention needed",
        "session_id": session_id,
        "context": {
            "intent": intent,
            "retry_count": retry_count,
            "last_user_message": [m.content for m in state.get('messages', []) if isinstance(m, HumanMessage)][-1] if state.get('messages') else None
        }
    })

    if human_response:
        return {
            'messages': [AIMessage(content=f"[Human Agent]: {human_response}")],
            'escalate': False
        }
    return {'escalate': True}


# ============================================================
# Finalize
# ============================================================

def finalize(state: Dict[str, Any]) -> Dict[str, Any]:
    """Generate natural closing message using LLM."""
    closing = _call_llm(
        [{"role": "user", "content": "Please naturally end this customer service conversation with thanks and warm wishes."}],
        SYSTEM_PROMPT + "\nOnly generate the closing message, brief and warm.",
        max_tokens=100
    )

    ai_message = AIMessage(content=closing)
    print(f"[Finalize] Conversation ended")
    return {'messages': [ai_message]}
