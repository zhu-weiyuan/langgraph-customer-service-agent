# Evaluation System — LangGraph Customer Service Agent

Four-layer metric system for RAG → Generation → Agent → Engineering quality assessment.

---

## Quick Start

```bash
# 1) Dry-run with mocks (zero cost, validates pipeline logic)
python scripts/run_eval.py --layer all --mock

# 2) Retrieval-only benchmark (cheap, no LLM generation)
python scripts/eval_retrieval.py --backend tfidf

# 3) Full real eval (produces cost — start with small limit)
python scripts/eval_real.py --mock --mode both --limit 5
python scripts/eval_real.py --mode both --backend hybrid --limit 10
python scripts/eval_real.py --mode retrieval --backend pgvector
```

---

## Metric System (4 Layers)

See [EVAL_METRICS.md](EVAL_METRICS.md) for full formula reference.

| Layer | Core Question | Key Metrics |
|-------|--------------|-------------|
| **Retrieval** | Did we find the right evidence? Ranked well? | Recall@k, HitRate@k, MRR, Context Precision, Context Recall |
| **Generation** | Is the answer faithful, on-point, complete? Used context? | Faithfulness, Answer Relevance, Completeness, Context Usage, Noise Sensitivity, Refusal Correctness |
| **Agent** | Right tools? Correct args? Recovered from failures? | Tool Selection Accuracy, Parameter Accuracy, Unnecessary Call Rate, Task Completion Rate, Error Recovery Rate, Stability |
| **Engineering** | Output well-structured? Latency/cost/retry under control? | JSON Validity, Schema Pass Rate, Enum Accuracy, TTFT, E2E Latency (p95/p99), Retry Rate, Hallucination Rate |

Target thresholds defined in `eval/harness.py:TARGETS`.

---

## Files

| File | Purpose |
|------|---------|
| `eval/metrics.py` | Pure-function metric implementations; injectable judge_fn/embed_fn |
| `eval/harness.py` | EvalRunner with GoldenCase, scorers, TARGETS config |
| `eval/EVAL_METRICS.md` | Metric formulas + target thresholds + hand-calc examples |
| `eval/rag_eval_hard.jsonl` | Hard evaluation dataset (92 cases with KB-grounded annotations) |
| `eval/golden_set.jsonl` | Core golden set (50 cases: normal 25 / edge 13 / adversarial 7 / high_weight 5) |
| `scripts/run_eval.py` | Layer-selectable eval runner |
| `scripts/eval_real.py` | End-to-end real eval: retrieval + generation + LLM-as-Judge scoring |
| `scripts/eval_retrieval.py` | Retrieval-only benchmark (no LLM cost) |
| `scripts/_gen_hard_eval.py` | Dataset source generator — modify corpus then re-run to regenerate jsonl |

---

## Real Eval Cost Warning

Real mode calls per question:
- **Embedding**: 1× for query embedding (plain); more for agentic (rewrite variants + multi-round)
- **Generation LLM**: 1× plain; 2–3× agentic (rewrite + optional evaluate + generate)
- **Judge LLM**: 1× pointwise score × 2 (plain+agentic) + 2× pairwise comparison (debias by swapping A/B order)

≈ **6–9 LLM calls per question**. Full 92-case run is expensive — always start with `--limit`.

Mock mode (`--mock`) produces zero cost.

---

## Golden Set Schema

```json
{
  "id": "case_001",
  "category": "normal|edge|adversarial",
  "difficulty": "easy|medium|hard",
  "query": "用户问题原文",
  "expected_keywords": ["关键词1", "关键词2"],
  "should_refuse": false,
  "weight": 1
}
```

Weights: normal=1, edge=1, adversarial=2, high_weight=3.

---

## Evaluation Reports (Archived)

| File | Content |
|------|---------|
| `eval/report.md` | Baseline TF-IDF retrieval report |
| `eval/report_comparison.md` | Hybrid vs TF-IDF comparison |
| `eval/vector_agentic_report.md` | Agentic RAG report |
| `eval/four_rag_eval_local_report.md` | Four-RAG local comparison |
| `eval/RAG_COMPARISON_REPORT.md` | PgVector vs hybrid vs TF-IDF comparison |
| `eval/report_ragas_v2.md` | Ragas v2 evaluation run |
| `eval/BENCHMARK_ENRICHED_NOTES.md` | Benchmark enrichment notes |
