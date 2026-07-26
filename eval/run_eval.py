#!/usr/bin/env python3
"""Run RAG evaluation against benchmark dataset."""

import sys
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.rag import retrieve
from eval.ragas_eval import RAGEvaluator, QueryResult, BENCHMARK_DATASET


def main():
    print("=" * 60)
    print("RAG Evaluation Pipeline")
    print("=" * 60)

    evaluator = RAGEvaluator()
    results = []

    for i, item in enumerate(BENCHMARK_DATASET, 1):
        query = item["query"]
        ground_truth = item["ground_truth"]

        # Retrieve
        hits = retrieve(query, top_k=5)
        retrieved_sources = [h["source"] for h in hits]
        retrieved_texts = [h["text"][:100] for h in hits]

        result = QueryResult(
            query=query,
            ground_truth_sources=ground_truth,
            retrieved_sources=retrieved_sources,
            retrieved_texts=retrieved_texts,
        )
        results.append(result)

        # Print per-query result
        hit = any(s in set(ground_truth) for s in retrieved_sources[:3])
        status = "✅" if hit else "❌"
        print(f"  [{i:2d}/{len(BENCHMARK_DATASET)}] {status} '{query}' → {retrieved_sources[:3]}")

    # Compute metrics
    report = evaluator.evaluate(results)

    print("\n" + "=" * 60)
    print(report.to_markdown())

    # Save report
    report_path = Path(__file__).parent / "report.md"
    report_path.write_text(report.to_markdown(), encoding="utf-8")
    print(f"Report saved to {report_path}")


if __name__ == "__main__":
    main()
