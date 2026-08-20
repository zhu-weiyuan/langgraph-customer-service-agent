# Advanced Reference — LangGraph Customer Service Agent

Deep-dive reference for deployment, observability, trace replay, RAG upgrades, and the self-improvement loop. See [README.md](../README.md) for architecture overview and quick start.

---

## 1. Deployment

See also: [DEPLOYMENT.md](../DEPLOYMENT.md)

### Multi-worker Notes

Workers do not share process memory. The following state is per-worker:

1. **Rate limiting**: Redis available → 4-layer token bucket globally consistent. Redis down → each worker enters local conservative mode (50% limit independently).
2. **Concurrency gate** (`asyncio.Semaphore`, default 10): global cap ≈ `workers × max_concurrency`.
3. **`/api/metrics`**: returns single-worker metrics; Prometheus scrape will "jump" between workers. For precise aggregation use `WORKERS=1` or prometheus_client multiprocess mode (not built-in).
4. **SQLite**: all writes use WAL + busy_timeout; safe for concurrent multi-worker writes but throughput-limited. Scale to `USE_POSTGRES=1` for high write volume.
5. **AlertService** sliding windows are per-worker; the same rule may fire from multiple workers.
6. **Gateway in-process idempotency cache** is per-worker: same `X-Idempotency-Key` hitting different workers won't dedupe; cross-instance idempotency relies on Redis cache layer.

### Graceful Shutdown

SIGTERM → lifespan shutdown: cancel alert background task → drain inflight requests (≤`SHUTDOWN_TIMEOUT_SECONDS`) → close checkpointer / aiosqlite pool. Compose `stop_grace_period: 30s` aligned with uvicorn `--timeout-graceful-shutdown 35`.

---

## 2. Health & Monitoring

See also: [HEALTHCHECK.md](../HEALTHCHECK.md)

### Health Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /healthz` | Liveness probe only (no LLM/DB/Redis checks). Used by docker healthcheck and k8s livenessProbe. |
| `GET /api/ready` | Readiness: SQLite writable + Redis (degradable) + graph built + config present. Returns 503 if not ready. |
| `GET /api/health` | Full health: LLM reachability (httpx GET `$OPENAI_BASE_URL/models`, 3s timeout), DB summary, rate limiter stats. |

### Prometheus Metrics (`GET /api/metrics`)

| Metric | Type | Labels | Source |
|--------|------|--------|--------|
| `http_requests_total` | counter | method, endpoint, status | Observation middleware |
| `http_request_duration_seconds` | histogram | method, endpoint | Observation middleware |
| `llm_tokens_total` | counter | model, scene, direction | llm_gateway |
| `llm_cost_yuan_total` | counter | model | llm_gateway |
| `llm_requests_total` | counter | model, outcome | llm_gateway |
| `node_duration_seconds` | histogram | node | Graph node timing |
| `rag_hit_ratio` | gauge | — | RAG retrieval |
| `rate_limit_events_total` | counter | tier (global/ip/user/session/concurrency) | 429 handler |
| `circuit_breaker_state` | gauge | name (0=closed, 1=half_open, 2=open) | Circuit breaker |
| `cache_events_total` | counter | cache (hit/miss) | Response cache |
| `feedback_events_total` | counter | kind (rating/reaction/feedback_form) | Feedback endpoints |

### Alert Rules (AlertService — background task, 30s evaluation cycle)

| Rule | Metric (window name) | Condition |
|------|---------------------|-----------|
| `high_http_error_rate` | `http_error` | 5xx > 10 in 5 min |
| `high_latency_p_avg_ms` | `http_latency_ms` | mean > 5000ms in 5 min (≥5 samples) |
| `rate_limit_burst` | `rate_limited` | 429 > 30 in 5 min |
| `limiter_degraded` | `limiter_degraded` | Redis limiter degraded flag set |
| `chat_errors` | `chat_error` | chat 5xx/timeout > 5 in 5 min |

### Probe Configuration Suggestion

```yaml
livenessProbe:  { httpGet: {path: /healthz,   port: 7860}, periodSeconds: 30 }
readinessProbe: { httpGet: {path: /api/ready, port: 7860}, periodSeconds: 10 }
```

---

## 3. Trace Replay & Debugging

Every request creates a `TraceSession` with 8 structured partitions. All PII fields are redacted before persisting to SQLite (`$TRACE_DB`).

### Partition Quick Reference

| Partition | Method | Key Fields |
|-----------|--------|-----------|
| Prompt | `record_prompt()` | template name, version, rendered messages hash |
| Retrieval | `record_retrieval()` | query, chunks (summary), scores, sources, ACL filter result, rerank rank |
| Memory | `record_memory()` | matched memories, source, update time, confidence |
| Tool | `record_tool()` | tool name, args, ACL result, duration, return summary, error |
| Model | `record_model()` | provider, model, sampling params, in/out tokens, TTFT, finish reason |
| Latency | `record_latency()` | entry_ms, retrieval_ms, model_ttft_ms, tool_total_ms, total_ms |
| Cost | `record_cost()` | input cost, output cost, cache hit flag, tenant, scene |
| Result | `record_result()` | final answer, parsed intent/emotion/reply_type, eval score |

### CLI Workflow

```bash
# Find low-score or failed traces
python -m scripts.trace_tool list --low-score
python -m scripts.trace_tool list --failed --user u_123

# View a single request's structured timeline
python -m scripts.trace_tool show <request_id>

# Re-run retrieval with current RAG to compare against what was retrieved at the time
python -m scripts.trace_tool replay <request_id> --rerun

# Diff two traces (e.g., before/after prompt change)
python -m scripts.trace_tool diff <id_before> <id_after>
```

### Programmatic Access

```python
from agent import trace_replay
bad = trace_replay.list_traces({"low_score": True, "scene": "returns"})
for row in bad:
    diff = trace_replay.replay(row["request_id"], "rerun",
                               retriever=my_new_retriever, echo=False)
    # Compare diff["sources_added"] / ["sources_removed"] to assess RAG improvement
```

### Relationship Between Traces, Logs, and Metrics

| Dimension | Carrier | Granularity | Use Case |
|-----------|---------|-------------|----------|
| Trace | `TraceSession` → SQLite | Per-request full dump | Replay a request's prompt/retrieval/model/cost/result |
| Metrics | Prometheus (`/api/metrics`) | Aggregated | Dashboard/alerting: QPS, P95 latency, token/cost totals |
| Log | JSON structured logs | Event stream | Debug: correlate discrete log lines by `request_id` |

All three linked by **`request_id`**: one trace ↔ several log lines ↔若干 metric samples.

---

## 4. RAG Upgrade Runbook (PgVector Migration)

### Quick Steps

```bash
# (a) Start pgvector container (local Docker)
docker run -d --name pgvector -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agent \
  pgvector/pgvector:pg16

# (b) Add to .env:
OPENAI_API_KEY=sk-xxxx
OPENAI_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=text-embedding-3-small
PG_DSN=postgresql://postgres:postgres@localhost:5432/agent
# Do NOT set RAG_BACKEND yet — wait for baseline comparison

# (c) Import knowledge base (dry-run first, then actual)
python scripts/ingest_knowledge.py --dry-run
python scripts/ingest_knowledge.py

# (d) Compare retrieval quality before switching backend
python scripts/eval_retrieval.py --backend tfidf --json > eval/baseline_tfidf.json
python scripts/eval_retrieval.py --backend pgvector --json > eval/candidate_pg.json

# (e) After metrics look good, switch and restart
# Add to .env: RAG_BACKEND=pgvector
python app_fastapi.py

# Rollback: remove RAG_BACKEND line and restart
```

### Backends

| `RAG_BACKEND` | Behavior |
|---------------|----------|
| unset / `tfidf` / invalid | Same as current (rag.py BM25/TF-IDF + vector fusion) |
| `hybrid` | In-process dual-path (rag.py + vector_rag) + RRF + rerank |
| `pgvector` | PgHybridStore.hybrid_search (DB-internal RRF) → RuleReranker → parent mapping |
| Any new backend at runtime failure | Warning log + fallback to TF-IDF for that request only |

### Dependencies

```bash
pip install "psycopg[binary]" pgvector python-dotenv httpx
```

---

## 5. Prompt Self-Improvement (P4)

### Status Machine

```
candidate ──(shadow eval passes)──► pending_approval
                                         │
                    ┌────────────────────┘
                    ▼ (human approve + percent)
                 approved ──(promote_full / released)──► active
                                         │
                         rollback ◄─────┘ (marks retired, restores prev)
```

### CLI Commands

```bash
# Run one cycle: collect feedback → analyze → generate candidates → shadow eval → summary
python scripts/improvement_cycle.py

# Dry-run (no LLM calls, validates pipeline logic)
python scripts/improvement_cycle.py --dry-run

# List available prompt versions and their status
python scripts/approve_prompt.py list

# Approve version 3 with 10% traffic (gray release)
python scripts/approve_prompt.py approve 3 --percent 10

# Promote to full rollout
python scripts/approve_prompt.py promote

# Rollback to previous active version
python scripts/approve_prompt.py rollback
```

### Cron Schedule

```
0 3 * * * cd <repo> && python3 scripts/improvement_cycle.py >> logs/improvement_cycle.log 2>&1
```

### App-Level Wiring (already in app_fastapi.py)

- `POST /api/rating` → `feedback_store.record_rating()` (stars ≤ 3 only)
- `POST /api/reaction` → `feedback_store.record_reaction()` (negative emoji only)
- `POST /api/feedback` → `feedback_store.record_feedback()` (low rating or with comment)
- Chat handler detects escalation → `feedback_store.record_escalation()`
- Chat handler per-turn → `feedback_store.record_repeat_question()`
- `_call_llm` resolves active prompt via `prompt_registry.get_active("system_prompt", ...)`

---

## 6. Known Limits

- **Streaming token granularity** depends on langgraph `astream(stream_mode=["messages","updates"])`; older langgraph guarded fallback slices at node level (per-node burst rather than per-token).
- **ContextOverflowError** compaction retry runs once; if still overflowing returns 413, user must start a new session.
- **`/api/sessions` / `/api/analytics`** read directly from `user_memory.db`; read path uses sqlite3+to_thread. Large historical tables (>1M rows) need pagination or materialization.
- **Escalation** suspends the graph via langgraph interrupt; no HTTP resume endpoint exists yet (manual intervention requires direct checkpointer access, or add `/api/admin/resume` later).
- **Trace/feedback SQLite** not auto-cleaned; set up a cron to archive older entries.
- **Non-streaming `/api/chat`** no longer has the old Redis response cache (cache moved down to llm_gateway layer with tenant×prompt_version×model dimension).
