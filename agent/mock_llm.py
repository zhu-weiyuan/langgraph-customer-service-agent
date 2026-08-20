# -*- coding: utf-8 -*-
"""Mock LLM / Embedding 层 —— 应用层压测专用（默认完全关闭）。

为什么需要它
------------
本项目线上接的是本地 35B 模型，单次生成 15~22s。直接用 100 并发压 `/api/chat`，
压出来的 P95 基本等于"模型吞吐"，跟应用代码好坏没关系。要证明**应用层**能力
（异步化是否真的没阻塞事件循环、限流是否生效、会话/checkpoint 是否是瓶颈、
SSE 是否能扛住长连接），必须把 LLM 换成"固定延迟的假模型"，让延迟成为已知常量。

开关（全部默认关闭，不设就是原行为）
------------------------------------
    MOCK_LLM=1                 打开 LLM mock（chat / chat_json / 流式 / gateway）
    MOCK_LLM_DELAY_MS=200      单次"生成"耗时（毫秒，默认 200）
    MOCK_LLM_JSON_DELAY_MS     结构化输出（意图/情绪）耗时，默认同上
    MOCK_LLM_TOKENS=48         流式时切出的 token 数（每 token 间隔 = 总延迟/token 数）
    MOCK_LLM_REPLY="..."       覆盖固定回复文本
    MOCK_EMBEDDING=1           打开 embedding mock（返回确定性伪向量，不打网络）
    MOCK_EMBEDDING_DELAY_MS=5  单批 embedding 耗时（毫秒，默认 5）
    MOCK_EMBEDDING_DIM=1024    伪向量维度（EmbeddingClient 显式指定 dimensions 时以其为准）

同步路径用 ``time.sleep``，异步路径用 ``asyncio.sleep``——后者是关键：如果应用把
LLM 调用放在了同步阻塞路径上，压测会立刻在 QPS 上暴露出来（QPS 卡在
worker 数 / 延迟，而不是并发数 / 延迟）。

本模块纯 stdlib，可被任何模块无条件 import。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Sequence

__all__ = [
    "mock_llm_enabled", "mock_embedding_enabled",
    "mock_delay_seconds", "mock_json_delay_seconds",
    "mock_sleep", "mock_sleep_async",
    "mock_chat", "mock_chat_async", "mock_chat_json", "mock_chat_json_async",
    "mock_stream", "mock_stream_async",
    "mock_reply_text", "mock_json_payload", "split_tokens",
    "fake_embedding", "fake_embeddings",
]

DEFAULT_DELAY_MS = 200.0
DEFAULT_EMBED_DELAY_MS = 5.0
DEFAULT_TOKENS = 48
DEFAULT_EMBED_DIM = 1024

_DEFAULT_REPLY = (
    "您好，已经为您查到相关信息。根据知识库内容："
    "该问题通常可以按以下步骤处理——第一步，确认设备已连接电源并处于待机状态；"
    "第二步，在 App 内进入「设备管理」重新配网；"
    "第三步，若仍未解决，可在保修期内申请免费换新。"
    "还有其他可以帮您的吗？"
)


# ── 开关读取 ────────────────────────────────────────────────

def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def mock_llm_enabled() -> bool:
    """MOCK_LLM 是否打开（每次读环境变量，便于测试里临时切换）。"""
    return _truthy(os.environ.get("MOCK_LLM"))


def mock_embedding_enabled() -> bool:
    """MOCK_EMBEDDING 是否打开。MOCK_LLM 不隐含打开它（RAG 可单独压真实向量库）。"""
    return _truthy(os.environ.get("MOCK_EMBEDDING"))


def _float_env(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _int_env(name: str, default: int) -> int:
    return int(_float_env(name, float(default)))


def mock_delay_seconds() -> float:
    """普通生成的模拟耗时（秒）。"""
    return _float_env("MOCK_LLM_DELAY_MS", DEFAULT_DELAY_MS) / 1000.0


def mock_json_delay_seconds() -> float:
    """结构化输出（意图识别等短请求）的模拟耗时（秒）。"""
    raw = str(os.environ.get("MOCK_LLM_JSON_DELAY_MS", "")).strip()
    if not raw:
        return mock_delay_seconds()
    return _float_env("MOCK_LLM_JSON_DELAY_MS", DEFAULT_DELAY_MS) / 1000.0


def mock_embedding_delay_seconds() -> float:
    return _float_env("MOCK_EMBEDDING_DELAY_MS", DEFAULT_EMBED_DELAY_MS) / 1000.0


def mock_sleep(seconds: Optional[float] = None) -> None:
    """同步路径的模拟耗时。"""
    delay = mock_delay_seconds() if seconds is None else seconds
    if delay > 0:
        time.sleep(delay)


async def mock_sleep_async(seconds: Optional[float] = None) -> None:
    """异步路径的模拟耗时（必须是 asyncio.sleep，否则压测测不出事件循环阻塞）。"""
    delay = mock_delay_seconds() if seconds is None else seconds
    if delay > 0:
        await asyncio.sleep(delay)


# ── 文本生成 ────────────────────────────────────────────────

def _last_user_text(messages: Optional[Sequence[Any]]) -> str:
    if not messages:
        return ""
    for msg in reversed(list(messages)):
        if isinstance(msg, dict):
            if msg.get("role") in ("user", "human"):
                return str(msg.get("content") or "")
        else:
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content:
                return content
    first = list(messages)[-1]
    if isinstance(first, dict):
        return str(first.get("content") or "")
    return str(first)


def mock_reply_text(messages: Optional[Sequence[Any]] = None) -> str:
    """确定性假回复：同样的输入永远得到同样的输出（压测结果可复现）。"""
    override = os.environ.get("MOCK_LLM_REPLY")
    if override:
        return override
    question = _last_user_text(messages).strip()
    if not question:
        return _DEFAULT_REPLY
    head = question[:40].replace("\n", " ")
    return f"关于「{head}」，{_DEFAULT_REPLY}"


def split_tokens(text: str, n_tokens: Optional[int] = None) -> List[str]:
    """把文本切成 n 段，模拟逐 token 输出（切片而非真分词，够压测用）。"""
    if not text:
        return []
    n = n_tokens if n_tokens and n_tokens > 0 else _int_env("MOCK_LLM_TOKENS",
                                                            DEFAULT_TOKENS)
    n = max(1, min(n, len(text)))
    size = max(1, math.ceil(len(text) / n))
    return [text[i:i + size] for i in range(0, len(text), size)]


def mock_chat(messages: Optional[Sequence[Any]] = None) -> str:
    """同步 chat mock（time.sleep）。"""
    mock_sleep()
    return mock_reply_text(messages)


async def mock_chat_async(messages: Optional[Sequence[Any]] = None) -> str:
    """异步 chat mock（asyncio.sleep）。"""
    await mock_sleep_async()
    return mock_reply_text(messages)


def mock_stream(messages: Optional[Sequence[Any]] = None) -> Iterator[str]:
    """同步流式 mock：逐 token 吐出，间隔 = 总延迟 / token 数。"""
    text = mock_reply_text(messages)
    pieces = split_tokens(text)
    per_token = (mock_delay_seconds() / len(pieces)) if pieces else 0.0
    for piece in pieces:
        if per_token > 0:
            time.sleep(per_token)
        yield piece


async def mock_stream_async(
        messages: Optional[Sequence[Any]] = None) -> AsyncIterator[str]:
    """异步流式 mock：逐 token 吐出，间隔 = 总延迟 / token 数。"""
    text = mock_reply_text(messages)
    pieces = split_tokens(text)
    per_token = (mock_delay_seconds() / len(pieces)) if pieces else 0.0
    for piece in pieces:
        if per_token > 0:
            await asyncio.sleep(per_token)
        yield piece


# ── 结构化输出（chat_json）─────────────────────────────────
#
# 返回的 key 必须落在 llm_client.LLMClient.EXPECTED_JSON_KEYS 白名单内，
# 否则调用方（nodes/sentiment/agentic_rag/summary）会走兜底分支，压测就绕过了
# 正常代码路径。这里按 system prompt 关键词分流。

def _scene_of(text: str) -> str:
    low = text.lower()
    if any(k in text for k in ("情绪", "情感")) or "emotion" in low:
        return "sentiment"
    if any(k in text for k in ("满意", "评价")) or "satisfaction" in low:
        return "satisfaction"
    if any(k in text for k in ("检索", "资料是否", "足够", "充分")) or \
            "sufficient" in low:
        return "rag_judge"
    if any(k in text for k in ("工单", "总结", "摘要")) or \
            any(k in low for k in ("issue_category", "summary")):
        return "summary"
    if any(k in text for k in ("意图", "分类")) or "intent" in low:
        return "intent"
    return "intent"


def mock_json_payload(messages: Optional[Sequence[Any]] = None,
                      system: Optional[str] = None) -> Dict[str, Any]:
    """按场景返回合法且字段齐全的假 JSON（确定性）。"""
    probe = f"{system or ''}\n{_last_user_text(messages)}"
    scene = _scene_of(probe)
    if scene == "sentiment":
        return {"emotion": "neutral", "intensity": 0.3, "confidence": 0.9}
    if scene == "satisfaction":
        return {"satisfaction": "satisfied", "satisfied": True,
                "confidence": 0.9}
    if scene == "rag_judge":
        return {"sufficient": True, "reason": "mock: context is sufficient",
                "new_queries": [], "confidence": 0.9}
    if scene == "summary":
        return {"issue_category": "产品咨询",
                "description": "mock summary of the conversation",
                "resolution": "已解答", "priority": "low", "confidence": 0.9}
    ending = any(w in _last_user_text(messages) for w in ("谢谢", "再见", "拜拜"))
    return {"intent": "consult", "ending": ending, "confidence": 0.9}


def mock_chat_json(messages: Optional[Sequence[Any]] = None,
                   system: Optional[str] = None) -> Dict[str, Any]:
    """同步 chat_json mock。"""
    mock_sleep(mock_json_delay_seconds())
    return mock_json_payload(messages, system)


async def mock_chat_json_async(messages: Optional[Sequence[Any]] = None,
                               system: Optional[str] = None) -> Dict[str, Any]:
    """异步 chat_json mock。"""
    await mock_sleep_async(mock_json_delay_seconds())
    return mock_json_payload(messages, system)


def mock_json_text(messages: Optional[Sequence[Any]] = None,
                   system: Optional[str] = None) -> str:
    """结构化输出的**字符串**形式（gateway 返回 content 字符串，由调用方解析）。"""
    return json.dumps(mock_json_payload(messages, system), ensure_ascii=False)


# ── Embedding ──────────────────────────────────────────────

def fake_embedding(text: str, dim: Optional[int] = None) -> List[float]:
    """确定性伪向量：同文本 → 同向量，且已 L2 归一化（余弦相似度可用）。

    用 sha256 逐块扩展成 dim 个 [-1,1] 浮点，纯 CPU、无网络、无三方依赖。
    """
    n = int(dim or _int_env("MOCK_EMBEDDING_DIM", DEFAULT_EMBED_DIM))
    n = max(1, n)
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    raw = bytearray()
    counter = 0
    while len(raw) < n * 2:
        raw.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    values = []
    for i in range(n):
        word = int.from_bytes(raw[i * 2:i * 2 + 2], "big")
        values.append((word / 32767.5) - 1.0)
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def fake_embeddings(texts: Sequence[str],
                    dim: Optional[int] = None) -> List[List[float]]:
    """批量伪向量（带一次可配延迟，模拟 embedding 服务往返）。"""
    delay = mock_embedding_delay_seconds()
    if delay > 0:
        time.sleep(delay)
    return [fake_embedding(t, dim) for t in texts]


def mock_status() -> Dict[str, Any]:
    """当前 mock 配置快照（可挂到 /api/health 或压测报告里，方便自证）。"""
    return {
        "mock_llm": mock_llm_enabled(),
        "mock_llm_delay_ms": round(mock_delay_seconds() * 1000, 1),
        "mock_llm_json_delay_ms": round(mock_json_delay_seconds() * 1000, 1),
        "mock_llm_tokens": _int_env("MOCK_LLM_TOKENS", DEFAULT_TOKENS),
        "mock_embedding": mock_embedding_enabled(),
        "mock_embedding_dim": _int_env("MOCK_EMBEDDING_DIM", DEFAULT_EMBED_DIM),
    }
