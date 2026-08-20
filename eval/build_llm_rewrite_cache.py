# -*- coding: utf-8 -*-
"""Build a resumable, on-disk cache of strict LLM query rewrites for evaluation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.benchmark_enriched import ENRICHED_BENCHMARK_DATASET
from agent.agentic_vector_llm_rag import _rewrite_queries, _load_rewrite_cache


def main() -> None:
    total = len(ENRICHED_BENCHMARK_DATASET)
    failures = []
    for index, item in enumerate(ENRICHED_BENCHMARK_DATASET, 1):
        query = item["query"]
        key = f"gpt-5.6-terra::{query}"
        if key in _load_rewrite_cache():
            print(f"[{index}/{total}] cached: {query}", flush=True)
            continue
        try:
            rewrites = _rewrite_queries(query, max_queries=3, strict=True)
            print(f"[{index}/{total}] saved: {query} -> {rewrites[1:]}", flush=True)
        except Exception as exc:
            failures.append({"query": query, "error": str(exc)})
            print(f"[{index}/{total}] failed: {query} ({exc})", flush=True)

    print(f"Complete. cached={len(_load_rewrite_cache())}/{total}, failures={len(failures)}", flush=True)
    if failures:
        print("Failed queries:", [x["query"] for x in failures], flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
