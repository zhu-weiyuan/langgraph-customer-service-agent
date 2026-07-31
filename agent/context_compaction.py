#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Context Compaction — 对话历史压缩

当多轮对话过长时，不直接截断旧消息（丢失信息），而是用 LLM
对早期对话做摘要压缩，保留关键上下文。

策略：
1. **滑动窗口**：最近 K 轮完整保留
2. **LLM 摘要**：更早的对话压缩成一段 summary，注入 system prompt
3. **增量压缩**：只在超过阈值时才触发，避免每次都跑 LLM
4. **结构化摘要**：保留用户画像、产品兴趣、已解决问题、未解决痛点

参考 JavaGuide "上下文窗口优化" 最佳实践。

面试亮点：
- 不是简单截断，而是语义压缩（信息密度 > 原始消息）
- 增量触发，不浪费 Token
- summary 可复用（写入 memory，下次直接加载）

Usage:
    from agent.context_compaction import ContextCompactor

    compactor = ContextCompactor()
    result = compactor.maybe_compact(messages, session_id)
    # result.summary → 注入 system prompt
    # result.messages → 裁剪后的消息列表
"""

import json
import logging
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

# 三方守卫：langchain_core 缺席时降级为等价的轻量消息类型（仅用于 isinstance
# 分流与 .content 读取）。纯 stdlib 单测无需安装 langchain 即可导入本模块。
try:
    from langchain_core.messages import HumanMessage, AIMessage
except Exception:  # pragma: no cover
    class _BaseMessage:
        def __init__(self, content: str = "", **kwargs):
            self.content = content

        def __repr__(self) -> str:
            return f"{type(self).__name__}({self.content!r})"

    class HumanMessage(_BaseMessage):  # type: ignore
        type = "human"

    class AIMessage(_BaseMessage):  # type: ignore
        type = "ai"

logger = logging.getLogger(__name__)

# Lazy import for LLMGateway (avoid circular import at module load time)
_llm_gateway_module = None

def _get_llm_gateway_module():
    """Lazy-load llm_gateway module. Testable via monkey-patching."""
    global _llm_gateway_module
    if _llm_gateway_module is None:
        from . import llm_gateway as _mg
        _llm_gateway_module = _mg
    return _llm_gateway_module


# ─────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────

# 触发压缩的阈值
COMPACTION_TRIGGER_MESSAGES = 16     # 超过 16 条消息时触发压缩
COMPACTION_TRIGGER_TOKENS = 40_000   # 或估算 Token 超过 40K

# 保留最近多少轮完整对话（一轮 = user + assistant）
KEEP_RECENT_TURNS = 5               # 保留最近 5 轮 = 10 条消息

# 摘要最大长度
MAX_SUMMARY_CHARS = 800


# ─────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────

@dataclass
class CompactionResult:
    """压缩结果"""
    summary: str                    # 对话摘要（注入 system prompt）
    messages: List                  # 裁剪后的消息列表
    compacted: bool                 # 是否执行了压缩
    tokens_saved: int               # 节省的 Token 估算
    old_count: int                  # 原始消息数
    new_count: int                  # 压缩后消息数


# ─────────────────────────────────────────────────────
# Token 估算（复用 nodes.py 的逻辑）
# ─────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    """粗略估算 Token 数。"""
    import re
    chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
    english = len(re.findall(r'[a-zA-Z]+', text))
    other = len(text) - chinese - len(re.findall(r'[a-zA-Z]+', text))
    return int(chinese * 1.5 + english * 1.3 + other * 0.5)


def _estimate_messages_tokens(messages: List) -> int:
    total = 0
    for msg in messages:
        content = getattr(msg, 'content', str(msg))
        total += _estimate_tokens(content)
    return total


# ─────────────────────────────────────────────────────
# 压缩器
# ─────────────────────────────────────────────────────

class ContextCompactor:
    """对话历史压缩器。

    工作流程：
    1. 检查是否需要压缩（消息数/token 阈值）
    2. 分离"需要压缩的旧消息"和"保留的新消息"
    3. 用 LLM 对旧消息做结构化摘要
    4. 返回 (summary, trimmed_messages)

    面试要点：
    - 为什么不用简单截断？→ 截断丢失关键上下文（用户之前问过什么、情绪走向）
    - 怎么保证压缩质量？→ 结构化 prompt，强制保留实体和事实
    - 性能？→ 只在阈值触发时跑一次 LLM，有缓存机制
    """

    # 摘要生成 System Prompt
    COMPACT_SYSTEM = """你是一个对话摘要助手。你的任务是将一段客服对话历史压缩成简洁的结构化摘要。

要求：
1. 保留所有**关键事实**：用户提到的产品、问题、订单号、时间等
2. 标注用户的**情绪变化**（如有）
3. 列出**已解决的问题**和**未解决/待跟进的问题**
4. 提取用户的**偏好信息**（如饮食偏好、预算范围等，如有）
5. 保持客观，不要添加原始对话中没有的信息

输出格式（严格 JSON）：
{
    "products_mentioned": ["产品A", "产品B"],
    "resolved_issues": ["问题1已解决"],
    "pending_issues": ["问题2待处理"],
    "user_preferences": {"偏好类型": "偏好值"},
    "emotion_summary": "用户从不满转为满意",
    "key_facts": ["订单号xxx", "购买日期xxx"]
}"""

    def __init__(self, llm_client=None):
        """初始化压缩器。

        Args:
            llm_client: LLMClient 实例，如未提供则使用全局单例
        """
        self._llm = llm_client
        # 缓存：session_id → summary（避免重复压缩同一会话）
        self._summary_cache: Dict[str, str] = {}

    def _get_llm(self):
        if self._llm is None:
            from .llm_client import get_llm_client
            self._llm = get_llm_client()
        return self._llm

    def maybe_compact(
        self,
        messages: List,
        session_id: str = "",
        force: bool = False,
    ) -> CompactionResult:
        """检查是否需要压缩，如需则执行。

        Args:
            messages: 完整消息列表
            session_id: 会话 ID（用于缓存）
            force: 强制压缩（忽略阈值）

        Returns:
            CompactionResult with summary and trimmed messages
        """
        total_tokens = _estimate_messages_tokens(messages)

        # 检查是否需要压缩
        needs_compaction = (
            force or
            len(messages) > COMPACTION_TRIGGER_MESSAGES or
            total_tokens > COMPACTION_TRIGGER_TOKENS
        )

        if not needs_compaction:
            return CompactionResult(
                summary="",
                messages=messages,
                compacted=False,
                tokens_saved=0,
                old_count=len(messages),
                new_count=len(messages),
            )

        # 分离：保留首个 user query + 压缩中间 + 保留最近 N 轮
        #
        # 产品要求修正：只保留最近 N 轮会丢掉开头——而首个问题常含订单号 /
        # 背景 / 诉求，是后续所有轮次的锚点。因此切分为三段：
        #   [首个 user turn]  +  summarize(中间)  +  [最近 KEEP_RECENT_TURNS 轮]
        keep = KEEP_RECENT_TURNS * 2  # 每轮 = user + assistant
        first_idx = self._first_user_index(messages)

        # 需要有可压缩的"中间段"：首 user turn 之后、最近窗口之前还有消息。
        # 若消息太少（首turn与最近窗口重叠/相邻），无中间可压 → 不压缩。
        recent_start = len(messages) - keep
        if first_idx is None or recent_start <= first_idx + 1:
            return CompactionResult(
                summary="",
                messages=messages,
                compacted=False,
                tokens_saved=0,
                old_count=len(messages),
                new_count=len(messages),
            )

        first_turn = messages[first_idx:first_idx + 1]      # 首个 user 消息
        middle_messages = messages[first_idx + 1:recent_start]  # 被摘要的中间段
        new_messages = messages[recent_start:]              # 最近 N 轮完整保留
        preserved = first_turn + new_messages               # 首尾拼接

        # 检查缓存（中间段摘要按 session 复用）
        cached_summary = self._summary_cache.get(session_id) if session_id else None
        if cached_summary:
            old_tokens = _estimate_messages_tokens(middle_messages)
            return CompactionResult(
                summary=cached_summary,
                messages=preserved,
                compacted=False,  # 来自缓存，不是新压缩
                tokens_saved=old_tokens,
                old_count=len(messages),
                new_count=len(preserved),
            )

        # 执行 LLM 压缩（仅压中间段；首尾原样保留）
        summary = self._compact_old_messages(middle_messages)

        if session_id:
            self._summary_cache[session_id] = summary

        old_tokens = _estimate_messages_tokens(middle_messages)
        logger.info(
            f"[Compaction] {len(messages)} → {len(preserved)} messages "
            f"(首 query 保留 + 中间 {len(middle_messages)} 条摘要 + 最近 "
            f"{len(new_messages)} 条), saved ~{old_tokens} tokens, "
            f"summary={len(summary)} chars"
        )

        return CompactionResult(
            summary=summary,
            messages=preserved,
            compacted=True,
            tokens_saved=old_tokens,
            old_count=len(messages),
            new_count=len(preserved),
        )

    @staticmethod
    def _first_user_index(messages: List) -> Optional[int]:
        """返回第一个 HumanMessage 的下标；找不到返回 None。"""
        for i, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                return i
        return None

    def _compact_old_messages(self, old_messages: List) -> str:
        """用 LLM 对旧消息做结构化摘要（优先走 Gateway 的 balanced tier 省成本）。"""
        import re

        # 格式化消息为文本
        dialogue_text = ""
        for msg in old_messages:
            content = getattr(msg, 'content', str(msg))
            role = "用户" if isinstance(msg, HumanMessage) else "客服"
            dialogue_text += f"{role}: {content}\n"

        messages = [
            {"role": "system", "content": self.COMPACT_SYSTEM},
            {"role": "user", "content": f"请压缩以下对话历史：\n\n{dialogue_text}"},
        ]

        # ── 尝试调用 LLM（Gateway → 直接 LLM） ────────────────
        raw = None

        # 优先走 LLM Gateway（lazy import 避免循环引用）
        # 注意：chat_simple 是 async，必须通过 chat_sync 同步调用，
        # 否则 coroutine 会静默泄漏导致 Gateway 被跳过。
        try:
            gw_module = _get_llm_gateway_module()
            gateway = gw_module.get_llm_gateway()
            from .llm_gateway import GatewayRequest
            result = gateway.chat_sync(GatewayRequest(
                messages=messages,
                scene="context_compaction",
                temperature=0.1,
            ))
            raw = result.content
        except Exception as e:
            logger.warning(f"[Compaction] Gateway unavailable, falling back to direct LLM: {e}")

        # Gateway 不可用时降级到直接 LLM
        if raw is None:
            try:
                llm = self._get_llm()
                raw = llm.chat(messages, temperature=0.1, max_tokens=512)
            except Exception as e:
                logger.error(f"[Compaction] LLM failed, using fallback summary: {e}")
                return self._fallback_summary(old_messages)[:MAX_SUMMARY_CHARS]

        # ── 解析结果 ──────────────────────────────────────────
        # 尝试直接解析整段文本
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                summary = self._format_summary(data)
                return summary[:MAX_SUMMARY_CHARS]
        except (json.JSONDecodeError, TypeError):
            pass

        # 尝试提取最外层的 JSON 对象（支持嵌套大括号）
        json_match = re.search(r'\{[^}]*(?:\{[^}]*\}[^}]*)*\}', raw, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                summary = self._format_summary(data)
            except json.JSONDecodeError:
                summary = raw
        else:
            summary = raw

        return summary[:MAX_SUMMARY_CHARS]

    def _format_summary(self, data: Dict[str, Any]) -> str:
        """将结构化摘要格式化为自然语言。"""
        parts = []

        if data.get("key_facts"):
            parts.append(f"关键事实：{'；'.join(data['key_facts'])}")

        if data.get("products_mentioned"):
            products = '、'.join(data['products_mentioned'])
            parts.append(f"涉及产品：{products}")

        if data.get("resolved_issues"):
            resolved = '；'.join(data['resolved_issues'])
            parts.append(f"已解决：{resolved}")

        if data.get("pending_issues"):
            pending = '；'.join(data['pending_issues'])
            parts.append(f"待处理：{pending}")

        if data.get("user_preferences"):
            prefs = ', '.join(
                f"{k}={v}" for k, v in data['user_preferences'].items()
            )
            parts.append(f"用户偏好：{prefs}")

        if data.get("emotion_summary"):
            parts.append(f"情绪变化：{data['emotion_summary']}")

        return "【对话历史摘要】\n" + "\n".join(parts) if parts else "【对话历史摘要】用户与客服进行了多轮对话。"

    def _fallback_summary(self, old_messages: List) -> str:
        """LLM 不可用时的降级方案：简单提取关键词。"""
        # 提取所有用户消息的前 50 字
        user_snippets = []
        for msg in old_messages:
            if isinstance(msg, HumanMessage):
                content = msg.content[:50]
                user_snippets.append(content)

        if user_snippets:
            return "【对话历史摘要】\n用户曾提到：" + "；".join(user_snippets[:5])

        return "【对话历史摘要】（压缩失败，使用空摘要）"

    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计。"""
        return {"cached_sessions": len(self._summary_cache)}


# ─────────────────────────────────────────────────────
# 全局单例
# ─────────────────────────────────────────────────────

_compactor_instance: Optional[ContextCompactor] = None


def get_compactor() -> ContextCompactor:
    """获取全局压缩器实例。"""
    global _compactor_instance
    if _compactor_instance is None:
        _compactor_instance = ContextCompactor()
    return _compactor_instance
