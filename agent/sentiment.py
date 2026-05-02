"""
Sentiment Analysis Module

Detects user emotion from their messages and adjusts bot tone accordingly.
Uses LLM-based classification (same local llama.cpp) for accuracy with Chinese text.

Emotion categories:
- neutral: Normal conversation
- angry: Frustrated, complaining strongly
- sad: Disappointed, helpless
- anxious: Worried about something not working
- happy: Satisfied, positive
"""

from typing import Dict, Any, Optional

# Sentiment classification prompt
SENTIMENT_SYSTEM = """你是一个情感分析器。分析用户消息的情绪，返回严格的 JSON（不要其他文字）：
{"emotion": "情绪类型", "intensity": 强度}

情绪类型：
- "neutral"：中性、普通对话
- "angry"：愤怒、强烈不满
- "sad"：失望、无助
- "anxious"：焦虑、担心（如设备坏了很着急）
- "happy"：开心、满意

强度（1-5）：
1=非常轻微, 2=轻微, 3=中等, 4=强烈, 5=非常强烈"""

# Cache for recent sentiment results
_sentiment_cache = {}


def _call_llm_json(messages, system: str) -> dict:
    """Call local LLM and parse JSON response (imported from nodes to avoid circular)."""
    import urllib.request
    import json

    LLM_API_URL = "http://127.0.0.1:8080/v1/chat/completions"
    LLM_API_KEY = "your_key_here"

    payload = {
        "messages": [{"role": "system", "content": system}] + messages,
        "max_tokens": 64,
        "temperature": 0.1,
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            text = result["choices"][0]["message"]["content"].strip()
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue
            return {"emotion": "neutral", "intensity": 1}
    except Exception as e:
        print(f"[Sentiment LLM Error] {e}")
        return {"emotion": "neutral", "intensity": 1}


def analyze(text: str, cache_key: Optional[str] = None) -> Dict[str, Any]:
    """Analyze the sentiment of a user message.

    Args:
        text: User's message text
        cache_key: Optional cache key (e.g., session_id + message hash)

    Returns:
        {"emotion": str, "intensity": int}
    """
    ck = cache_key or text[:50]
    if ck in _sentiment_cache:
        return _sentiment_cache[ck]

    result = _call_llm_json(
        [{"role": "user", "content": f"分析这句话的情绪：{text}"}],
        SENTIMENT_SYSTEM
    )

    emotion = result.get("emotion", "neutral")
    intensity = int(result.get("intensity", 1))

    _sentiment_cache[ck] = {"emotion": emotion, "intensity": intensity}
    return {"emotion": emotion, "intensity": intensity}


def get_tone_adjustment(emotion: str, intensity: int) -> str:
    """Generate tone adjustment instructions based on detected emotion.

    Args:
        emotion: Detected emotion type
        intensity: Emotion intensity (1-5)

    Returns:
        String to append to system prompt for tone adjustment
    """
    adjustments = {
        "angry": {
            (1, 2): "注意：用户有些不满，请语气更加谦逊有礼。",
            (3, 4): "注意：用户比较生气，请先诚恳道歉，表达理解，再耐心解决问题。",
            (5,): "注意：用户非常愤怒！请先真诚道歉，表达充分理解和同情，避免任何推卸责任的措辞，优先给出解决方案。",
        },
        "sad": {
            (1, 2): "注意：用户有些失落，请语气温和，给予鼓励。",
            (3, 4): "注意：用户感到失望，请先表达理解和同情，再积极提供帮助。",
            (5,): "注意：用户非常沮丧，请先温暖安慰，让对方感受到被重视，再一步步协助解决。",
        },
        "anxious": {
            (1, 2): "注意：用户有些担心，请给出明确的步骤和预期结果。",
            (3, 4): "注意：用户比较着急，请直接给出解决方案，减少废话，让用户放心。",
            (5,): "注意：用户非常焦虑！请立即给出清晰的解决步骤和时间预期，让对方知道问题可以解决。",
        },
        "happy": {
            (1, 2): "注意：用户心情不错，可以轻松愉快地交流。",
            (3, 4): "注意：用户很开心，继续保持热情友好的态度。",
            (5,): "注意：用户非常高兴！可以活泼一些，适当表达开心。",
        },
    }

    if emotion == "neutral":
        return ""

    emotion_adjustments = adjustments.get(emotion, {})
    for intensity_range, instruction in emotion_adjustments.items():
        if intensity in intensity_range:
            return f"\n\n{instruction}"

    return ""


def clear_cache():
    """Clear sentiment cache."""
    _sentiment_cache.clear()
