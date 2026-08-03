# -*- coding: utf-8 -*-
"""
LLM-guided Agentic Vector Hybrid RAG

只用 LLM 做 query rewrite；检索仍然走本地 vector hybrid。
不做 LLM sufficiency judge，避免把不稳定性放大到每一轮决策里。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

from .llm_client import get_llm_client
from .rag import retrieve as hybrid_retrieve


REWRITE_CACHE_PATH = Path(__file__).parent.parent / "eval" / "llm_query_rewrite_cache.json"


def _load_rewrite_cache() -> Dict[str, List[str]]:
    try:
        data = json.loads(REWRITE_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_rewrite_cache(cache: Dict[str, List[str]]) -> None:
    REWRITE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = REWRITE_CACHE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(REWRITE_CACHE_PATH)


REWRITE_PROMPT = """你是一个信息检索专家。用户问了一个问题，你需要生成 2-3 个不同的搜索查询词，用于在知识库中检索相关信息。

要求：
- 每个查询词 3-12 个字，尽量用关键词而非完整句子
- 从不同角度覆盖用户问题（同义词、相关概念、产品名、故障现象）
- 至少保留一个最贴近原问题的表述
- 返回严格的 JSON 数组格式：["查询1", "查询2", "查询3"]
不要返回其他任何文字。"""


def _rewrite_queries(user_query: str, max_queries: int = 3, strict: bool = False) -> List[str]:
    cache_key = f"gpt-5.6-terra::{user_query}"
    cached = _load_rewrite_cache().get(cache_key)
    if isinstance(cached, list) and cached:
        return [user_query] + [q for q in cached if q != user_query][:max_queries]

    llm = get_llm_client()
    try:
        text = _chat_for_rewrite(llm, user_query)
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            queries = json.loads(match.group())
            if isinstance(queries, list) and queries:
                cleaned = [str(q).strip() for q in queries if str(q).strip()]
                result = [user_query]
                for q in cleaned:
                    if q not in result:
                        result.append(q)
                cache = _load_rewrite_cache()
                cache[cache_key] = result[1:max_queries + 1]
                _save_rewrite_cache(cache)
                return result[:max_queries + 1]
    except Exception as e:
        print(f"[Agentic Vector LLM RAG] Query rewrite failed: {e}")
        if strict:
            raise RuntimeError(f"LLM query rewrite failed for: {user_query}") from e
    return [user_query]


def _chat_for_rewrite(llm, user_query: str) -> str:
    """Small, bounded LLM call using the shared retrying client."""
    return llm.chat(
        [
            {"role": "system", "content": REWRITE_PROMPT},
            {"role": "user", "content": f"用户问题：{user_query}\n请生成搜索查询词。"},
        ],
        temperature=0.1,
        max_tokens=384,
        max_retries=3,
    )


def _merge_hits(query_to_hits: Dict[str, List[Dict]]) -> List[Dict]:
    merged: Dict[Tuple[str, str], Dict] = {}
    first_query = next(iter(query_to_hits.keys()), "")

    for query, hits in query_to_hits.items():
        for rank, hit in enumerate(hits, 1):
            key = (hit["title"], hit["source"])
            item = merged.setdefault(key, {
                "title": hit["title"],
                "text": hit["text"],
                "source": hit["source"],
                "score": 0.0,
                "max_score": 0.0,
                "hit_count": 0,
                "best_rank": 999,
                "queries": [],
                "original_query_hit": False,
            })
            item["max_score"] = max(item["max_score"], hit.get("score", 0.0))
            item["hit_count"] += 1
            item["best_rank"] = min(item["best_rank"], rank)
            item["queries"].append(query)
            if query == first_query:
                item["original_query_hit"] = True

    results = []
    for item in merged.values():
        # 强化原始 query 命中，防止 LLM 改写把 top1 冲散
        item["score"] = round(
            item["max_score"]
            + (0.8 if item["original_query_hit"] else 0.15)
            + 0.18 * (item["hit_count"] - 1)
            + 0.06 * max(0, 5 - item["best_rank"]),
            4,
        )
        results.append(item)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def _needs_rewrite(query: str, base_hits: List[Dict]) -> bool:
    """Only pay LLM cost on queries where rewrite is likely to help."""
    if len(query) >= 12:
        return True
    if any(token in query for token in ("咋", "那个", "是不是", "连不上", "没反应", "不太会", "帮我看看")):
        return True
    if len(base_hits) < 3:
        return True
    top1 = base_hits[0].get("score", 0.0) if base_hits else 0.0
    top2 = base_hits[1].get("score", 0.0) if len(base_hits) > 1 else 0.0
    if top1 < 8.5:
        return True
    if (top1 - top2) < 0.35:
        return True
    return False


def _blend_base_and_expansion(base_hits: List[Dict], query_to_hits: Dict[str, List[Dict]], top_k: int) -> List[Dict]:
    """Keep original-query ranking as the spine; expansion results only fill gaps."""
    if not query_to_hits:
        return base_hits[:top_k]

    merged = _merge_hits(query_to_hits)
    base_keys = [(h["title"], h["source"]) for h in base_hits]
    merged_by_key = {(h["title"], h["source"]): h for h in merged}

    final_results: List[Dict] = []
    seen = set()

    # Preserve the strongest original-query results first.
    for key in base_keys[: min(2, top_k)]:
        hit = merged_by_key.get(key)
        if hit and key not in seen:
            final_results.append(hit)
            seen.add(key)

    # Then add the best genuinely new evidence from rewrite queries.
    for hit in merged:
        key = (hit["title"], hit["source"])
        if key in seen:
            continue
        final_results.append(hit)
        seen.add(key)
        if len(final_results) >= top_k:
            break

    # If rewrite produced nothing useful, backfill from original ranking.
    if len(final_results) < top_k:
        for hit in base_hits:
            key = (hit["title"], hit["source"])
            if key in seen:
                continue
            final_results.append(hit)
            seen.add(key)
            if len(final_results) >= top_k:
                break

    return final_results[:top_k]


def agentic_llm_hybrid_retrieve(
    query: str,
    top_k: int = 3,
    per_query_k: int = 5,
    max_queries: int = 3,
    use_vector: bool = True,
    strict_rewrite: bool = False,
) -> List[Dict]:
    """LLM query rewrite followed by keyword or vector-hybrid retrieval."""
    base_hits = hybrid_retrieve(query, top_k=max(per_query_k, top_k), use_vector=use_vector)
    if not _needs_rewrite(query, base_hits):
        return base_hits[:top_k]

    queries = _rewrite_queries(query, max_queries=max_queries, strict=strict_rewrite)
    query_to_hits: Dict[str, List[Dict]] = {}
    for q in queries:
        query_to_hits[q] = hybrid_retrieve(q, top_k=per_query_k, use_vector=use_vector)
    return _blend_base_and_expansion(base_hits, query_to_hits, top_k)


def agentic_vector_llm_retrieve(
    query: str,
    top_k: int = 3,
    per_query_k: int = 5,
    max_queries: int = 3,
) -> List[Dict]:
    """Backward-compatible vector-hybrid entry point."""
    return agentic_llm_hybrid_retrieve(
        query,
        top_k=top_k,
        per_query_k=per_query_k,
        max_queries=max_queries,
        use_vector=True,
    )
