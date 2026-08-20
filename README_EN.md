# 🤖 LangGraph Customer Service Agent — Production-Grade AI Customer Service System

> **A production-grade AI Customer Service Agent built with LangGraph, featuring Hybrid RAG, Multi-model LLM Gateway, Full Observability & Self-Improvement Loop.**

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-orange.svg)](https://langchain-ai.github.io/langgraph/)
[![Vue3](https://img.shields.io/badge/Vue3-3.4+-brightgreen.svg)](https://vuejs.org)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 📖 Table of Contents

- [Key Highlights](#-key-highlights)
- [Architecture Overview](#-architecture-overview)
- [Core Capabilities](#-core-capabilities)
- [Quick Start](#-quick-start)
- [Configuration](#-configuration)
- [API Endpoints](#-api-endpoints)
- [Evaluation System](#-evaluation-system)
- [Project Structure](#-project-structure)
- [Deployment](#-deployment)
- [Contributing](#-contributing)

---

## 🌟 Key Highlights

| Dimension | Technical Implementation | Interview Talking Points |
|-----------|--------------------------|--------------------------|
| **State Orchestration** | LangGraph StateGraph 6 nodes + conditional edges + PostgreSQL Checkpointer | "Explicit multi-turn flow: intent → reply → satisfaction → retry/escalate" |
| **Knowledge Retrieval** | Hybrid RAG (Dense+Sparse+RRF) + PgVector + Agentic RAG (rewrite→sufficiency→retry) | "Single retrieval unstable for colloquial queries; Agentic RAG lets LLM rewrite first, then judge sufficiency" |
| **Model Governance** | LLM Gateway: multi-model routing, fallback chain, circuit breaker, token budget, exact cache, cost tracking | "Unified model entry point — business code never calls models directly; handles routing/rate-limiting/cost control" |
| **Security** | Prompt Injection Guard + PII Redaction + 4-layer Rate Limiting + JWT/API Key dual auth | "Input scanned for injection, logs auto-redacted, Redis Lua atomic rate limiting, fail-closed support" |
| **Observability** | Prometheus 11+ metrics + OTel tracing + TraceSession 8 partitions + Alert Service | "Structured Trace per request, PII-redacted persistence, replay & diff support" |
| **Self-Improvement Loop** | Feedback Store + Prompt Registry (versioned + shadow eval) + automated improvement cycle | "Low-score/negative feedback collected → candidate prompts → shadow eval → human approval → release" |
| **Frontend Delivery** | Vue3+Vite: SSE streaming, session list, star ratings, emoji reactions, voice I/O, dark mode, bilingual | "Complete frontend-backend separation, not just a backend demo" |
| **Evaluation System** | 4-layer metrics (Retrieval/Generation/Agent/Engineering) + LLM-as-Judge (debias) + real/offline dual mode | "Retrieval owns recall/ranking, Generation owns faithfulness/completeness, Agent owns tool calls, Engineering owns structure/latency" |

---

## 🏗️ Architecture Overview

```mermaid
graph TD
    A[Vue3 Frontend] -->|HTTP / SSE| B[FastAPI App Layer]
    B -->|Auth / RateLimit / Metrics| C[LangGraph Workflow]
    C --> D[identify_intent]
    D --> E[generate_reply]
    E -->|ending| F[check_satisfaction]
    E -->|retry| G[finalize]
    E -->|normal| H[END]
    F --> I[process_satisfaction]
    I -->|satisfied| G
    I -->|dissatisfied & retry<2| E
    I -->|dissatisfied≥2 or strong_negative| J[escalate_to_human]
    J -->|interrupt| G
    G --> H
    
    C -->|RAG Query| K[RAG Layer]
    K --> L[Hybrid Retrieval]
    L -->|Dense + Sparse + RRF| M[PgVector / TF-IDF Fallback]
    K --> N[Agentic RAG Loop]
    
    C -->|LLM Call| O[LLM Gateway]
    O --> P[Routing + Fallback]
    O --> Q[Token Budget]
    O --> R[Exact Cache]
    O --> S[Circuit Breaker]
    
    C -->|Observability| T[TraceSession]
    T --> U[Prometheus / OTel]
    T --> V[Alert Service]
```

### Data Flow

```
User Query
    ↓
FastAPI: Auth → RateLimit → Metrics → PromptGuard
    ↓
LangGraph: identify_intent (sentiment) → generate_reply
    ↓                                    ↓
                          Agentic RAG (query rewrite → retrieve → evaluate → retry)
                          ↓
                    Hybrid Retrieval (dense+sparse+RRF+rerank)
                          ↓
                    Context Assembler (memory + RAG + tone + compaction)
                          ↓
                    LLM Gateway (route → budget → cache → call → fallback)
                          ↓
                    Reply + Memory Save + Trace Persistence
```

---

## ⚡ Core Capabilities

### 1️⃣ Multi-Turn Conversation Orchestration
- **6 Explicit Nodes**: `identify_intent` → `generate_reply` → `check_satisfaction` → `process_satisfaction` → `escalate_to_human` → `finalize`
- **Smart Routing**: Auto-escalate to human after 2 consecutive dissatisfactions or strong negative emotion (intensity ≥ 4)
- **State Persistence**: PostgreSQL `AsyncPostgresSaver` / `PostgresSaver` — SQLite fallback disabled (prevents silent conversation loss)
- **Interrupt & Resume**: `langgraph.types.interrupt` for human-in-the-loop suspension/resumption

### 2️⃣ Hybrid RAG Knowledge Retrieval
| Feature | Description |
|---------|-------------|
| **Dual-Path Retrieval** | Vector (Embedding) + Keyword (BM25/TF-IDF) |
| **Fusion Strategy** | RRF (k=60) + Rule Reranker + Semantic Deduplication |
| **Parent-Child Chunking** | ~300-char child retrieval → ~1200-char parent output |
| **Backend Options** | `tfidf` (default) / `hybrid` / `pgvector` (PostgreSQL) |
| **Agentic Mode** | Fast (single pgvector) / Deep (LLM rewrite + evaluate + retry, ≤2 rounds) |
| **Graceful Fallback** | pgvector failure → TF-IDF auto-fallback with warning log |

### 3️⃣ LLM Gateway — Unified Model Layer
```python
# Core Capabilities
- Multi-Model Routing: by scene + tenant + tier (nano/fast/balanced/flagship)
- Fallback Chain: primary → backup, circuit breaker per provider/model
- Retry Policy: exponential backoff + full jitter, total deadline constraint (default 60s)
- Token Budget: estimate → reserve → usage → reconcile, daily reset
- Exact Cache: SHA256(tenant+prompt_version+model+messages), low-risk scenes only
- Idempotency: X-Idempotency-Key header for replay protection
- Cost Tracking: versioned MODEL_PRICES table, real usage-based billing
```

### 4️⃣ Security Governance
| Layer | Implementation |
|-------|----------------|
| **Prompt Injection** | System Prompt hardening + input scanning (`agent/security/prompt_guard.py`) |
| **PII Redaction** | Phone/ID/email detection, auto-redaction in logs/traces (`agent/security/pii_redactor.py`) |
| **Rate Limiting** | Redis Lua 4-layer token bucket (global/ip/user/session), fail-closed to local 50% |
| **Authentication** | JWT (HS256) + API Key, query param fallback support |
| **Concurrency Gate** | `asyncio.Semaphore`, returns 429 when exceeded |

### 5️⃣ Observability Stack
- **TraceSession**: 8-partition structured (prompt/retrieval/memory/tool/model/cost/result/latency), auto PII-redacted, finally persisted
- **Metrics**: 11+ Prometheus metrics (`/api/metrics`), built-in text format fallback
- **Alerting**: 30s sliding window, 5 rules (error rate/latency/rate limit/degradation/chat errors)
- **Distributed Tracing**: OTel OTLP export (optional), structured JSON logs + contextvars
- **Replay Tool**: `trace_tool.py list --low-score --failed` / `show` / `replay --rerun` / `diff`

### 6️⃣ Self-Improvement Loop (P4)
```
Low ratings / negative feedback / escalations / repeat questions
        ↓
FeedbackStore collection
        ↓
PromptRegistry generates candidates (candidate → pending_approval → approved)
        ↓
Shadow Evaluation (auto test suite, must pass to enter pending)
        ↓
Human approval → release / rollback
        ↓
CLI: improvement_cycle.py / approve_prompt.py
```

### 7️⃣ Frontend Workbench
- **Stack**: Vue3 + Vite + Pinia + TypeScript
- **Core Features**: Chat workbench, session list (search/export), ⭐ star ratings, 👍 emoji reactions, voice I/O, dark mode, bilingual (CN/EN)
- **Analytics Panel**: KPI cards, intent/emotion distribution charts, rating visualization, auto-refresh
- **SSE Streaming**: Token-level streaming display, auto-cancel on disconnect

---

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- PostgreSQL 15+ (with pgvector extension) — **Production Required**
- Redis 7+ (rate limiting / cache)
- Local LLM service (llama.cpp / vLLM) or Cloud API Key

### 1. Clone & Install Dependencies
```bash
git clone https://github.com/your-org/langgraph-customer-service-agent.git
cd langgraph-customer-service-agent

# Python dependencies
pip install -r requirements.txt

# Frontend dependencies (optional)
cd frontend && npm install && cd ..
```

### 2. Environment Configuration
```bash
cp .env.example .env
# Edit .env with required values:
# OPENAI_BASE_URL=http://your-llm:8080/v1
# OPENAI_API_KEY=sk-xxx
# JWT_SECRET=your-secret-key
# POSTGRES_DSN=postgresql://user:pass@host:5432/db
```

### 3. Launch Options

#### A. Docker Compose (Recommended — Full Stack)
```bash
mkdir -p data logs
docker compose -f docker-compose.prod.yml up -d --build
# With monitoring: docker compose -f docker-compose.prod.yml --profile monitoring up -d
curl localhost:7860/healthz
```

#### B. Local Development (Single Process)
```bash
# Terminal 1: Backend
uvicorn app_fastapi:app --host 0.0.0.0 --port 7860 --reload

# Terminal 2: Frontend
cd frontend && npm run dev
# Open http://localhost:5173 (proxies to :7860)
```

#### C. Production Multi-Worker
```bash
# Reads $WORKERS (default 2)
WORKERS=4 python app_fastapi.py
```

### 4. Verification
```bash
# Health checks
curl http://localhost:7860/healthz
curl http://localhost:7860/api/ready

# Test message
curl -X POST http://localhost:7860/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "How to connect speaker to WiFi?", "stream": true}'
```

---

## ⚙️ Configuration

### Required Environment Variables (Production)

| Variable | Description | Example |
|----------|-------------|---------|
| `OPENAI_BASE_URL` | OpenAI-compatible inference endpoint | `http://llama.cpp:8080/v1` |
| `OPENAI_API_KEY` | Inference API Key | `sk-local` |
| `JWT_SECRET` | HS256 signing key, **required**, else admin endpoints return 403 | `super-secret-key` |
| `POSTGRES_DSN` | PostgreSQL connection string (with pgvector) | `postgresql://user:pass@pg:5432/langgraph` |

### Runtime Parameters

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `7860` | Listen port |
| `WORKERS` | `2` | Uvicorn worker count |
| `LOG_LEVEL` | `INFO` | Log level |
| `GRAPH_TIMEOUT_SECONDS` | `120` | Graph execution timeout; reverse proxy must be higher |
| `SHUTDOWN_TIMEOUT_SECONDS` | `30` | Graceful shutdown drain window |
| `CONCURRENCY_WAIT_SECONDS` | `10` | Concurrency gate wait, then 429 |

### RAG Backend Selection

| `RAG_BACKEND` | Description | Use Case |
|---------------|-------------|----------|
| `tfidf` | Pure TF-IDF + BM25, no external deps | Dev/test/no-GPU |
| `hybrid` | In-process dual-path + RRF + rerank | Single-node prod, no pgvector |
| `pgvector` | PostgreSQL + pgvector, single SQL dual-path | **Production Recommended**, scales |

> **Note**: `pgvector` requires PostgreSQL `pgvector` extension; run `scripts/ingest_knowledge.py` to build index.

### Model Configuration (Optional)
```bash
# Model registry (JSON, takes precedence over env vars)
MODEL_REGISTRY_PATH=config/model_registry.json

# Price version for cost tracking
PRICE_VERSION=2026-07
```

---

## 🔌 API Endpoints

### Core Chat
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/chat` | POST | `{"message":"...", "session_id":"...", "stream":false}` → JSON or SSE streaming |
| `/api/session/<id>` | GET | Session detail (messages, intent, emotion, satisfaction) |
| `/api/sessions` | GET | Session list `?search=keyword&limit=20` |
| `/api/export/<id>` | GET | Export session as JSON |

### Feedback & Rating
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/rating` | POST | `{"session_id":"...", "stars":5}` star rating |
| `/api/reaction` | POST | `{"session_id":"...", "emoji":"👍"}` emoji reaction |
| `/api/feedback` | POST | `{"session_id":"...", "query":"...", "answer":"..."}` text feedback |

### Memory & Analytics
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/memory` | GET | Current user's long-term memories |
| `/api/analytics` | GET | Stats data (for frontend analytics panel) |
| `/api/observability` | GET | Observability metrics (for frontend analytics panel) |

### Auth & Admin
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/login` | POST | `{"username":"...", "password":"..."}` → JWT |
| `/api/auth/register` | POST | User registration |
| `/api/auth/me` | GET | Current user info |
| `/api/admin/prompts` | GET/POST | Prompt Registry admin (requires admin scope) |

### Monitoring & Health
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/healthz` | GET | Liveness probe (k8s liveness) |
| `/api/ready` | GET | Readiness probe (k8s readiness, 503 if unavailable) |
| `/api/health` | GET | Full health (LLM/DB/Redis/RateLimit) |
| `/api/metrics` | GET | Prometheus metrics text format |

---

## 📊 Evaluation System

> Full docs: [`eval/README.md`](eval/README.md) & [`eval/EVAL_METRICS.md`](eval/EVAL_METRICS.md)

### Four-Layer Metrics Framework

| Layer | Core Metrics | Target Thresholds | Evaluation Method |
|-------|--------------|-------------------|-------------------|
| **Retrieval** | Recall@5, HitRate@5, MRR, Context Precision, Context Recall | ≥0.85/0.90/0.80/0.80/0.85 | Rules / Embedding / LLM Judge |
| **Generation** | Faithfulness, Answer Relevance, Completeness, Context Usage, Noise Sensitivity, Refusal Correctness | ≥0.90/0.80/0.80/0.50/≤0.15/≥0.90 | LLM-as-Judge (sentence-level / debiased) |
| **Agent** | Tool Selection Acc, Param Acc, Task Completion, Error Recovery, Stability(pass@N) | ≥0.90/0.85/0.85/0.70/≥0.95 | Trajectory comparison |
| **Engineering** | JSON Validity, Schema Pass Rate, TTFT/E2E p95, Retry Rate, Hallucination Rate | ≥0.98/0.95/observe/≤0.20/≤0.10 | Runtime records |

### Evaluation Datasets

| Dataset | Samples | Characteristics |
|---------|---------|-----------------|
| `golden_set_v2.jsonl` | **114** | Generation core set, chunk-level golden, multi-turn context, sg/multi_hop tags, 24 noise_probes |
| `rag_eval_hard.jsonl` | **92** | Retrieval hard set, 27 semantic gap, 6 multi-hop, 4 refusal, 12 KB full coverage |
| `golden_set.jsonl` | **63** | Early 4-layer set: retrieval(22)+generation(15)+agent(14)+engineering(12) |

### Running Evaluations

```bash
# 1. Offline full run (zero cost, mock mode)
python scripts/run_eval.py --layer all --mock

# 2. Real small run (check cost first)
set RAG_BACKEND=hybrid
python scripts/eval_real.py --mode both --limit 8

# 3. Real full run (requires .env config)
python scripts/eval_real.py --mode both --backend pgvector --csv real_result.csv

# 4. Retrieval only (cheapest)
python scripts/eval_real.py --mode retrieval --backend pgvector
```

### Key Evaluation Results (Latest Real pgvector Run)

| Metric | Real pgvector Report | Offline Mock Baseline |
|--------|---------------------|----------------------|
| HitRate@5 | 1.000 | 1.000 |
| MRR | 1.000 | 1.000 |
| Context Recall | 1.000 | 1.000 |
| Context Precision | 0.783 | 1.000 |
| Faithfulness | 1.000 | 1.000 |
| Answer Relevancy | 0.867 | 1.000 |
| Answer Correctness | 1.000 | 1.000 |
| Citation Accuracy | 0.783 | 1.000 |
| Citation Integrity Rate | 0.333 | — |

> **Improvement Focus**: Context Precision / Citation Accuracy need rerank threshold tuning & citation discipline prompts.

---

## 📁 Project Structure

```
langgraph-customer-service-agent/
├── app_fastapi.py              # FastAPI production entry (P2+)
├── app_original_sync.py        # Archived: sync version (reference only)
├── agent/
│   ├── __init__.py
│   ├── graph.py                # LangGraph workflow definition
│   ├── state.py                # CustomerServiceState (single state definition)
│   ├── nodes.py                # Node implementations (intent/reply/satisfaction/escalate/finalize)
│   ├── runner.py               # Graph execution wrapper (timeout/streaming/error classification)
│   ├── llm_gateway.py          # Multi-model gateway (routing/retry/budget/cache/breaker)
│   ├── hybrid_rag.py           # Hybrid RAG: dual-path + RRF + rerank + parent-child
│   ├── agentic_rag.py          # Agentic RAG: rewrite → sufficiency check → retry
│   ├── pgvector_hybrid.py      # PostgreSQL + pgvector retrieval backend
│   ├── embedding_client.py     # OpenAI-compatible embedding client
│   ├── rate_limiter.py         # 4-layer Redis token bucket limiter
│   ├── auth.py                 # JWT + API Key authentication
│   ├── circuit_breaker.py      # Circuit breaker (per provider/model)
│   ├── context_compaction.py   # Context compression (LLM summary instead of truncation)
│   ├── context_assembler.py    # Context assembly (RAG + Memory + Tone + Compaction)
│   ├── metrics.py              # Prometheus metrics (prometheus_client or builtin fallback)
│   ├── observability.py        # TraceSession + TraceService + AlertService
│   ├── prompt_registry.py      # Versioned prompts + approval workflow
│   ├── feedback_store.py       # Feedback recording (ratings/reactions/escalations/repeats)
│   ├── user_memory.py          # Long-term user memory
│   ├── rag_backend.py          # Runtime retrieval backend router (tfidf/hybrid/pgvector)
│   ├── security/
│   │   ├── prompt_guard.py     # Prompt injection detection
│   │   └── pii_redactor.py     # PII scanning & redaction
│   └── ... (other utility modules)
├── frontend/                    # Vue3 + Vite frontend
│   ├── src/
│   │   ├── App.vue             # Main layout
│   │   ├── components/         # ChatInput/MessageList/SessionSidebar/AnalyticsPanel...
│   │   ├── stores/chat.ts      # Chat state + SSE streaming
│   │   ├── stores/ui.ts        # UI state (sidebar/theme/language)
│   │   └── api/client.ts       # API wrapper + SSE parsing
│   └── package.json
├── knowledge/                   # 13 domain Markdown knowledge bases (auto-loaded)
├── monitoring/                  # Prometheus + Grafana configs
├── eval/                        # Evaluation system
│   ├── harness.py              # Evaluation runner (GoldenCase/Scorer)
│   ├── metrics.py              # 4-layer pure function metrics
│   ├── EVAL_METRICS.md         # Metrics cheatsheet (interview-ready)
│   ├── EVAL_REAL_README.md     # Real evaluation guide
│   └── *.jsonl                 # Evaluation datasets
├── scripts/                     # Utility scripts
│   ├── eval_real.py            # End-to-end real evaluation
│   ├── eval_retrieval.py       # Retrieval benchmark
│   ├── improvement_cycle.py    # P4 self-improvement cycle
│   ├── approve_prompt.py       # Prompt approval CLI
│   ├── trace_tool.py           # Trace replay/diff
│   ├── ingest_knowledge.py     # Knowledge ingestion to pgvector
│   ├── validate_golden_set.py  # Dataset validator
│   ├── fix_golden_sets.py      # Auto-fix tool
│   └── gen_noise_probes.py     # Noise probe generator
├── templates/                   # Legacy HTML templates (fallback)
├── docker-compose.prod.yml      # Production Docker Compose
├── DEPLOYMENT.md               # Deployment guide
├── HEALTHCHECK.md              # Health endpoints + alerting reference
└── README.md                    # This file
```

---

## 🐳 Deployment

### Docker Compose Production
```yaml
# docker-compose.prod.yml includes:
# - app (FastAPI multi-worker)
# - postgres (pgvector enabled)
# - redis (rate limiting / cache)
# - prometheus + grafana (optional profile)
```

```bash
# 1. Prepare directories
mkdir -p data logs monitoring/grafana/dashboards

# 2. Configure environment
cp .env.example .env
# Must change: POSTGRES_DSN, OPENAI_BASE_URL, OPENAI_API_KEY, JWT_SECRET

# 3. Launch
docker compose -f docker-compose.prod.yml up -d --build

# 4. Launch monitoring (optional)
docker compose -f docker-compose.prod.yml --profile monitoring up -d
# Grafana: http://localhost:3000 (admin/admin)
```

### Kubernetes Deployment Notes
- **Checkpoint**: Must use PostgreSQL (`USE_POSTGRES=1`), SQLite disabled
- **Graceful Shutdown**: `preStop` hook waits `SHUTDOWN_TIMEOUT_SECONDS`, configure `terminationGracePeriodSeconds`
- **Concurrency Control**: `MAX_CONCURRENT_REQUESTS` + `RATE_LIMIT_*` limits per-pod throughput
- **Metrics Aggregation**: Multi-replica needs Prometheus multiprocess mode (`prometheus_multiproc_dir`)

### Known Limitations (see [DEPLOYMENT.md](DEPLOYMENT.md))
- SSE token granularity depends on LangGraph version; legacy falls back to node-level slices
- ContextOverflowError compression retry only once; still overflow returns 413
- `/api/sessions` reads SQLite directly; needs pagination/materialized view at scale
- Human escalation suspends Graph; no HTTP resume endpoint yet
- Trace/Feedback SQLite no auto-cleanup; requires periodic archival
- Multi-worker `/api/metrics` returns single-process values; precise aggregation needs multiprocess mode

---

## 🤝 Contributing

### Development Standards
```bash
# Syntax check
python -m py_compile agent/*.py

# Unit tests
python -m pytest tests/ -v

# Code style
ruff check .
ruff format .
```

### Commit Message Convention
```
feat: add Agentic RAG query rewrite
fix: fix ContextOverflowError compression retry logic
docs: update evaluation metrics documentation
refactor: refactor LLM Gateway retry strategy
chore: clean up temp files
```

### Evaluation-Driven Development
1. Before changing retrieval/generation, run `python scripts/run_eval.py --layer all --mock`
2. Watch Δ comparison; ensure core metrics don't regress
3. Real eval must `--limit` small-run first to estimate cost

---

## 📚 Documentation Index

| Document | Description |
|----------|-------------|
| [DEPLOYMENT.md](DEPLOYMENT.md) | Multi-worker deploy, graceful shutdown, known limits |
| [HEALTHCHECK.md](HEALTHCHECK.md) | Health endpoints, Prometheus metrics, alert rules |
| [docs/TRACE_REPLAY.md](docs/TRACE_REPLAY.md) | Trace replay, diff, RAG iteration debugging |
| [docs/PROMPT_SYSTEM.md](docs/PROMPT_SYSTEM.md) | Prompt Registry workflow, self-improvement cycle |
| [docs/RAG_UPGRADE.md](docs/RAG_UPGRADE.md) | pgvector migration runbook |
| [eval/README.md](eval/README.md) | Evaluation system usage guide |
| [eval/EVAL_METRICS.md](eval/EVAL_METRICS.md) | 4-layer metrics definitions, hand-calc examples, interview cheatsheet |

---

## 📄 License

MIT License — see [LICENSE](LICENSE)

---

## 🙏 Acknowledgments

- [LangGraph](https://github.com/langchain-ai/langgraph) — Stateful Agent orchestration framework
- [pgvector](https://github.com/pgvector/pgvector) — PostgreSQL vector search
- [SiliconFlow](https://siliconflow.cn) — Embedding & Reranker API
- [Vue.js](https://vuejs.org) / [Vite](https://vitejs.dev) — Frontend framework

---

> **Interview Prep Tip**: This project upgrades "raw model output" to "controllable system" — core pillars are **LangGraph Orchestration** + **Hybrid/Agentic RAG** + **LLM Gateway Governance** + **Security/Observability/Evaluation Loop**. Prepare with `INTERVIEW_GUIDE.md` and `EVAL_METRICS.md`.

---

## 📝 中文版已在 [README.md](README.md) 中完整提供

The Chinese version is fully included in [README.md](README.md).