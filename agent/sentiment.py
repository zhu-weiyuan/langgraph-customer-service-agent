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

# ── Lightweight keyword-based fallback (avoids LLM call when obvious) ──
_KEYWORDS = {
    "angry": [
        "垃圾", "太差", "操你", "傻逼", "废物", "骗子", "黑心", "坑人",
        "愤怒", "气死", "恶心", "无语", "受够了", "滚蛋", "去死",
        "投诉", "举报", "维权", "差评", "退款", "退货",
    ],
    "sad": [
        "失望", "难过", "伤心", "可怜", "无助", "绝望", "心碎",
        "不好用", "没用", "白买了", "浪费钱", "后悔",
    ],
    "anxious": [
        "着急", "急死", "快点", "紧急", "怎么办", "救命", "来不及",
        "坏了", "不能用", "开不了机", "连不上", "闪退",
    ],
    "happy": [
        "太好了", "很棒", "喜欢", "满意", "好用", "赞", "给力",
        "谢谢", "感谢", "不错", "很好", "完美", "开心",
    ],
}


def _keyword_sentiment(text: str) -> Optional[Dict[str, Any]]:
    """Fast keyword-based emotion detection. Returns None if inconclusive."""
    scores = {"angry": 0, "sad": 0, "anxious": 0, "happy": 0}
    for emotion, words in _KEYWORDS.items():
        for word in words:
            if word in text:
                scores[emotion] += 1

    max_emotion = max(scores, key=scores.get)
    max_score = scores[max_emotion]

    # Only trust keyword match if at least 2 hits or 1 hit with strong words
    if max_score >= 2:
        intensity = min(5, max_score + 1)
        return {"emotion": max_emotion, "intensity": intensity}
    elif max_score == 1:
        # Single hit — only trust for very strong keywords
        strong_words = {"操你", "傻逼", "去死", "滚蛋", "救命", "紧急", "完美"}
        for word in _KEYWORDS.get(max_emotion, []):
            if word in text and word in strong_words:
                return {"emotion": max_emotion, "intensity": 4}

    return None


# Cache for recent sentiment results
_sentiment_cache = {}


from .llm_client import get_llm_client


def _call_llm_json(messages, system: str) -> dict:
    """Call LLM through shared client."""
    result = get_llm_client().chat_json(messages, system, max_tokens=64)
    if not result:
        return {"emotion": "neutral", "intensity": 1}
    return result


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

    # Fast path: keyword-based detection (avoids LLM call when obvious)
    kw_result = _keyword_sentiment(text)
    if kw_result:
        _sentiment_cache[ck] = kw_result
        return kw_result

    # Slow path: LLM-based classification
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
