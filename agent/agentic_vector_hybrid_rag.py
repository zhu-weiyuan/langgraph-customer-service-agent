# -*- coding: utf-8 -*-
"""
Agentic Vector Hybrid RAG

目标：在不依赖外部 LLM 稳定性的前提下，把“多轮 query 改写”
和“BM25 + Vector + RRF 混合检索”组合起来。

策略：
1. 第一轮直接用原始 query 走 hybrid retrieval
2. 若结果分数不够集中 / query 口语化较强，则生成规则改写 variants
3. 每轮都调用 agent.rag.retrieve(..., use_vector=True)
4. 合并去重，按最大分数 + 多 query 命中加权重排
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .rag import retrieve as hybrid_retrieve


SYNONYM_MAP = {
    "咋整": "怎么办", "咋连": "怎么连接", "咋开": "怎么开发票",
    "咋退": "怎么退货", "咋升": "怎么升级", "咋配": "怎么配对",
    "WiFi": "无线网络连接", "Wi-Fi": "无线网络连接",
    "固件": "设备固件升级", "音箱": "智能音箱",
    "没声": "没声音", "离线": "设备离线", "断网": "网络连接失败",
    "退货": "退换货政策", "退款": "退换货政策",
    "保修": "保修服务", "维修": "保修服务",
    "发票": "开具发票", "开票": "开具发票",
    "配对": "设备配对", "连不上": "连接失败",
    "抽风": "故障排除", "死机": "设备无法启动",
}

STOP_WORDS = {
    '的', '了', '是', '在', '有', '和', '就', '都', '而',
    '与', '及', '这', '那', '吧', '吗', '呢', '啊', '哦',
    '什么', '怎么', '如何', '可以', '能够', '请问', '一下',
    '帮我', '我想', '我要', '能不能', '有没有'
}


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[:max_len]


def _generate_variants(query: str, max_variants: int = 4) -> List[str]:
    """规则改写：尽量轻量，但覆盖口语化/缩写/长 query。"""
    variants = [query]

    expanded = query
    for slang, standard in sorted(SYNONYM_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        if slang in expanded:
            expanded = expanded.replace(slang, f"{slang} {standard}")
    if expanded != query:
        variants.append(expanded)

    core = ''.join(ch for ch in query if ch not in STOP_WORDS and ch.strip())
    if core and core != query:
        variants.append(core)

    if len(query) > 6:
        variants.append(_truncate(query, 6))

    # 针对多意图长问题，再补一条更短关键词串
    if len(core) > 8:
        variants.append(_truncate(core, 8))

    seen = set()
    result = []
    for q in variants:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            result.append(q)
    return result[:max_variants]


def _should_expand(query: str, hits: List[Dict], top_k: int) -> bool:
    """是否需要第二轮：偏保守，宁可多搜一轮。"""
    if len(hits) < top_k:
        return True
    if len(query) >= 12:
        return True
    if any(token in query for token in ("咋", "嘛", "那个", "那个啥", "是不是", "对了")):
        return True
    top1 = hits[0].get("score", 0.0) if hits else 0.0
    top2 = hits[1].get("score", 0.0) if len(hits) > 1 else 0.0
    # 最高分不高，或前两名差距太小，说明还可以再搜一轮
    return top1 < 10.0 or (top1 - top2) < 0.5


def _merge_hits(query_to_hits: Dict[str, List[Dict]]) -> List[Dict]:
    merged: Dict[Tuple[str, str], Dict] = {}

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
            })
            item["max_score"] = max(item["max_score"], hit.get("score", 0.0))
            item["hit_count"] += 1
            item["best_rank"] = min(item["best_rank"], rank)
            item["queries"].append(query)

    results = []
    for item in merged.values():
        # 排序分：最大分数 + 多 query 复现奖励 + 排名奖励
        item["score"] = round(
            item["max_score"]
            + 0.35 * (item["hit_count"] - 1)
            + 0.08 * max(0, 5 - item["best_rank"]),
            4,
        )
        results.append(item)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def agentic_vector_hybrid_retrieve(
    query: str,
    top_k: int = 3,
    per_query_k: int = 5,
    max_rounds: int = 2,
) -> List[Dict]:
    """多轮规则改写 + hybrid retrieval。"""
    query_to_hits: Dict[str, List[Dict]] = {}

    round1_queries = [query]
    for q in round1_queries:
        query_to_hits[q] = hybrid_retrieve(q, top_k=per_query_k, use_vector=True)

    merged_round1 = _merge_hits(query_to_hits)
    if max_rounds <= 1 or not _should_expand(query, merged_round1, top_k=top_k):
        return merged_round1[:top_k]

    for q in _generate_variants(query):
        if q not in query_to_hits:
            query_to_hits[q] = hybrid_retrieve(q, top_k=per_query_k, use_vector=True)

    merged = _merge_hits(query_to_hits)
    return merged[:top_k]


def agentic_vector_hybrid_context(query: str, top_k: int = 3, max_length: int = 1500) -> str:
    """与 build_context 风格一致的 context 构造。"""
    results = agentic_vector_hybrid_retrieve(query, top_k=top_k)
    if not results:
        return ""

    parts = ["\n## 参考资料（知识库 · Agentic Vector Hybrid RAG）\n"]
    total_length = len(parts[0])
    for i, section in enumerate(results, 1):
        section_text = section["text"]
        if len(section_text) > 500:
            section_text = section_text[:500] + "..."
        block = f"\n### [{i}] {section['title']}\n{section_text}\n"
        if total_length + len(block) > max_length and i > 1:
            break
        parts.append(block)
        total_length += len(block)
    return "".join(parts)

