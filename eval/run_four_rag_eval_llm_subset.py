# -*- coding: utf-8 -*-
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from eval.benchmark_dataset import BENCHMARK_DATASET
from eval.run_four_rag_eval_local import (
    compute_metrics,
    make_row,
    eval_standard,
    eval_sim_agentic,
    eval_vector,
)
from agent.rag import retrieve as hybrid_retrieve, _load_knowledge_base
from agent.agentic_vector_llm_rag import agentic_vector_llm_retrieve


def subset_dataset(per_level: int = 4) -> List[Dict]:
    picked = []
    for level in ['L1', 'L2', 'L3', 'L4', 'L5']:
        level_items = [x for x in BENCHMARK_DATASET if x['difficulty'] == level]
        picked.extend(level_items[:per_level])
    return picked


def eval_standard_subset(items):
    rows = []
    for item in items:
        hits = hybrid_retrieve(item['query'], top_k=3, use_vector=False)
        rows.append(make_row(item, [h['source'] for h in hits]))
    return rows


def eval_vector_subset(items):
    rows = []
    for item in items:
        hits = hybrid_retrieve(item['query'], top_k=3, use_vector=True)
        rows.append(make_row(item, [h['source'] for h in hits]))
    return rows


def eval_agentic_vector_llm_subset(items):
    rows = []
    for i, item in enumerate(items, 1):
        hits = agentic_vector_llm_retrieve(item['query'], top_k=3, per_query_k=5, max_queries=3)
        row = make_row(item, [h['source'] for h in hits], {'top_titles': [(h['title'], h['source']) for h in hits]})
        rows.append(row)
        print(f"[{i:02d}/{len(items)}] {item['difficulty']} {item['query']} -> {row['retrieved_sources']}")
    return rows


def main():
    os.environ.setdefault('USE_LOCAL_EMBEDDING', '1')
    os.environ.setdefault('LOCAL_EMBEDDING_BASE_URL', 'http://127.0.0.1:8080/v1')
    os.environ.setdefault('EMBEDDING_MODEL', 'D:\\download\\Qwen3-Embedding-8B-Q8_0.gguf')

    _load_knowledge_base()
    items = subset_dataset()

    standard = eval_standard_subset(items)
    vector = eval_vector_subset(items)
    agentic_vector_llm = eval_agentic_vector_llm_subset(items)

    metrics = {
        'standard_rag': compute_metrics(standard),
        'vector_hybrid_rag': compute_metrics(vector),
        'agentic_vector_llm_rag': compute_metrics(agentic_vector_llm),
    }

    out = {
        'subset_size': len(items),
        'metrics': metrics,
        'standard_rag': standard,
        'vector_hybrid_rag': vector,
        'agentic_vector_llm_rag': agentic_vector_llm,
    }

    p = Path(__file__).parent / 'four_rag_eval_llm_subset.json'
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f'Saved to: {p}')


if __name__ == '__main__':
    main()
