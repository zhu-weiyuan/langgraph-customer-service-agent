"""
Dialogue Summary Module

自动生成服务工单记录。在对话结束时，将整段对话摘要为结构化工单。

输出格式：
{
    "ticket_id": "T-20260503-001",
    "customer_name": "...",
    "issue_category": "技术支持/产品咨询/投诉...",
    "description": "问题描述摘要",
    "resolution": "解决方案摘要",
    "satisfaction": "满意/不满意/未评价",
    "priority": "高/中/低",
    "created_at": "2026-05-03T03:00:00"
}
"""

import json
import uuid
from datetime import datetime
from typing import Dict, Any, List
from langchain_core.messages import HumanMessage, AIMessage

from .llm_client import get_llm_client

SUMMARY_SYSTEM = """你是一个客服工单生成器。根据以下对话记录，生成结构化的服务工单摘要。

返回严格的 JSON 格式（不要任何其他文字）：
{
    "issue_category": "问题分类",
    "description": "用户问题的简要描述（1-2句话）",
    "resolution": "解决方案或处理结果（1-2句话）",
    "priority": "high/medium/low"
}

分类选项：技术支持、产品咨询、投诉建议、账号问题、其他

优先级判断：
- high: 用户愤怒、设备无法使用、紧急问题
- medium: 一般功能咨询、需要指导
- low: 闲聊、简单问候"""


def _call_llm_json(messages, system: str) -> dict:
    """Call LLM through shared client."""
    result = get_llm_client().chat_json(messages, system, max_tokens=256)
    if not result:
        return {"issue_category": "其他", "description": "", "resolution": "", "priority": "low"}
    return result


def generate_summary(messages: List[Any], emotion: str = "neutral",
                     emotion_intensity: int = 1, satisfaction: bool = None) -> Dict[str, Any]:
    """Generate a service ticket summary from conversation messages.

    Args:
        messages: Full conversation history (HumanMessage/AIMessage objects)
        emotion: Detected user emotion
        emotion_intensity: Emotion intensity (1-5)
        satisfaction: User satisfaction if known

    Returns:
        Structured ticket dict
    """
    # Build conversation text for LLM
    conv_lines = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            conv_lines.append(f"用户: {msg.content}")
        elif isinstance(msg, AIMessage):
            conv_lines.append(f"客服: {msg.content}")

    conv_text = "\n".join(conv_lines[-20:])  # Last 20 messages to avoid token overflow

    summary = _call_llm_json(
        [{"role": "user", "content": f"请为以下对话生成工单摘要：\n\n{conv_text}"}],
        SUMMARY_SYSTEM
    )

    # Adjust priority based on emotion
    priority = summary.get("priority", "low")
    if emotion == "angry" and emotion_intensity >= 3:
        priority = "high"
    elif emotion in ("sad", "anxious") and emotion_intensity >= 4:
        priority = "medium"

    # Build ticket
    now = datetime.now()
    ticket = {
        # 修复：旧格式 T-YYYYMMDD-HHMM 同一分钟内多个工单会同号（配合 DB 层
        # INSERT OR REPLACE 时直接互相覆盖）。追加 uuid 短后缀保证唯一。
        "ticket_id": f"T-{now.strftime('%Y%m%d')}-{now.hour:02d}{now.minute:02d}-{uuid.uuid4().hex[:8]}",
        "issue_category": summary.get("issue_category", "其他"),
        "description": summary.get("description", ""),
        "resolution": summary.get("resolution", ""),
        "satisfaction": {True: "满意", False: "不满意", None: "未评价"}.get(satisfaction, "未评价"),
        "priority": priority,
        "emotion": emotion,
        "emotion_intensity": emotion_intensity,
        "message_count": len(messages),
        "created_at": now.isoformat(),
    }

    return ticket


def format_ticket(ticket: Dict[str, Any]) -> str:
    """Format ticket as readable text."""
    priority_map = {"high": "🔴 高", "medium": "🟡 中", "low": "🟢 低"}
    lines = [
        f"📋 服务工单 #{ticket['ticket_id']}",
        f"分类: {ticket['issue_category']} | 优先级: {priority_map.get(ticket['priority'], ticket['priority'])}",
        f"问题: {ticket['description']}",
        f"解决: {ticket['resolution']}",
        f"满意度: {ticket['satisfaction']}",
        f"创建时间: {ticket['created_at']}",
    ]
    return "\n".join(lines)
