# Fair Four-RAG Retrieval Evaluation

## Reproducible evaluation setup

- Dataset: 55 enriched customer-service benchmark queries.
- Knowledge base: 64 sections.
- Embedding: local `Qwen3-Embedding-8B-Q6_K.gguf` at `http://127.0.0.1:8080/v1`.
- Vector search: in-memory cosine similarity, fused with BM25 through RRF.
- LLM query rewriting: `gpt-5.6-terra` at `https://docode.cc/v1`.
- LLM rewrite cache: `llm_query_rewrite_cache.json`, built successfully for all **55/55** queries before this evaluation.
- Fairness control: `DISABLE_QUERY_SYNONYM_EXPANSION=1`; the legacy hand-authored synonym dictionary was disabled for every method.

## Methods

1. **Standard RAG** — BM25 keyword retrieval only.
2. **LLM Agentic Keyword RAG** — real LLM query rewrite, then BM25 retrieval across original and rewritten queries.
3. **Vector Hybrid RAG** — BM25 + local embedding vector search + RRF; no LLM rewrite.
4. **LLM Agentic Vector Hybrid RAG** — real LLM query rewrite, then BM25 + local embedding vector search + RRF for each query variant.

## Core metrics

| Method | HitRate@1 | HitRate@3 | Recall@3 | MRR | Coverage |
|---|---:|---:|---:|---:|---:|
| Standard RAG | 40.0% | 67.3% | 78.5% | 0.524 | 98.2% |
| LLM Agentic Keyword RAG | 40.0% | 70.9% | 98.5% | **0.539** | 100.0% |
| Vector Hybrid RAG | 40.0% | **74.5%** | 96.4% | **0.539** | 100.0% |
| LLM Agentic Vector Hybrid RAG | 40.0% | 72.7% | **103.6%** | 0.533 | 100.0% |

## Enriched source metrics

| Method | Preferred Hit@3 | Acceptable Hit@1 | Acceptable Hit@3 | Preferred MRR | Acceptable MRR |
|---|---:|---:|---:|---:|---:|
| Standard RAG | 65.5% | 65.5% | 83.6% | 0.518 | 0.733 |
| LLM Agentic Keyword RAG | 70.9% | 65.5% | 85.5% | **0.539** | 0.739 |
| Vector Hybrid RAG | 70.9% | **74.5%** | **87.3%** | 0.527 | **0.800** |
| LLM Agentic Vector Hybrid RAG | **72.7%** | **74.5%** | 85.5% | 0.533 | 0.794 |

## Interpretation

- The old rule-based `simulated_agentic_rag` result is intentionally excluded: it was not LLM Agentic RAG and its hand-authored vocabulary overlapped the benchmark.
- Real LLM rewriting improves BM25 retrieval materially over the no-rewrite keyword baseline: HitRate@3 +3.6 points and Recall@3 +20.0 points.
- Vector Hybrid RAG is the best current overall choice for this knowledge base: highest HitRate@3, acceptable Hit@1/3, and acceptable MRR.
- Adding LLM rewriting on top of Vector Hybrid improves preferred-source HitRate@3 and aggregate recall, but currently slightly lowers acceptable Hit@3 and ranking metrics. The likely cause is multi-query result merging; this needs learned/LLM reranking or stronger original-query score preservation.
- Recall@3 can exceed 100% under this repository's existing metric because it accumulates hits across multi-source gold labels instead of clipping each query to 1.0. Use it for within-report comparison only.

## Artifacts

- Full per-query metrics: `four_rag_eval_local.json`
- Real LLM rewrite cache: `llm_query_rewrite_cache.json`
- Cache builder: `build_llm_rewrite_cache.py`
- Evaluation runner: `run_four_rag_eval_local.py`
