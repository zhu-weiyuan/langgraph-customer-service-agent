# -*- coding: utf-8 -*-
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from eval.benchmark_dataset import BENCHMARK_DATASET, SUMMARY_STATS
from agent.rag import retrieve as hybrid_retrieve, _load_knowledge_base
from eval.run_comparison import _generate_keyword_variants


def compute_metrics(results: List[Dict]) -> Dict[str, float]:
    if not results:
        return {"hit_rate_1": 0.0, "hit_rate_3": 0.0, "recall_at_3": 0.0, "mrr": 0.0, "coverage": 0.0}
    n = len(results)
    hit1 = sum(1 for r in results if r['hit_rate_1']) / n
    hit3 = sum(1 for r in results if r['hit_rate_3']) / n
    recall3 = sum(r['recall_at_3'] for r in results) / n
    mrr_sum = 0.0
    covered = 0
    for r in results:
        gt = set(r['ground_truth'])
        rr = 0.0
        for idx, src in enumerate(r['retrieved_sources'], 1):
            if src in gt:
                rr = 1.0 / idx
                break
        mrr_sum += rr
        if r['retrieved_sources']:
            covered += 1
    return {
        'hit_rate_1': hit1,
        'hit_rate_3': hit3,
        'recall_at_3': recall3,
        'mrr': mrr_sum / n,
        'coverage': covered / n,
    }


def make_row(item: Dict, retrieved_sources: List[str], extra: Dict = None) -> Dict:
    gt = set(item['ground_truth'])
    hit1 = retrieved_sources[0] in gt if retrieved_sources else False
    hit3 = any(src in gt for src in retrieved_sources[:3])
    recall_num = sum(1 for src in retrieved_sources[:3] if src in gt)
    recall3 = recall_num / len(gt) if gt else 0.0
    row = {
        'query': item['query'],
        'difficulty': item['difficulty'],
        'category': item['category'],
        'ground_truth': list(gt),
        'retrieved_sources': retrieved_sources,
        'hit_rate_1': hit1,
        'hit_rate_3': hit3,
        'recall_at_3': recall3,
    }
    if extra:
        row.update(extra)
    return row


def eval_standard():
    rows = []
    for item in BENCHMARK_DATASET:
        hits = hybrid_retrieve(item['query'], top_k=3, use_vector=False)
        rows.append(make_row(item, [h['source'] for h in hits]))
    return rows


def eval_sim_agentic():
    rows = []
    for item in BENCHMARK_DATASET:
        query = item['query']
        all_hits = []
        seen = set()
        tried = [query]
        for h in hybrid_retrieve(query, top_k=5, use_vector=False):
            key = (h['title'], h['source'])
            if key not in seen:
                seen.add(key)
                all_hits.append(h)
        for variant in _generate_keyword_variants(query):
            tried.append(variant)
            for h in hybrid_retrieve(variant, top_k=3, use_vector=False):
                key = (h['title'], h['source'])
                if key not in seen:
                    seen.add(key)
                    all_hits.append(h)
        all_hits.sort(key=lambda x: x.get('score', 0), reverse=True)
        rows.append(make_row(item, [h['source'] for h in all_hits[:5]], {'queries_tried': tried}))
    return rows


def eval_vector():
    rows = []
    for item in BENCHMARK_DATASET:
        hits = hybrid_retrieve(item['query'], top_k=3, use_vector=True)
        rows.append(make_row(item, [h['source'] for h in hits]))
    return rows


def main():
    os.environ.setdefault('USE_LOCAL_EMBEDDING', '1')
    os.environ.setdefault('LOCAL_EMBEDDING_BASE_URL', 'http://127.0.0.1:8080/v1')
    os.environ.setdefault('EMBEDDING_MODEL', 'D:\\download\\Qwen3-Embedding-8B-Q8_0.gguf')

    _load_knowledge_base()

    standard = eval_standard()
    sim_agentic = eval_sim_agentic()
    vector = eval_vector()

    metrics = {
        'standard_rag': compute_metrics(standard),
        'simulated_agentic_rag': compute_metrics(sim_agentic),
        'vector_hybrid_rag': compute_metrics(vector),
    }

    out = {
        'summary_stats': SUMMARY_STATS,
        'metrics': metrics,
        'standard_rag': standard,
        'simulated_agentic_rag': sim_agentic,
        'vector_hybrid_rag': vector,
    }

    p = Path(__file__).parent / 'three_rag_eval_local.json'
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')

    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f'Saved to: {p}')


if __name__ == '__main__':
    main()
