# LangGraph Customer Service Agent

A production-grade AI customer service agent built with LangGraph, featuring Hybrid RAG, multi-model LLM Gateway, observability, and a Vue3 frontend.

## Architecture

```
[Vue3 Frontend]
    ↓ HTTP / SSE
[FastAPI App Layer]          ← auth, rate limiting, metrics middleware
    ↓
[LangGraph Workflow]         ← agent/graph.py
  START → identify_intent → generate_reply
                              ├── check_satisfaction → process_satisfaction
                              │                    ├── finalize
                              │                    └── escalate_to_human → finalize
                              └── END (multi-turn)
    ↓
[RAG Layer]                  ← hybrid_rag / pgvector / TF-IDF fallback
    ↓
[LLM Gateway]                ← llm_gateway.py (routing, retry, budget, cache)
```

## Features

### Core Engine
| Feature | Description |
|---------|-------------|
| SSE Streaming | Token-by-token streaming via `agent/runner.py` astream |
| Intent Detection | Auto-classify: consult / complaint / chat / ending |
| Sentiment Analysis | Detects emotion, adjusts bot tone |
| Satisfaction Loop | Retry on dissatisfaction, escalate to human after 2 consecutive failures |
| Context Compaction | Automatic context compression when approaching token limit |
| Human Escalation | LangGraph `interrupt` suspend for manual handling |

### RAG Knowledge Base
- **Hybrid Retrieval**: Dense (vector) + Sparse (BM25/TF-IDF) with RRF fusion
- **PgVector Backend**: PostgreSQL + pgvector via `RAG_BACKEND=pgvector`
- **Agentic RAG**: Query rewrite → retrieve → sufficiency check → retry loop
- **Parent-Child Chunking**: ~300 char child检索 → ~1200 char parent输出
- **Synonym Expansion**: Chinese synonym-aware query rewriting
- **Hot Reload**: `/api/rag/reload` without server restart

### LLM Gateway (`agent/llm_gateway.py`)
- Multi-model routing with tier whitelist (nano/fast/balanced/flagship)
- Fallback chain with circuit breaker per model/provider
- Retry: exponential backoff + full jitter, configurable total deadline
- Token Budget Manager (daily reset, reserve → reconcile)
- Exact SHA256 response cache (tenant × prompt_version × model key)
- Idempotency support via `X-Idempotency-Key` header
- Cost tracking with versioned pricing table

### Security
- **Prompt Injection Guard**: Scans input before processing
- **PII Redaction**: Detects and logs PII in traces
- **Rate Limiting**: 4-layer token bucket (global/ip/user/session) via Redis Lua
- **Auth**: JWT + API key, optional query param fallback
- **Concurrency Gate**: Per-worker `asyncio.Semaphore`, returns 429 when exceeded

### Observability
- **Prometheus Metrics**: `/api/metrics` — 11+ metrics (http, llm, node, cache, rate limit)
- **OpenTelemetry**: Traces export via OTLP (optional, set `OTEL_EXPORTER_OTLP_ENDPOINT`)
- **Request Tracing**: `TraceSession` with 8 structured partitions (prompt/retrieval/memory/tool/model/cost/result/latency), auto-PII-redacted, persisted to SQLite
- **Alert Service**: Background sliding-window alerts for errors, latency, rate limits
- **Structured Logs**: JSON logs with `request_id`/`session_id` context

### Self-Improvement (P4)
- `feedback_store`: Records low ratings, negative reactions, escalations, repeat questions
- `prompt_registry`: Versioned prompts with candidate → pending_approval → approved lifecycle
- Shadow evaluation: Auto-test prompt candidates before promotion
- CLI: `scripts/improvement_cycle.py`, `scripts/approve_prompt.py`

### Frontend (`frontend/`)
- Vue3 + Vite, SSE streaming support
- Session sidebar with search and export
- ⭐ Star ratings + emoji reactions on bot messages
- Dark mode toggle (localStorage persisted)
- Voice I/O via Web Speech API
- Bilingual Chinese / English
- Analytics dashboard at `/analytics`

## Project Structure

```
langgraph-customer-service-agent/
├── app_fastapi.py              # FastAPI entry point (P2+)
├── agent/
│   ├── graph.py                # LangGraph workflow definition
│   ├── state.py                # CustomerServiceState schema
│   ├── nodes.py                # Node implementations (intent/reply/satisfaction)
│   ├── runner.py               # Graph execution wrapper with timeout
│   ├── llm_gateway.py          # Multi-model gateway (routing, retry, budget, cache)
│   ├── hybrid_rag.py           # Hybrid RAG: RRF fusion + rerank + parent-child
│   ├── agentic_rag.py          # Agentic RAG: query rewrite + sufficiency check
│   ├── pgvector_hybrid.py      # PostgreSQL + pgvector retrieval backend
│   ├── embedding_client.py     # OpenAI-compatible embedding client
│   ├── rate_limiter.py         # 4-layer Redis token bucket limiter
│   ├── auth.py                 # JWT + API key authentication
│   ├── circuit_breaker.py      # Per-provider circuit breaker
│   ├── context_compaction.py   # Context compression when near limit
│   ├── metrics.py              # Prometheus metrics (prometheus_client or builtin fallback)
│   ├── observability.py        # TraceSession + TraceService + AlertService
│   ├── prompt_registry.py      # Versioned prompts with approval workflow
│   ├── feedback_store.py       # Feedback recording (ratings, reactions, escalations)
│   ├── user_memory.py          # Long-term user memory store
│   └── security/
│       ├── prompt_guard.py     # Prompt injection detection
│       └── pii_redactor.py     # PII scanning and redaction
├── frontend/                   # Vue3 + Vite chat UI
├── knowledge/                  # Auto-loaded markdown knowledge base
├── monitoring/                 # Prometheus + Grafana configs
├── eval/                       # Evaluation system (see eval/README.md)
│   ├── harness.py              # Eval runner with GoldenCase / scorers
│   ├── metrics.py              # 4-layer metrics (retrieval/generation/agent/engineering)
│   └── rag_eval_*.jsonl        # Evaluation datasets
├── scripts/                    # Utility scripts
│   ├── eval_real.py            # End-to-end real evaluation runner
│   ├── eval_retrieval.py       # Retrieval-only benchmark
│   ├── improvement_cycle.py    # P4 prompt self-improvement cycle
│   └── trace_tool.py           # Trace replay and diff CLI
├── templates/                  # Legacy HTML templates (fallback)
├── docker-compose.prod.yml     # Production Docker Compose
├── DEPLOYMENT.md               # Deployment guide
├── HEALTHCHECK.md              # Health endpoints + alerting reference
└── README.md                   # This file
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env: set OPENAI_BASE_URL, OPENAI_API_KEY, JWT_SECRET
```

### 3. Start the server

```bash
# Single worker (debug)
uvicorn app_fastapi:app --host 0.0.0.0 --port 7860

# Multi-worker (production)
python app_fastapi.py   # reads $WORKERS, default 2
```

### 4. Docker Compose (full stack)

```bash
mkdir -p data logs
docker compose -f docker-compose.prod.yml up -d --build
# + monitoring: docker compose -f docker-compose.prod.yml --profile monitoring up -d
curl localhost:7860/healthz
```

### 5. Run tests

```bash
python -m pytest tests/ -v
python -m py_compile agent/*.py   # syntax check
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Chat UI |
| `/analytics` | GET | Analytics Dashboard |
| `/api/chat` | POST | `{"message":"...", "session_id":"...", "stream":false}` → bot reply (JSON or SSE) |
| `/api/session/<id>` | GET | Session detail (messages, intent, emotion) |
| `/api/sessions` | GET | List sessions (`?search=keyword`) |
| `/api/export/<id>` | GET | Export session as JSON |
| `/api/rating` | POST | `{"session_id":"...", "stars":5}` — star rating |
| `/api/reaction` | POST | `{"session_id":"...", "emoji":"👍"}` — emoji reaction |
| `/api/feedback` | POST | `{"session_id":"...", "query":"...", "answer":"..."}` — text feedback |
| `/api/memory` | GET | Current user's long-term memories |
| `/api/health` | GET | Full health (LLM, DB, Redis, rate limiter) |
| `/api/ready` | GET | Readiness probe (503 if graph/DB unavailable) |
| `/healthz` | GET | Liveness probe only |
| `/api/metrics` | GET | Prometheus metrics |
| `/api/auth/login` | POST | JWT login (`{"username":"...", "password":"..."}`) |
| `/api/admin/prompts` | GET/POST | Prompt registry admin (JWT scope=admin) |

## Environment Variables

### Required (production)

| Variable | Description |
|----------|-------------|
| `OPENAI_BASE_URL` | OpenAI-compatible inference endpoint |
| `OPENAI_API_KEY` | Inference API key |
| `JWT_SECRET` | HS256 signing key; without it admin endpoints return 403 |

### Runtime

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `7860` | Listen port |
| `WORKERS` | `2` | Uvicorn worker count |
| `LOG_LEVEL` | `INFO` | Log level (JSON structured) |
| `GRAPH_TIMEOUT_SECONDS` | `120` | Graph execution timeout; configure the reverse proxy/LB timeout higher |
| `SHUTDOWN_TIMEOUT_SECONDS` | `30` | Graceful shutdown drain window |
| `CONCURRENCY_WAIT_SECONDS` | `10` | Concurrency gate wait, then 429 |

### Persistence

| Variable | Default | Description |
|----------|---------|-------------|
| `CHECKPOINT_DB` | `checkpoints/checkpoints.db` | LangGraph checkpoint store |
| `USE_POSTGRES` | `0` | Switch to AsyncPostgresSaver |
| `TRACE_DB` | `data/trace.db` | Request trace SQLite |
| `REDIS_URL` | `redis://localhost:6379/0` | Rate limiting / cache (fail-closed fallback) |

### RAG Backend

| Variable | Default | Description |
|----------|---------|-------------|
| `RAG_BACKEND` | `tfidf` | `tfidf` / `hybrid` / `pgvector` |

## Architecture Deep Dives

- [Deployment Guide](DEPLOYMENT.md) — multi-worker notes, graceful shutdown, known limits
- [Health & Monitoring](HEALTHCHECK.md) — health endpoints, Prometheus metrics, alert rules
- [Trace Replay](docs/TRACE_REPLAY.md) — replay requests, compare RAG iterations
- [Prompt System](docs/PROMPT_SYSTEM.md) — prompt registry workflow, self-improvement cycle
- [RAG Upgrade](docs/RAG_UPGRADE.md) — pgvector migration runbook
- [Evaluation System](eval/README.md) — 4-layer metrics, harness usage, real eval

## License

MIT
