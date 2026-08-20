# -*- coding: utf-8 -*-
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from eval.benchmark_dataset import SUMMARY_STATS
from eval.benchmark_enriched import ENRICHED_BENCHMARK_DATASET
from agent.rag import retrieve as hybrid_retrieve, _load_knowledge_base
from agent.agentic_vector_llm_rag import agentic_llm_hybrid_retrieve


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


def compute_enriched_metrics(results: List[Dict]) -> Dict[str, float]:
    if not results:
        return {
            "preferred_hit_rate_1": 0.0,
            "preferred_hit_rate_3": 0.0,
            "acceptable_hit_rate_1": 0.0,
            "acceptable_hit_rate_3": 0.0,
            "preferred_mrr": 0.0,
            "acceptable_mrr": 0.0,
            "acceptable_coverage": 0.0,
        }

    n = len(results)
    pref_hit1 = pref_hit3 = acc_hit1 = acc_hit3 = 0
    pref_mrr = acc_mrr = 0.0
    acc_cov = 0

    for r in results:
        pref = set(r.get("preferred_sources", r.get("ground_truth", [])))
        acc = set(r.get("acceptable_sources", list(pref)))
        retrieved = r.get("retrieved_sources", [])

        if retrieved:
            if retrieved[0] in pref:
                pref_hit1 += 1
            if retrieved[0] in acc:
                acc_hit1 += 1
            if any(src in acc for src in retrieved[:3]):
                acc_cov += 1

        if any(src in pref for src in retrieved[:3]):
            pref_hit3 += 1
        if any(src in acc for src in retrieved[:3]):
            acc_hit3 += 1

        for idx, src in enumerate(retrieved, 1):
            if pref and src in pref:
                pref_mrr += 1.0 / idx
                break
        for idx, src in enumerate(retrieved, 1):
            if acc and src in acc:
                acc_mrr += 1.0 / idx
                break

    return {
        "preferred_hit_rate_1": pref_hit1 / n,
        "preferred_hit_rate_3": pref_hit3 / n,
        "acceptable_hit_rate_1": acc_hit1 / n,
        "acceptable_hit_rate_3": acc_hit3 / n,
        "preferred_mrr": pref_mrr / n,
        "acceptable_mrr": acc_mrr / n,
        "acceptable_coverage": acc_cov / n,
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
        'preferred_sources': list(item.get('preferred_sources', list(gt))),
        'acceptable_sources': list(item.get('acceptable_sources', list(gt))),
        'retrieved_sources': retrieved_sources,
        'hit_rate_1': hit1,
        'hit_rate_3': hit3,
        'recall_at_3': recall3,
    }
    if 'gold_answer_hint' in item:
        row['gold_answer_hint'] = item['gold_answer_hint']
    if extra:
        row.update(extra)
    return row


def eval_standard():
    rows = []
    for item in ENRICHED_BENCHMARK_DATASET:
        hits = hybrid_retrieve(item['query'], top_k=3, use_vector=False)
        rows.append(make_row(item, [h['source'] for h in hits]))
    return rows


def eval_llm_agentic_keyword():
    rows = []
    for item in ENRICHED_BENCHMARK_DATASET:
        hits = agentic_llm_hybrid_retrieve(
            item['query'], top_k=3, per_query_k=5, max_queries=3, use_vector=False,
            strict_rewrite=True,
        )
        rows.append(make_row(item, [h['source'] for h in hits]))
    return rows


def eval_vector():
    rows = []
    for item in ENRICHED_BENCHMARK_DATASET:
        hits = hybrid_retrieve(item['query'], top_k=3, use_vector=True)
        rows.append(make_row(item, [h['source'] for h in hits]))
    return rows


def eval_llm_agentic_vector():
    rows = []
    for item in ENRICHED_BENCHMARK_DATASET:
        hits = agentic_llm_hybrid_retrieve(
            item['query'], top_k=3, per_query_k=5, max_queries=3, use_vector=True,
            strict_rewrite=True,
        )
        rows.append(make_row(item, [h['source'] for h in hits]))
    return rows


def main():
    os.environ.setdefault('USE_LOCAL_EMBEDDING', '1')
    os.environ.setdefault('LOCAL_EMBEDDING_BASE_URL', 'http://127.0.0.1:8080/v1')
    os.environ.setdefault('EMBEDDING_MODEL', 'Qwen3-Embedding-8B-Q6_K.gguf')
    os.environ.setdefault('DISABLE_QUERY_SYNONYM_EXPANSION', '1')

    _load_knowledge_base()

    standard = eval_standard()
    llm_agentic_keyword = eval_llm_agentic_keyword()
    vector = eval_vector()
    llm_agentic_vector = eval_llm_agentic_vector()

    metrics = {
        'standard_rag': compute_metrics(standard),
        'llm_agentic_keyword_rag': compute_metrics(llm_agentic_keyword),
        'vector_hybrid_rag': compute_metrics(vector),
        'llm_agentic_vector_hybrid_rag': compute_metrics(llm_agentic_vector),
    }
    enriched_metrics = {
        'standard_rag': compute_enriched_metrics(standard),
        'llm_agentic_keyword_rag': compute_enriched_metrics(llm_agentic_keyword),
        'vector_hybrid_rag': compute_enriched_metrics(vector),
        'llm_agentic_vector_hybrid_rag': compute_enriched_metrics(llm_agentic_vector),
    }

    out = {
        'summary_stats': SUMMARY_STATS,
        'metrics': metrics,
        'enriched_metrics': enriched_metrics,
        'standard_rag': standard,
        'llm_agentic_keyword_rag': llm_agentic_keyword,
        'vector_hybrid_rag': vector,
        'llm_agentic_vector_hybrid_rag': llm_agentic_vector,
    }

    p = Path(__file__).parent / 'four_rag_eval_local.json'
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')

    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(json.dumps(enriched_metrics, ensure_ascii=False, indent=2))
    print(f'Saved to: {p}')


if __name__ == '__main__':
    main()
