# -*- coding: utf-8 -*-
"""
RAG Comparison Evaluation — Standard RAG vs Simulated Agentic RAG

对比两套检索策略在 55 条 benchmark 数据集上的表现。
不依赖 Vector RAG 和 LLM，纯 BM25+TF-IDF + 关键词自适应。

Usage:
    cd langgraph-customer-service-agent
    python -m eval.run_comparison
"""

import json
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Keyword-based query variants generator ───────────────────
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

STOP_WORDS = {'的', '了', '是', '在', '有', '和', '就', '都', '而',
              '与', '及', '这', '那', '吧', '吗', '呢', '啊', '哦',
              '什么', '怎么', '如何', '可以', '能够', '请问', '一下',
              '帮我', '我想', '我要', '能不能', '有没有'}


def _generate_keyword_variants(query: str) -> List[str]:
    """Generate keyword variants for adaptive retrieval."""
    variants = []
    
    # 1. Synonym expansion
    expanded = query
    sorted_synonyms = sorted(SYNONYM_MAP.items(), key=lambda x: len(x[0]), reverse=True)
    for slang, standard in sorted_synonyms:
        if slang in expanded:
            expanded = expanded.replace(slang, f"{slang} {standard}")
    if expanded != query:
        variants.append(expanded)
    
    # 2. Core keywords (remove stop words)
    core = ''.join([w for w in query if w not in STOP_WORDS and len(w.strip()) > 0])
    if core and core != query:
        variants.append(core)
    
    # 3. Short version
    if len(query) > 4:
        variants.append(query[:4])
    
    return variants[:3]


def run_evaluation():
    import os
    # Vector retrieval reads credentials from the environment; never embed keys in evaluation code.
    # This comparison runs BM25 + TF-IDF only, so no OpenRouter credential is required.
    openrouter_api_key = os.getenv('OPENROUTER_API_KEY', '')
    if openrouter_api_key:
        print("OPENROUTER_API_KEY detected; vector retrieval remains disabled for this comparison.")

    from agent.rag import retrieve as standard_retrieve, _load_knowledge_base
    from eval.benchmark_dataset import BENCHMARK_DATASET, SUMMARY_STATS
    
    print("=" * 70)
    print("RAG Comparison Evaluation")
    print("=" * 70)
    print(f"Dataset: {SUMMARY_STATS['total']} queries")
    print(f"Difficulty levels: L1-L5")
    print()
    
    # Load knowledge base (BM25 + TF-IDF only, no vector)
    _load_knowledge_base()
    
    # ── Phase 1: Standard RAG ───────────────────────────────
    print("=" * 70)
    print("Phase 1: Standard RAG (BM25 + TF-IDF)")
    print("=" * 70)
    
    standard_results = []
    for item in BENCHMARK_DATASET:
        query = item["query"]
        # Standard RAG: BM25 + TF-IDF only (no vector)
        hits = standard_retrieve(query, top_k=3, use_vector=False)
        
        ground_truth = set(item["ground_truth"])
        retrieved_sources = [h["source"] for h in hits]
        
        hit_1 = retrieved_sources[0] in ground_truth if retrieved_sources else False
        hit_3 = any(src in ground_truth for src in retrieved_sources[:3])
        recall_numerator = sum(1 for src in retrieved_sources[:3] if src in ground_truth)
        recall_at_3 = recall_numerator / len(ground_truth) if ground_truth else 0.0
        
        standard_results.append({
            "query": query,
            "difficulty": item["difficulty"],
            "category": item["category"],
            "ground_truth": list(ground_truth),
            "retrieved_sources": retrieved_sources,
            "hit_rate_1": hit_1,
            "hit_rate_3": hit_3,
            "recall_at_3": recall_at_3,
        })
    
    std_metrics = _compute_metrics(standard_results)
    print(f"\nStandard RAG Results:")
    print(f"  HitRate@1   : {std_metrics['hit_rate_1']:.1%}")
    print(f"  HitRate@3   : {std_metrics['hit_rate_3']:.1%}")
    print(f"  Recall@3    : {std_metrics['recall_at_3']:.1%}")
    print(f"  MRR         : {std_metrics['mrr']:.3f}")
    print(f"  Coverage    : {std_metrics['coverage']:.1%}")
    
    # By difficulty
    print("\nBy Difficulty:")
    for level in ["L1", "L2", "L3", "L4", "L5"]:
        level_results = [r for r in standard_results if r["difficulty"] == level]
        if level_results:
            m = _compute_metrics(level_results)
            print(f"  {level}: HitRate@1={m['hit_rate_1']:.1%} "
                  f"HitRate@3={m['hit_rate_3']:.1%} Recall@3={m['recall_at_3']:.1%}")
    
    # ── Phase 2: Simulated Agentic RAG ───────────────────────
    print("\n" + "=" * 70)
    print("Phase 2: Simulated Agentic RAG (keyword-adaptive retrieval)")
    print("=" * 70)
    
    agentic_results = []
    for i, item in enumerate(BENCHMARK_DATASET):
        query = item["query"]
        all_hits = []
        seen_titles = set()
        queries_tried = [query]
        
        # Round 1: Original query
        hits = standard_retrieve(query, top_k=5, use_vector=False)
        for h in hits:
            key = (h["title"], h["source"])
            if key not in seen_titles:
                seen_titles.add(key)
                all_hits.append(h)
        
        # Round 2: Keyword variants
        variants = _generate_keyword_variants(query)
        for variant in variants:
            queries_tried.append(variant)
            hits = standard_retrieve(variant, top_k=3, use_vector=False)
            for h in hits:
                key = (h["title"], h["source"])
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_hits.append(h)
        
        # Sort by score, take top 5
        all_hits.sort(key=lambda x: x.get("score", 0), reverse=True)
        all_hits = all_hits[:5]
        retrieved_sources = [h["source"] for h in all_hits]
        
        ground_truth = set(item["ground_truth"])
        hit_1 = retrieved_sources[0] in ground_truth if retrieved_sources else False
        hit_3 = any(src in ground_truth for src in retrieved_sources[:3])
        recall_numerator = sum(1 for src in retrieved_sources[:3] if src in ground_truth)
        recall_at_3 = recall_numerator / len(ground_truth) if ground_truth else 0.0
        
        agentic_results.append({
            "query": query,
            "difficulty": item["difficulty"],
            "category": item["category"],
            "ground_truth": list(ground_truth),
            "retrieved_sources": retrieved_sources,
            "hit_rate_1": hit_1,
            "hit_rate_3": hit_3,
            "recall_at_3": recall_at_3,
            "queries_tried": queries_tried,
        })
    
    agen_metrics = _compute_metrics(agentic_results)
    print(f"\nSimulated Agentic RAG Results:")
    print(f"  HitRate@1   : {agen_metrics['hit_rate_1']:.1%}")
    print(f"  HitRate@3   : {agen_metrics['hit_rate_3']:.1%}")
    print(f"  Recall@3    : {agen_metrics['recall_at_3']:.1%}")
    print(f"  MRR         : {agen_metrics['mrr']:.3f}")
    print(f"  Coverage    : {agen_metrics['coverage']:.1%}")
    
    # ── Comparison Summary ───────────────────────────────
    print("\n" + "=" * 70)
    print("Comparison Summary")
    print("=" * 70)
    print(f"{'Metric':<20} {'Standard RAG':<15} {'Agentic RAG':<15} {'Delta':<10}")
    print("-" * 60)
    for key in ["hit_rate_1", "hit_rate_3", "recall_at_3", "mrr", "coverage"]:
        std_val = std_metrics[key]
        agen_val = agen_metrics[key]
        delta = agen_val - std_val
        if key == "mrr":
            print(f"{key:<20} {std_val:<15.3f} {agen_val:<15.3f} {delta:+.3f}")
        else:
            print(f"{key:<20} {std_val:<15.1%} {agen_val:<15.1%} {delta:+.1%}")
    
    # ── Generate Report ───────────────────────────────
    report = _generate_report(standard_results, agentic_results, std_metrics, agen_metrics)
    report_path = Path(__file__).parent / "report_comparison.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved to: {report_path}")
    
    # Save detailed JSON
    json_path = Path(__file__).parent / "comparison_details.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "standard_rag": standard_results,
            "agentic_rag": agentic_results,
            "summary_stats": SUMMARY_STATS,
        }, f, ensure_ascii=False, indent=2)
    print(f"Detailed data saved to: {json_path}")


def _compute_metrics(results: List[dict]) -> Dict[str, float]:
    n = len(results)
    if n == 0:
        return {"hit_rate_1": 0, "hit_rate_3": 0, "recall_at_3": 0, "mrr": 0, "coverage": 0}
    
    hit_1_count = sum(1 for r in results if r["hit_rate_1"])
    hit_3_count = sum(1 for r in results if r["hit_rate_3"])
    recall_sum = sum(r["recall_at_3"] for r in results)
    coverage_count = sum(1 for r in results if r["retrieved_sources"])
    
    rr_sum = 0.0
    for r in results:
        ground_truth = set(r["ground_truth"])
        sources = r["retrieved_sources"]
        first_rank = len(sources) + 1
        for i, src in enumerate(sources):
            if src in ground_truth:
                first_rank = i + 1
                break
        if first_rank <= len(sources):
            rr_sum += 1.0 / first_rank
    
    return {
        "hit_rate_1": hit_1_count / n,
        "hit_rate_3": hit_3_count / n,
        "recall_at_3": recall_sum / n,
        "mrr": rr_sum / n,
        "coverage": coverage_count / n,
    }


def _generate_report(std_results, agen_results, std_metrics, agen_metrics) -> str:
    lines = [
        "# RAG Comparison Evaluation Report",
        "",
        "## Standard RAG Results (BM25 + TF-IDF)",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| HitRate@1 | {std_metrics['hit_rate_1']:.1%} |",
        f"| HitRate@3 | {std_metrics['hit_rate_3']:.1%} |",
        f"| Recall@3 | {std_metrics['recall_at_3']:.1%} |",
        f"| MRR | {std_metrics['mrr']:.3f} |",
        f"| Coverage | {std_metrics['coverage']:.1%} |",
        "",
        "### By Difficulty Level",
        "",
        "| Difficulty | Count | HitRate@1 | HitRate@3 | Recall@3 |",
        "|-----------|-------|-----------|-----------|----------|",
    ]
    
    for level in ["L1", "L2", "L3", "L4", "L5"]:
        level_results = [r for r in std_results if r["difficulty"] == level]
        if level_results:
            m = _compute_metrics(level_results)
            lines.append(f"| {level} | {len(level_results)} | "
                        f"{m['hit_rate_1']:.1%} | {m['hit_rate_3']:.1%} | "
                        f"{m['recall_at_3']:.1%} |")
    
    # Missed queries
    lines.extend([
        "",
        "### Missed Queries (Standard RAG)",
        "",
    ])
    missed = [r for r in std_results if not r["hit_rate_3"]]
    if missed:
        for r in missed:
            lines.append(f"- **{r['query']}** (expected: {r['ground_truth']})")
    
    # Agentic RAG results
    lines.extend([
        "",
        "## Simulated Agentic RAG Results",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| HitRate@1 | {agen_metrics['hit_rate_1']:.1%} |",
        f"| HitRate@3 | {agen_metrics['hit_rate_3']:.1%} |",
        f"| Recall@3 | {agen_metrics['recall_at_3']:.1%} |",
        f"| MRR | {agen_metrics['mrr']:.3f} |",
        f"| Coverage | {agen_metrics['coverage']:.1%} |",
        "",
        "## Comparison Summary",
        "",
        "| Metric | Standard RAG | Agentic RAG | Delta |",
        "|--------|-------------|-------------|-------|",
    ])
    
    for key in ["hit_rate_1", "hit_rate_3", "recall_at_3", "mrr", "coverage"]:
        std_val = std_metrics[key]
        agen_val = agen_metrics[key]
        delta = agen_val - std_val
        if key == "mrr":
            lines.append(f"| {key} | {std_val:.3f} | {agen_val:.3f} | {delta:+.3f} |")
        else:
            lines.append(f"| {key} | {std_val:.1%} | {agen_val:.1%} | {delta:+.1%} |")
    
    return "\n".join(lines)


if __name__ == "__main__":
    run_evaluation()
