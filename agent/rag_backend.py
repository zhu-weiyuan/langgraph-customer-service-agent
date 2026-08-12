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
<<<<<<< HEAD
import time
=======
>>>>>>> origin/master
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
<<<<<<< HEAD
    "search_cache": {},  # query-key -> (expire_ts, results)
=======
>>>>>>> origin/master
}


def reset_cache() -> None:
    """Clear cached retrievers, stores, parent mappings and rerankers."""
    _cache.update({
        "hybrid": None,
        "pg_store": None,
        "parent_map": None,
        "pg_reranker": None,
<<<<<<< HEAD
        "search_cache": {},
=======
>>>>>>> origin/master
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
<<<<<<< HEAD
    from .remote_reranker import RemoteReranker
=======
>>>>>>> origin/master
    from .pgvector_hybrid import PgHybridStore

    if _cache["pg_store"] is None:
        client = EmbeddingClient.from_env(strict=False)
        embed_fn = (lambda t: client.embed_one(t)) if client else None
        _cache["pg_store"] = PgHybridStore.from_env(embed_fn=embed_fn)
    store = _cache["pg_store"]

<<<<<<< HEAD
    # Remote SiliconFlow rerank is opt-in by mode and falls back to rule ranking
    # on timeout/provider errors. Local CrossEncoder remains available.
    if _cache["pg_reranker"] is None:
        mode = os.getenv("RAG_RERANKER", "rule").strip().lower()
        fallback = RuleReranker()
        if mode in {"remote", "siliconflow", "remote_reranker"}:
            _cache["pg_reranker"] = RemoteReranker.from_env(fallback=fallback)
            if _cache["pg_reranker"] is None:
                _cache["pg_reranker"] = fallback
            logger.info("[RAG backend] pgvector reranker=remote (cached)")
        elif mode in {"cross_encoder", "cross-encoder"}:
            _cache["pg_reranker"] = CrossEncoderReranker()
            logger.info("[RAG backend] pgvector reranker=cross_encoder (cached)")
        else:
            _cache["pg_reranker"] = fallback
=======
    # Default to the local rule reranker so online requests never download a
    # model. CrossEncoder is opt-in and is also cached for the process lifetime.
    if _cache["pg_reranker"] is None:
        mode = os.getenv("RAG_RERANKER", "rule").strip().lower()
        if mode in {"cross_encoder", "cross-encoder"}:
            _cache["pg_reranker"] = CrossEncoderReranker()
            logger.info("[RAG backend] pgvector reranker=cross_encoder (cached)")
        else:
            _cache["pg_reranker"] = RuleReranker()
>>>>>>> origin/master
            logger.info("[RAG backend] pgvector reranker=rule (cached)")
    reranker = _cache["pg_reranker"]

    def search(query: str, top_k: int) -> List[dict]:
<<<<<<< HEAD
        # TTL cache: identical queries within RAG_SEARCH_CACHE_TTL reuse results
        try:
            ttl = max(0.0, float(os.getenv("RAG_SEARCH_CACHE_TTL", "60")))
        except ValueError:
            ttl = 60.0
        cache_key = f"{top_k}:{query}"
        if ttl > 0:
            cached = _cache["search_cache"].get(cache_key)
            if cached and cached[0] > time.time():
                return [dict(c) for c in cached[1]]
=======
>>>>>>> origin/master
        hits = store.hybrid_search(query, top_k=max(top_k * 4, 20))
        if _cache["parent_map"] is None:
            try:
                _cache["parent_map"] = store.load_parent_map()
            except Exception:
                _cache["parent_map"] = {}
        reranked = reranker.rerank(query, hits, top_n=max(top_k * 2, 8))
<<<<<<< HEAD
        # CrossEncoderReranker may return only its model score.  Normalize the
        # shared lexical diagnostic before applying the lexical relevance gate
        # so the gate never silently drops every result just because a
        # reranker implementation omitted that optional field.
        from .hybrid_rag import RuleReranker
        for item in reranked:
            if "lexical_overlap" not in item:
                item["lexical_overlap"] = round(
                    RuleReranker.lexical_overlap(query, item), 6
                )
=======
>>>>>>> origin/master
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

<<<<<<< HEAD
        # ── rerank 分数阈值（RAG_MIN_RERANK_SCORE）──────────────────────
        # 低分候选基本是噪声（见真实评估：0.3 以下多为 ✗），但保证至少
        # RAG_MIN_RESULTS 条进上下文，避免上下文过短导致回答质量崩塌。
        try:
            min_rerank = max(
                0.0, float(os.getenv("RAG_MIN_RERANK_SCORE", "0.05"))
            )
        except ValueError:
            min_rerank = 0.05
        try:
            min_results = max(1, int(os.getenv("RAG_MIN_RESULTS", "3")))
        except ValueError:
            min_results = 3
        if min_rerank and reranked:
            kept = [
                item for item in reranked
                if float(item.get("rerank_score", 0.0) or 0.0) >= min_rerank
            ]
            if len(kept) < min_results:
                # 分数阈值砍太狠：从被过滤的候选中按分数补足到 min_results
                kept_ids = {id(it) for it in kept}
                dropped = sorted(
                    [item for item in reranked if id(item) not in kept_ids],
                    key=lambda it: float(it.get("rerank_score", 0.0) or 0.0),
                    reverse=True,
                )
                kept = kept + dropped[: min_results - len(kept)]
            reranked = kept

        mapped = map_children_to_parents(reranked, _cache["parent_map"])
        results = [HybridRetriever._normalize(r) for r in mapped[:top_k]]
        if ttl > 0:
            _cache["search_cache"][cache_key] = (time.time() + ttl, [dict(r) for r in results])
            if len(_cache["search_cache"]) > 512:
                _cache["search_cache"] = {
                    k: v for k, v in _cache["search_cache"].items()
                    if v[0] > time.time()
                }
        return results
=======
        mapped = map_children_to_parents(reranked, _cache["parent_map"])
        return [HybridRetriever._normalize(r) for r in mapped[:top_k]]
>>>>>>> origin/master
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
