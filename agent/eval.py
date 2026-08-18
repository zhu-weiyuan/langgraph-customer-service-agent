# -*- coding: utf-8 -*-
"""
RAG Evaluation Module

Provides automated evaluation metrics for the RAG retrieval system:
- Recall@K: What fraction of relevant docs were retrieved in top-K?
- MRR (Mean Reciprocal Rank): Average rank of first relevant result
- HitRate@K: Whether at least one relevant doc appears in top-K
- Coverage: How many queries return any results

Usage:
    python -m agent.eval
    # or import and call evaluate() programmatically
"""

from typing import List, Dict, Tuple, Optional
from .rag import retrieve, _load_knowledge_base


# ─── Ground Truth Test Dataset ────────────────────────────────────────
# Each entry: (query, expected_doc_titles) where doc titles are keywords
# that should appear in the retrieved results' source/title fields.

GROUND_TRUTH: List[Tuple[str, List[str]]] = [
    # Product manual queries
    ("音箱怎么连WiFi", ["product-manual"]),
    ("设备离线怎么办", ["troubleshooting"]),
    ("支持哪些音乐平台", ["product-manual"]),
    ("音量怎么调节", ["product-manual"]),

    # Shipping / logistics queries
    ("快递几天到", ["shipping-logistics"]),
    ("可以自提吗", ["shipping-logistics"]),
    ("发什么快递", ["shipping-logistics"]),

    # Warranty queries
    ("保修多久", ["warranty-service"]),
    ("怎么申请保修", ["warranty-service"]),
    ("保修范围包括什么", ["warranty-service"]),

    # Return / refund queries
    ("怎么退货", ["returns-refunds"]),
    ("退款要多久", ["returns-refunds"]),
    ("七天无理由退货吗", ["returns-refunds"]),

    # Troubleshooting queries
    ("音箱连不上蓝牙", ["troubleshooting"]),
    ("设备无法开机", ["troubleshooting"]),
    ("声音有杂音怎么办", ["troubleshooting"]),

    # FAQ queries
    ("产品价格多少", ["faq"]),
    ("有优惠活动吗", ["faq"]),

    # Invoice / billing queries
    ("怎么开发票", ["product-manual"]),  # invoice info may be in manual or separate doc
    ("可以开增值税专用发票吗", ["product-manual"]),
]


def _matches(result: dict, expected_sources: List[str]) -> bool:
    """Check if a retrieval result matches any expected source."""
    title_lower = (result.get("title", "") + " " + result.get("source", "")).lower()
    for src in expected_sources:
        if src.lower() in title_lower:
            return True
    # Also check text content for keyword overlap
    text_snippet = result.get("text", "")[:200].lower()
    for src in expected_sources:
        if src.replace("-", " ") in text_snippet:
            return True
    return False


def evaluate(
    queries: Optional[List[Tuple[str, List[str]]]] = None,
    top_k: int = 3,
) -> Dict[str, float]:
    """Run RAG evaluation against ground truth dataset.

    Args:
        queries: Custom test set; defaults to GROUND_TRUTH
        top_k: Number of results to evaluate per query

    Returns:
        Dictionary of metric names → scores (0.0-1.0)
    """
    if queries is None:
        queries = GROUND_TRUTH

    n = len(queries)
    hits_at_k = 0       # HitRate@K numerator
    recall_sum = 0.0    # Recall@K sum
    rr_sum = 0.0        # MRR sum (reciprocal ranks)
    coverage_count = 0  # queries with any results

    details = []

    for query, expected_sources in queries:
        results = retrieve(query, top_k=top_k)

        if results:
            coverage_count += 1

        # Check each result for relevance
        relevant_found = 0
        first_relevant_rank = len(results) + 1  # worst case: not found

        for rank_idx, result in enumerate(results):
            if _matches(result, expected_sources):
                relevant_found += 1
                if rank_idx + 1 < first_relevant_rank:
                    first_relevant_rank = rank_idx + 1

        # HitRate@K: at least one relevant result in top-K?
        if relevant_found > 0:
            hits_at_k += 1

        # Recall@K: fraction of expected sources covered
        # (simplified: each query expects docs from 1 source category)
        if relevant_found > 0 and len(expected_sources) > 0:
            recall_sum += min(relevant_found, len(expected_sources)) / len(expected_sources)

        # MRR: reciprocal rank of first relevant result
        if first_relevant_rank <= top_k:
            rr_sum += 1.0 / first_relevant_rank

        details.append({
            "query": query,
            "expected": expected_sources,
            "results_count": len(results),
            "relevant_found": relevant_found,
            "first_rank": first_relevant_rank,
            "hit": relevant_found > 0,
        })

    return {
        "HitRate@K": hits_at_k / n if n > 0 else 0.0,
        "Recall@K": recall_sum / n if n > 0 else 0.0,
        "MRR": rr_sum / n if n > 0 else 0.0,
        "Coverage": coverage_count / n if n > 0 else 0.0,
        "NumQueries": n,
        "TopK": top_k,
        "Details": details,
    }


def print_report(metrics: Dict[str, float]) -> None:
    """Print a formatted evaluation report."""
    print("=" * 60)
    print("RAG Evaluation Report")
    print("=" * 60)
    print(f"  Queries tested : {metrics['NumQueries']}")
    print(f"  Top-K          : {metrics['TopK']}")
    print("-" * 60)
    print(f"  HitRate@{metrics['TopK']}   : {metrics['HitRate@K']:.1%}")
    print(f"  Recall@{metrics['TopK']}    : {metrics['Recall@K']:.1%}")
    print(f"  MRR            : {metrics['MRR']:.3f}")
    print(f"  Coverage       : {metrics['Coverage']:.1%}")
    print("-" * 60)

    # Per-query breakdown
    miss_count = sum(1 for d in metrics["Details"] if not d["hit"])
    if miss_count > 0:
        print(f"\n  Missed queries ({miss_count}):")
        for d in metrics["Details"]:
            if not d["hit"]:
                print(f"    - '{d['query']}' (expected: {d['expected']})")

    hit_count = sum(1 for d in metrics["Details"] if d["hit"])
    print(f"\n  [OK] {hit_count}/{metrics['NumQueries']} queries hit relevant results")
    print("=" * 60)


if __name__ == "__main__":
    # Load KB first
    _load_knowledge_base()

    metrics = evaluate(top_k=3)
    print_report(metrics)
