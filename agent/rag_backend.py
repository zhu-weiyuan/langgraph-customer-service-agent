# -*- coding: utf-8 -*-
"""
rag_backend — 运行时检索后端选择（env RAG_BACKEND=tfidf|hybrid|pgvector）

agentic_rag 通过本模块调用检索：
  * 未设置 / tfidf：完全保持现状（rag.retrieve，含其内部 vector 融合行为）。
  * hybrid：hybrid_rag.build_retriever_from_env()（进程内双路 + RRF + 重排）。
  * pgvector：PgHybridStore.hybrid_search（单 SQL 双路 CTE + RRF），查询嵌入走
    EmbeddingClient；此模式下不会触发 vector_rag 的全量索引 eager build。
  * 优雅降级：hybrid/pgvector 运行期失败（PG 挂 / psycopg 缺失 / 网络错误）
    → log warning，本次请求回落 TF-IDF（use_vector=False，避免 401 刷屏）。

编排逻辑收敛在 retrieve_with_backend()（依赖全可注入，stdlib 单测覆盖）。
"""

from __future__ import annotations

import logging
import os
import unicodedata
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("agent.rag_backend")

_BACKENDS = ("tfidf", "hybrid", "pgvector")

_RAG_SMALLTALK_EXACT = frozenset({'你好', '您好', '嗨', 'hello', 'hi', 'hey', '在吗', '在么', '谢谢', '谢谢了', '谢谢你的帮助', '感谢', '不用了谢谢', '再见', '拜拜', 'bye', '没事了', '好的谢谢', '就这样', '结束'})
_RAG_SMALLTALK_PHRASES = ('你是谁', '你叫什么', '你能做什么', '讲个笑话', '讲笑话', '写一首诗', '今天天气', '天气怎么样', '吃了吗',)
_RAG_BUSINESS_HINTS = ('订单', '退货', '退款', '换货', '换新', '发货', '物流', '保修', '维修', '售后', '音箱', '网关', '智能灯', '会员', '发票', '账号', '登录', '故障', '设备', 'wifi', '蓝牙', '错误', '支付', 'api', '产品', 'app', '价格', '安装',)


def should_skip_retrieval(query: str) -> bool:
    """Return true for clear non-knowledge turns that should not receive RAG context."""
    raw = (query or "").strip().lower()
    compact = "".join(
        ch for ch in raw
        if not ch.isspace() and not unicodedata.category(ch).startswith(("P", "S"))
    )
    if not compact:
        return True
    # Preserve retrieval for a real support question even when it starts with a
    # greeting or a thank-you.
    if any(hint in compact for hint in _RAG_BUSINESS_HINTS):
        return False
    return compact in _RAG_SMALLTALK_EXACT or any(
        phrase in compact for phrase in _RAG_SMALLTALK_PHRASES
    )


# ── 纯函数：后端选择（env 矩阵可测）─────────────────────────

def select_backend(value: Optional[str] = None) -> str:
    """RAG_BACKEND 取值归一：hybrid/pgvector 原样，其余（含未设置/非法）→ tfidf。"""
    if value is None:
        value = os.environ.get("RAG_BACKEND", "")
    v = (value or "").strip().lower()
    return v if v in ("hybrid", "pgvector") else "tfidf"


# ── 各后端检索函数（懒初始化、进程内缓存）────────────────────

_cache: Dict[str, Any] = {
    "hybrid": None,
    "pg_store": None,
    "parent_map": None,
    "pg_reranker": None,
}


def reset_cache() -> None:
    """Clear cached retrievers, stores, parent mappings and rerankers."""
    _cache.update({
        "hybrid": None,
        "pg_store": None,
        "parent_map": None,
        "pg_reranker": None,
    })


def _tfidf_retrieve(query: str, top_k: int = 3,
                    use_vector: bool = True) -> List[dict]:
    from .rag import retrieve as rag_retrieve
    return rag_retrieve(query, top_k=top_k, use_vector=use_vector)


def _get_hybrid_search_fn() -> Callable[[str, int], List[dict]]:
    if _cache["hybrid"] is None:
        from .hybrid_rag import build_retriever_from_env
        _cache["hybrid"] = build_retriever_from_env()
    retriever = _cache["hybrid"]

    def search(query: str, top_k: int) -> List[dict]:
        return retriever.search(query)[:top_k]
    return search


def _get_pg_search_fn() -> Callable[[str, int], List[dict]]:
    """pgvector 路：hybrid_search（DB 内 RRF）→ Cross-Encoder 重排 → parent 映射。

    查询嵌入由 PgHybridStore.from_env 注入的 EmbeddingClient 完成；
    不 import vector_rag，因此其 eager 索引构建不会触发。
    """
    from .embedding_client import EmbeddingClient
    from .hybrid_rag import (HybridRetriever, CrossEncoderReranker,
                             RuleReranker, map_children_to_parents)
    from .pgvector_hybrid import PgHybridStore

    if _cache["pg_store"] is None:
        client = EmbeddingClient.from_env(strict=False)
        embed_fn = (lambda t: client.embed_one(t)) if client else None
        _cache["pg_store"] = PgHybridStore.from_env(embed_fn=embed_fn)
    store = _cache["pg_store"]

    # Default to the local rule reranker so online requests never download a
    # model. CrossEncoder is opt-in and is also cached for the process lifetime.
    if _cache["pg_reranker"] is None:
        mode = os.getenv("RAG_RERANKER", "rule").strip().lower()
        if mode in {"cross_encoder", "cross-encoder"}:
            _cache["pg_reranker"] = CrossEncoderReranker()
            logger.info("[RAG backend] pgvector reranker=cross_encoder (cached)")
        else:
            _cache["pg_reranker"] = RuleReranker()
            logger.info("[RAG backend] pgvector reranker=rule (cached)")
    reranker = _cache["pg_reranker"]

    def search(query: str, top_k: int) -> List[dict]:
        hits = store.hybrid_search(query, top_k=max(top_k * 4, 20))
        if _cache["parent_map"] is None:
            try:
                _cache["parent_map"] = store.load_parent_map()
            except Exception:
                _cache["parent_map"] = {}
        reranked = reranker.rerank(query, hits, top_n=max(top_k * 2, 8))
        try:
            min_overlap = max(
                0.0,
                float(os.getenv("RAG_MIN_LEXICAL_OVERLAP", "0.04")),
            )
        except ValueError:
            min_overlap = 0.04

        # A vector/keyword top-k call always returns some candidates.  Do not
        # inject those weak candidates into the LLM context when they have no
        # lexical relation to the user question.  Set the env var to 0 to
        # disable the gate while diagnosing a corpus.
        if min_overlap:
            reranked = [
                item for item in reranked
                if float(item.get("lexical_overlap", 0.0) or 0.0) >= min_overlap
            ]

        mapped = map_children_to_parents(reranked, _cache["parent_map"])
        return [HybridRetriever._normalize(r) for r in mapped[:top_k]]
    return search


# ── 编排（依赖注入，可独测）──────────────────────────────────

def retrieve_with_backend(query: str, top_k: int, backend: str, *,
                          pg_search_fn: Optional[Callable] = None,
                          hybrid_search_fn: Optional[Callable] = None,
                          fallback_fn: Optional[Callable] = None) -> List[dict]:
    """按 backend 检索；hybrid/pgvector 失败时 warning + 回落 TF-IDF。

    Args:
        pg_search_fn / hybrid_search_fn: fn(query, top_k) -> [dict]（测试注入）
        fallback_fn: fn(query, top_k) -> [dict]，默认 TF-IDF（use_vector=False）
    """
    if should_skip_retrieval(query):
        logger.debug("[RAG backend] skip retrieval for non-knowledge turn")
        return []

    backend = select_backend(backend)

    if backend == "tfidf":
        fb = fallback_fn or (lambda q, k: _tfidf_retrieve(q, k, use_vector=True))
        return fb(query, top_k)  # 默认路径：保持现状（含原 vector 融合行为）

    try:
        if backend == "pgvector":
            fn = pg_search_fn or _get_pg_search_fn()
        else:  # hybrid
            fn = hybrid_search_fn or _get_hybrid_search_fn()
        return fn(query, top_k)
    except Exception as e:
        strict = os.getenv("RAG_STRICT", "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if backend == "pgvector" and strict:
            logger.error("[RAG backend] pgvector failed in strict mode: %s", e)
            raise
        logger.warning("[RAG backend] %s failed for this request (%s); "
                       "falling back to TF-IDF", backend, e)
        fb = fallback_fn or (lambda q, k: _tfidf_retrieve(q, k, use_vector=False))
        try:
            return fb(query, top_k)
        except Exception as e2:  # 双重失败也不打断对话流程
            logger.warning("[RAG backend] TF-IDF fallback also failed: %s", e2)
            return []


def retrieve(query: str, top_k: int = 3) -> List[dict]:
    """agentic_rag 的检索入口：env RAG_BACKEND 选择后端 + 优雅降级。"""
    return retrieve_with_backend(query, top_k, select_backend())
