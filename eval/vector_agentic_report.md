# Vector Hybrid RAG vs LLM-guided Agentic RAG

Dataset size: 55

## Vector Hybrid RAG

| Metric | Value |
|--------|-------|
| HitRate@1 | 50.9% |
| HitRate@3 | 76.4% |
| Recall@3 | 101.5% |
| MRR | 0.621 |
| Coverage | 76.4% |

### Vector Hybrid RAG by Difficulty

| Difficulty | HitRate@1 | HitRate@3 | Recall@3 | MRR | Coverage |
|------------|-----------|-----------|----------|-----|----------|
| L1 | 66.7% | 86.7% | 133.3% | 0.767 | 86.7% |
| L2 | 80.0% | 100.0% | 110.0% | 0.883 | 100.0% |
| L3 | 60.0% | 90.0% | 150.0% | 0.750 | 90.0% |
| L4 | 30.0% | 70.0% | 73.3% | 0.467 | 70.0% |
| L5 | 10.0% | 30.0% | 25.0% | 0.167 | 30.0% |

## LLM-guided Agentic RAG

| Metric | Value |
|--------|-------|
| HitRate@1 | 50.9% |
| HitRate@3 | 80.0% |
| Recall@3 | 71.5% |
| MRR | 0.642 |
| Coverage | 80.0% |

### LLM-guided Agentic RAG by Difficulty

| Difficulty | HitRate@1 | HitRate@3 | Recall@3 | MRR | Coverage |
|------------|-----------|-----------|----------|-----|----------|
| L1 | 66.7% | 86.7% | 86.7% | 0.767 | 86.7% |
| L2 | 80.0% | 100.0% | 80.0% | 0.900 | 100.0% |
| L3 | 60.0% | 90.0% | 90.0% | 0.750 | 90.0% |
| L4 | 30.0% | 80.0% | 58.3% | 0.517 | 80.0% |
| L5 | 10.0% | 40.0% | 35.0% | 0.217 | 40.0% |

## Summary

| Metric | Vector Hybrid RAG | LLM-guided Agentic RAG | Delta |
|--------|-------------------|-------------------------|-------|
| hit_rate_1 | 50.9% | 50.9% | +0.0% |
| hit_rate_3 | 76.4% | 80.0% | +3.6% |
| recall_at_3 | 101.5% | 71.5% | -30.0% |
| mrr | 0.621 | 0.642 | +0.021 |
| coverage | 76.4% | 80.0% | +3.6% |
