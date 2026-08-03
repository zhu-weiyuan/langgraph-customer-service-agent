# -*- coding: utf-8 -*-
"""
Evaluate Vector Hybrid RAG and LLM-guided Agentic RAG on the benchmark dataset.

Runs two retrieval settings:
1. Vector Hybrid RAG: BM25 + Vector (via agent.rag.retrieve(use_vector=True))
2. LLM-guided Agentic RAG: multi-round retrieval via agent.agentic_rag.agentic_rag()

Outputs:
- eval/vector_agentic_report.md
- eval/vector_agentic_details.json
"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _compute_metrics(results: List[Dict]) -> Dict[str, float]:
    if not results:
        return {
            "hit_rate_1": 0.0,
            "hit_rate_3": 0.0,
            "recall_at_3": 0.0,
            "mrr": 0.0,
            "coverage": 0.0,
        }

    n = len(results)
    hit1 = sum(1 for r in results if r["hit_rate_1"]) / n
    hit3 = sum(1 for r in results if r["hit_rate_3"]) / n
    recall3 = sum(r["recall_at_3"] for r in results) / n

    mrr_sum = 0.0
    covered = 0
    for r in results:
        gt = set(r["ground_truth"])
        rr = 0.0
        for idx, src in enumerate(r["retrieved_sources"], 1):
            if src in gt:
                rr = 1.0 / idx
                break
        mrr_sum += rr
        if any(src in gt for src in r["retrieved_sources"]):
            covered += 1

    return {
        "hit_rate_1": hit1,
        "hit_rate_3": hit3,
        "recall_at_3": recall3,
        "mrr": mrr_sum / n,
        "coverage": covered / n,
    }


def _metrics_by_difficulty(results: List[Dict]) -> Dict[str, Dict[str, float]]:
    output = {}
    for level in ["L1", "L2", "L3", "L4", "L5"]:
        level_results = [r for r in results if r.get("difficulty") == level]
        if level_results:
            output[level] = _compute_metrics(level_results)
    return output


def _make_result_record(item: Dict, retrieved_sources: List[str], extra: Dict = None) -> Dict:
    ground_truth = set(item["ground_truth"])
    hit_1 = retrieved_sources[0] in ground_truth if retrieved_sources else False
    hit_3 = any(src in ground_truth for src in retrieved_sources[:3])
    recall_numerator = sum(1 for src in retrieved_sources[:3] if src in ground_truth)
    recall_at_3 = recall_numerator / len(ground_truth) if ground_truth else 0.0

    row = {
        "query": item["query"],
        "difficulty": item["difficulty"],
        "category": item["category"],
        "ground_truth": list(ground_truth),
        "retrieved_sources": retrieved_sources,
        "hit_rate_1": hit_1,
        "hit_rate_3": hit_3,
        "recall_at_3": recall_at_3,
    }
    if extra:
        row.update(extra)
    return row


def _report_table(name: str, metrics: Dict[str, float]) -> List[str]:
    return [
        f"## {name}",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| HitRate@1 | {metrics['hit_rate_1']:.1%} |",
        f"| HitRate@3 | {metrics['hit_rate_3']:.1%} |",
        f"| Recall@3 | {metrics['recall_at_3']:.1%} |",
        f"| MRR | {metrics['mrr']:.3f} |",
        f"| Coverage | {metrics['coverage']:.1%} |",
        "",
    ]


def _report_difficulty(title: str, by_diff: Dict[str, Dict[str, float]]) -> List[str]:
    lines = [
        f"### {title}",
        "",
        "| Difficulty | HitRate@1 | HitRate@3 | Recall@3 | MRR | Coverage |",
        "|------------|-----------|-----------|----------|-----|----------|",
    ]
    for level in ["L1", "L2", "L3", "L4", "L5"]:
        m = by_diff.get(level)
        if not m:
            continue
        lines.append(
            f"| {level} | {m['hit_rate_1']:.1%} | {m['hit_rate_3']:.1%} | {m['recall_at_3']:.1%} | {m['mrr']:.3f} | {m['coverage']:.1%} |"
        )
    lines.append("")
    return lines


def main():
    from eval.benchmark_dataset import BENCHMARK_DATASET, SUMMARY_STATS
    from agent.rag import retrieve as hybrid_retrieve, _load_knowledge_base
    from agent.agentic_rag import agentic_rag

    print("=" * 70)
    print("Vector Hybrid RAG + LLM-guided Agentic RAG Evaluation")
    print("=" * 70)
    print(f"Dataset: {SUMMARY_STATS['total']} queries")
    print()

    if os.getenv("USE_LOCAL_EMBEDDING", "1") == "0" and not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required when USE_LOCAL_EMBEDDING=0")

    _load_knowledge_base()

    print("=" * 70)
    print("Phase 1: Vector Hybrid RAG (BM25 + Vector + RRF)")
    print("=" * 70)
    vector_results = []
    for i, item in enumerate(BENCHMARK_DATASET, 1):
        query = item["query"]
        hits = hybrid_retrieve(query, top_k=3, use_vector=True)
        retrieved_sources = [h["source"] for h in hits]
        vector_results.append(_make_result_record(item, retrieved_sources))
        print(f"[{i:02d}/{len(BENCHMARK_DATASET)}] Vector done: {query}")

    vector_metrics = _compute_metrics(vector_results)
    vector_by_diff = _metrics_by_difficulty(vector_results)

    print("=" * 70)
    print("Phase 2: LLM-guided Agentic RAG")
    print("=" * 70)
    agentic_results = []
    for i, item in enumerate(BENCHMARK_DATASET, 1):
        query = item["query"]
        rag_info = agentic_rag(query, max_rounds=2)
        context = rag_info.get("context", "")
        retrieved_sources = []
        for line in context.splitlines():
            if line.startswith("### ["):
                pass
        # Prefer source list from internal details if available; otherwise infer from query attempts
        unique_sources = []
        seen = set()
        for q in rag_info.get("queries_tried", []):
            hits = hybrid_retrieve(q, top_k=3, use_vector=True)
            for h in hits:
                src = h["source"]
                if src not in seen:
                    seen.add(src)
                    unique_sources.append(src)
        retrieved_sources = unique_sources[:5]
        agentic_results.append(_make_result_record(item, retrieved_sources, {
            "queries_tried": rag_info.get("queries_tried", []),
            "rounds": rag_info.get("rounds", 0),
            "sufficient": rag_info.get("sufficient", False),
        }))
        print(f"[{i:02d}/{len(BENCHMARK_DATASET)}] Agentic done: {query}")

    agentic_metrics = _compute_metrics(agentic_results)
    agentic_by_diff = _metrics_by_difficulty(agentic_results)

    report_lines = [
        "# Vector Hybrid RAG vs LLM-guided Agentic RAG",
        "",
        f"Dataset size: {SUMMARY_STATS['total']}",
        "",
    ]
    report_lines.extend(_report_table("Vector Hybrid RAG", vector_metrics))
    report_lines.extend(_report_difficulty("Vector Hybrid RAG by Difficulty", vector_by_diff))
    report_lines.extend(_report_table("LLM-guided Agentic RAG", agentic_metrics))
    report_lines.extend(_report_difficulty("LLM-guided Agentic RAG by Difficulty", agentic_by_diff))

    report_lines.extend([
        "## Summary",
        "",
        "| Metric | Vector Hybrid RAG | LLM-guided Agentic RAG | Delta |",
        "|--------|-------------------|-------------------------|-------|",
    ])
    for key in ["hit_rate_1", "hit_rate_3", "recall_at_3", "mrr", "coverage"]:
        v = vector_metrics[key]
        a = agentic_metrics[key]
        delta = a - v
        if key == "mrr":
            report_lines.append(f"| {key} | {v:.3f} | {a:.3f} | {delta:+.3f} |")
        else:
            report_lines.append(f"| {key} | {v:.1%} | {a:.1%} | {delta:+.1%} |")

    report_path = Path(__file__).parent / "vector_agentic_report.md"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    details_path = Path(__file__).parent / "vector_agentic_details.json"
    with open(details_path, "w", encoding="utf-8") as f:
        json.dump({
            "summary_stats": SUMMARY_STATS,
            "vector_hybrid_rag": vector_results,
            "llm_guided_agentic_rag": agentic_results,
            "metrics": {
                "vector_hybrid_rag": vector_metrics,
                "llm_guided_agentic_rag": agentic_metrics,
            },
            "by_difficulty": {
                "vector_hybrid_rag": vector_by_diff,
                "llm_guided_agentic_rag": agentic_by_diff,
            },
        }, f, ensure_ascii=False, indent=2)

    print(f"\nReport saved to: {report_path}")
    print(f"Details saved to: {details_path}")


if __name__ == "__main__":
    main()
