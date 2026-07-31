"""
Sentiment Analysis Module (Enhanced)

Detects user emotion from their messages and adjusts bot tone accordingly.
Uses LLM-based classification (same local llama.cpp) for accuracy with Chinese text.

Emotion categories:
- neutral: Normal conversation
- angry: Frustrated, complaining strongly
- sad: Disappointed, helpless
- anxious: Worried about something not working
- happy: Satisfied, positive

Enhanced Features:
1. Context-aware intensity calibration (based on recent conversation history)
2. Multi-turn emotion tracking (detects trends like anxious → angry)
3. Emotion-driven reply strategies (different RAG retrieval per emotion)
"""

import time
from typing import Dict, Any, Optional, List
from collections import deque

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
        "垃圾", "太差", "操你", "傻逼", "废物", "骗子", "黑心", "坑人", "愤怒", "气死", "恶心", "无语",
        "受够了", "滚蛋", "去死", "投诉", "举报", "维权", "差评", "我要退款", "要求退款", "不给退款",
        "退款失败", "为什么还不退款", "很生气",
        "气死我了"],
    "sad": [
        "失望", "难过", "伤心", "可怜", "无助", "绝望", "心碎", "不好用", "没用", "白买了", "浪费钱", "后悔",
        "太失望",
        "真的后悔"],
    "anxious": [
        "着急", "急死", "快点", "紧急", "怎么办", "救命", "来不及", "坏了", "不能用", "开不了机", "连不上", "闪退",
        "登录不上", "多久到账", "还没到账", "一直亮", "怎么找回",
    ],
    "happy": [
        "太好了", "很棒", "喜欢", "满意", "好用", "赞", "给力", "谢谢", "感谢", "不错", "很好", "完美", "开心",
        "解决了", "再见", "没事了",
    ],
}
_STRONG_KEYWORDS = {
    "angry": {"操你", "傻逼", "去死", "滚蛋", "垃圾", "太差", "坑人", "气死", "投诉", "我要退款", "要求退款", "不给退款", "为什么还不退款", "很生气", "气死我了"},
    "sad": {"失望", "难过", "伤心", "无助", "绝望", "后悔", "白买了", "太失望", "真的后悔"},
    "anxious": {"救命", "紧急", "急死", "着急", "怎么办", "连不上", "不能用", "登录不上", "多久到账", "还没到账", "怎么找回"},
    "happy": {"太好了", "满意", "谢谢", "感谢", "完美", "开心", "解决了", "很好", "好用", "再见", "没事了"},
}


def _keyword_sentiment(text: str) -> Optional[Dict[str, Any]]:
    """关键词情绪快判：高置信单词立即生效，降低本地 LLM JSON 失败带来的误判。"""
    text = text or ""
    scores = {"angry": 0, "sad": 0, "anxious": 0, "happy": 0}
    strongest: Dict[str, int] = {emotion: 0 for emotion in scores}
    for emotion, words in _KEYWORDS.items():
        for word in words:
            if word in text:
                # 较长短语比单字/双字更具辨识度，例如“多久到账”优先于泛化的“退款”。
                weight = 2 if len(word) >= 3 else 1
                if word in _STRONG_KEYWORDS[emotion]:
                    # Explicit but short wording (for example anger or regret)
                    # must not lose to a generic operational problem phrase.
                    weight = max(weight, 3)
                    strongest[emotion] = max(strongest[emotion], len(word))
                scores[emotion] += weight

    max_score = max(scores.values())
    candidates = [emotion for emotion, score in scores.items() if score == max_score]
    # 同分时优先选包含更长高置信短语的情绪；仍相同则保持稳定顺序。
    emotion = max(candidates, key=lambda item: strongest[item])
    if max_score >= 2 or strongest[emotion]:
        intensity = min(5, max(2, max_score + (1 if strongest[emotion] else 0)))
        return {"emotion": emotion, "intensity": intensity}
    return None

# Cache for recent sentiment results
_sentiment_cache = {}

# ── Multi-turn emotion tracking ────────────────────────────────
class EmotionTracker:
    """Tracks emotion trends across multiple conversation turns."""

    def __init__(self, max_history: int = 10):
        self.max_history = max_history
        self.history: deque = deque(maxlen=max_history)

    def add_emotion(self, session_id: str, emotion: str, intensity: int):
        """Record an emotion for a session."""
        self.history.append({
            "session_id": session_id,
            "emotion": emotion,
            "intensity": intensity,
            "timestamp": time.time()
        })

    def get_trend(self, session_id: str) -> Dict[str, Any]:
        """Analyze emotion trend for a session.

        Returns:
            {
                "current": current_emotion,
                "previous": previous_emotion (if any),
                "trend": "improving" | "worsening" | "stable" | "unknown",
                "avg_intensity": average intensity over recent turns
            }
        """
        session_history = [h for h in self.history if h["session_id"] == session_id]

        if not session_history:
            return {
                "current": "neutral",
                "previous": None,
                "trend": "unknown",
                "avg_intensity": 0
            }

        current = session_history[-1]
        previous = session_history[-2] if len(session_history) > 1 else None

        # Calculate trend based on intensity changes
        if len(session_history) >= 3:
            recent_intensities = [h["intensity"] for h in session_history[-3:]]
            avg_intensity = sum(recent_intensities) / len(recent_intensities)

            # Check if intensities are increasing or decreasing
            if all(recent_intensities[i] <= recent_intensities[i+1] for i in range(len(recent_intensities)-1)):
                trend = "worsening"
            elif all(recent_intensities[i] >= recent_intensities[i+1] for i in range(len(recent_intensities)-1)):
                trend = "improving"
            else:
                trend = "stable"
        else:
            avg_intensity = current["intensity"]
            trend = "unknown"

        return {
            "current": current["emotion"],
            "previous": previous["emotion"] if previous else None,
            "trend": trend,
            "avg_intensity": round(avg_intensity, 1)
        }

    def should_escalate(self, session_id: str) -> bool:
        """Determine if the conversation should be escalated based on emotion.

        Escalation criteria:
        - Current intensity >= 4 AND trend is worsening
        - OR multiple high-intensity emotions in recent history
        """
        session_history = [h for h in self.history if h["session_id"] == session_id]

        if not session_history:
            return False

        # Check current intensity
        current = session_history[-1]
        if current["intensity"] >= 4:
            trend = self.get_trend(session_id)["trend"]
            if trend == "worsening":
                return True

        # Check for repeated high-intensity emotions
        recent_high_intensity = sum(1 for h in session_history[-3:] if h["intensity"] >= 3)
        if recent_high_intensity >= 2:
            return True

        return False


# Global emotion tracker instance
_emotion_tracker = EmotionTracker()


def get_emotion_tracker() -> EmotionTracker:
    """Get the global emotion tracker instance."""
    return _emotion_tracker


from .llm_client import get_llm_client


def _call_llm_json(messages, system: str) -> dict:
    """Call LLM through shared client."""
    result = get_llm_client().chat_json(messages, system, max_tokens=64)
    if not result:
        return {"emotion": "neutral", "intensity": 1}
    return result


def analyze(text: str, session_id: Optional[str] = None, cache_key: Optional[str] = None) -> Dict[str, Any]:
    """Analyze the sentiment of a user message with context awareness.

    Args:
        text: User's message text
        session_id: Session ID for multi-turn tracking
        cache_key: Optional cache key (e.g., session_id + message hash)

    Returns:
        {"emotion": str, "intensity": int, "trend": str, "should_escalate": bool}
    """
    # 修复运算符优先级：旧写法 `cache_key or f"..." if session_id else text[:50]`
    # 解析为 `(cache_key or f"...") if session_id else text[:50]`，session_id 为空时
    # 显式传入的 cache_key 被直接忽略。加括号让 cache_key 始终优先生效。
    ck = cache_key or (f"{session_id}:{text[:50]}" if session_id else text[:50])
    if ck in _sentiment_cache:
        cached = _sentiment_cache[ck].copy()
        # Add trend info if session_id provided
        if session_id:
            trend_info = _emotion_tracker.get_trend(session_id)
            cached["trend"] = trend_info["trend"]
            cached["should_escalate"] = _emotion_tracker.should_escalate(session_id)
        return cached

    # Fast path: keyword-based detection (avoids LLM call when obvious)
    kw_result = _keyword_sentiment(text)
    if kw_result:
        emotion, intensity = kw_result["emotion"], kw_result["intensity"]
    else:
        # Slow path: LLM-based classification
        result = _call_llm_json(
            [{"role": "user", "content": f"分析这句话的情绪：{text}"}],
            SENTIMENT_SYSTEM
        )
        emotion = result.get("emotion", "neutral")
        intensity = int(result.get("intensity", 1))

    # Context-aware intensity calibration
    if session_id:
        trend_info = _emotion_tracker.get_trend(session_id)
        # If trend is worsening, increase intensity slightly
        if trend_info["trend"] == "worsening" and intensity < 5:
            intensity = min(5, intensity + 1)
        # If trend is improving, decrease intensity slightly
        elif trend_info["trend"] == "improving" and intensity > 1:
            intensity = max(1, intensity - 1)

    # Record in tracker
    _emotion_tracker.add_emotion(session_id or "anonymous", emotion, intensity)

    # Build result with trend info
    result = {
        "emotion": emotion,
        "intensity": intensity
    }

    if session_id:
        trend_info = _emotion_tracker.get_trend(session_id)
        result["trend"] = trend_info["trend"]
        result["should_escalate"] = _emotion_tracker.should_escalate(session_id)

    _sentiment_cache[ck] = result
    return result


def get_tone_adjustment(emotion: str, intensity: int, trend: str = "unknown") -> str:
    """Generate tone adjustment instructions based on detected emotion and trend.

    Args:
        emotion: Detected emotion type
        intensity: Emotion intensity (1-5)
        trend: Emotion trend ("improving", "worsening", "stable", "unknown")

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

    # Get base adjustment for emotion/intensity
    emotion_adjustments = adjustments.get(emotion, {})
    instruction = ""
    for intensity_range, adj in emotion_adjustments.items():
        if intensity in intensity_range:
            instruction = adj
            break

    # Add trend-based guidance
    if trend == "worsening":
        instruction += "\n注意：用户情绪正在恶化，请更加谨慎处理，优先解决问题。"
    elif trend == "improving":
        instruction += "\n注意：用户情绪正在好转，继续保持当前处理方式。"

    return f"\n\n{instruction}" if instruction else ""


def get_rag_strategy(emotion: str, intensity: int) -> Dict[str, Any]:
    """Get RAG retrieval strategy based on emotion.

    Different emotions require different information priorities:
    - Angry: Prioritize solutions and policies (quick resolution)
    - Anxious: Prioritize step-by-step guides and FAQs
    - Sad: Prioritize empathy and alternative options
    - Happy: Standard retrieval
    - Neutral: Standard retrieval

    Returns:
        {
            "priority": ["solutions", "policies", ...],  # Retrieval priority order
            "max_results": int,                           # Number of results to return
            "temperature": float                          # LLM temperature for generation
        }
    """
    strategies = {
        "angry": {
            (1, 2): {"priority": ["solutions", "policies"], "max_results": 3, "temperature": 0.5},
            (3, 4): {"priority": ["solutions", "escalation", "policies"], "max_results": 2, "temperature": 0.3},
            (5,): {"priority": ["escalation", "solutions"], "max_results": 1, "temperature": 0.2},
        },
        "anxious": {
            (1, 2): {"priority": ["guides", "faqs"], "max_results": 4, "temperature": 0.6},
            (3, 4): {"priority": ["step_by_step", "quick_fixes"], "max_results": 3, "temperature": 0.4},
            (5,): {"priority": ["immediate_solutions", "contact_info"], "max_results": 2, "temperature": 0.3},
        },
        "sad": {
            (1, 2): {"priority": ["alternatives", "empathy"], "max_results": 3, "temperature": 0.7},
            (3, 4): {"priority": ["empathy", "solutions"], "max_results": 2, "temperature": 0.5},
            (5,): {"priority": ["empathy", "escalation"], "max_results": 1, "temperature": 0.4},
        },
    }

    # Default strategy for happy/neutral or unknown emotions
    default = {"priority": ["standard"], "max_results": 3, "temperature": 0.7}

    emotion_strategies = strategies.get(emotion, {})
    for intensity_range, strategy in emotion_strategies.items():
        if intensity in intensity_range:
            return strategy

    return default


def clear_cache():
    """Clear sentiment cache."""
    _sentiment_cache.clear()
    _emotion_tracker.history.clear()
