# 🤖 LangGraph 智能客服 Agent — 生产级 AI 客服系统

> **A production-grade AI Customer Service Agent built with LangGraph, featuring Hybrid RAG, Multi-model LLM Gateway, Full Observability & Self-Improvement Loop.**

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-orange.svg)](https://langchain-ai.github.io/langgraph/)
[![Vue3](https://img.shields.io/badge/Vue3-3.4+-brightgreen.svg)](https://vuejs.org)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 📖 目录 / Table of Contents

- [项目亮点](#-项目亮点--key-highlights)
- [架构概览](#-架构概览--architecture-overview)
- [核心能力](#-核心能力--core-capabilities)
- [快速开始](#-快速开始--quick-start)
- [配置指南](#-配置指南--configuration)
- [API 接口](#-api-接口--api-endpoints)
- [评测体系](#-评测体系--evaluation-system)
- [项目结构](#-项目结构--project-structure)
- [部署指南](#-部署指南--deployment)
- [贡献指南](#-贡献指南--contributing)

---

## 🌟 项目亮点 / Key Highlights

| 维度 | 技术实现 | 面试话术 |
|------|----------|----------|
| **状态编排** | LangGraph StateGraph 6节点 + 条件边路由 + PostgreSQL Checkpointer | "把多轮客服流程显式化：意图识别→回复生成→满意度检测→重试/升级人工" |
| **知识检索** | Hybrid RAG (Dense+Sparse+RRF) + PgVector + Agentic RAG (查询改写→充分性判断→重试) | "单次检索对口语化表达不稳，Agentic RAG 让 LLM 先改写再判断是否够用" |
| **模型治理** | LLM Gateway：多模型路由、Fallback 链、熔断器、Token 预算、精确缓存、成本统计 | "统一模型入口，业务代码不直接调模型，做路由/限流/成本控制" |
| **安全防护** | Prompt Injection Guard + PII 脱敏 + 四层限流 + JWT/API Key 双认证 | "输入先过注入检测，日志脱敏，Redis Lua 原子限流，支持 fail-closed" |
| **可观测性** | Prometheus 11+指标 + OTel 链路追踪 + TraceSession 8分区 + 告警服务 | "每请求结构化 Trace，PII 脱敏落盘，支持回放对比" |
| **自改进闭环** | Feedback Store + Prompt Registry (版本化+影子评测) + 自动化改进周期 | "低分/负向反馈自动收集→生成候选 Prompt→影子评测→人工审批→发布" |
| **前端交付** | Vue3+Vite：SSE流式、会话列表、满意度评分、语音I/O、暗色模式、双语 | "完整前后端分离交付，非纯后端 Demo" |
| **评测体系** | 四层指标(检索/生成/Agent/工程) + LLM-as-Judge(去位置偏差) + 真实/离线双模式 | "检索管召回排序，生成管忠实度/完整性，Agent管工具调用，工程管结构/延迟" |

---

## 🏗️ 架构概览 / Architecture Overview

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

### 数据流向

```
用户提问
    ↓
FastAPI: Auth → RateLimit → Metrics → PromptGuard
    ↓
LangGraph: identify_intent (情感分析) → generate_reply
    ↓                                    ↓
                              Agentic RAG (query rewrite → retrieve → evaluate → retry)
                              ↓
                        Hybrid Retrieval (dense+sparse+RRF+rerank)
                              ↓
                        Context Assembler (memory + RAG + tone + compaction)
                              ↓
                        LLM Gateway (route → budget → cache → call → fallback)
                              ↓
                        回复 + 记忆保存 + Trace 落盘
```

---

## ⚡ 核心能力 / Core Capabilities

### 1️⃣ 多轮对话编排
- **6 个显式节点**：`identify_intent` → `generate_reply` → `check_satisfaction` → `process_satisfaction` → `escalate_to_human` → `finalize`
- **智能路由**：连续 2 次不满意或强负面情绪(强度≥4)自动触发人工升级
- **状态持久化**：PostgreSQL `AsyncPostgresSaver` / `PostgresSaver`，不支持 SQLite 降级（防止静默丢失对话）
- **中断恢复**：`langgraph.types.interrupt` 实现人工介入挂起/恢复

### 2️⃣ Hybrid RAG 知识检索
| 特性 | 说明 |
|------|------|
| **双路召回** | 向量(Embedding) + 关键词(BM25/TF-IDF) |
| **融合策略** | RRF (k=60) + Rule Reranker + 语义去重 |
| **Parent-Child** | 300字 child 检索 → 1200字 parent 输出 |
| **后端可选** | `tfidf` (默认) / `hybrid` / `pgvector` (PostgreSQL) |
| **Agentic 模式** | Fast (单次 pgvector) / Deep (LLM 改写+评估+重试，≤2轮) |
| **自动降级** | pgvector 失败自动回退 TF-IDF，记录警告 |

### 3️⃣ LLM Gateway 统一模型层
```python
# 核心能力
- 多模型路由：按 scene + tenant + tier 选择 (nano/fast/balanced/flagship)
- Fallback 链：主模型失败自动切备用，熔断器 per provider/model
- 重试策略：指数退避 + 全抖动，总 deadline 约束 (默认 60s)
- Token 预算：estimate → reserve → usage → reconcile，按自然日重置
- 精确缓存：SHA256(tenant+prompt_version+model+messages)，仅低风险场景启用
- 幂等键：X-Idempotency-Key 防重放
- 成本统计：版本化价格表 MODEL_PRICES，按真实 usage 计费
```

### 4️⃣ 安全治理
| 防护层 | 实现 |
|--------|------|
| **Prompt 注入** | System Prompt 加固 + 输入扫描 (`agent/security/prompt_guard.py`) |
| **PII 脱敏** | 手机号/身份证/邮箱等检测，日志/Trace 自动脱敏 (`agent/security/pii_redactor.py`) |
| **限流** | Redis Lua 四层令牌桶 (global/ip/user/session)，故障时本地 50% 降级 |
| **认证** | JWT (HS256) + API Key，支持 query 参数兼容 |
| **并发闸** | `asyncio.Semaphore`，超出返回 429 |

### 5️⃣ 可观测性栈
- **TraceSession**：8分区结构化 (prompt/retrieval/memory/tool/model/cost/result/latency)，finally 落盘
- **指标**：11+ Prometheus 指标 (`/api/metrics`)，内置文本格式降级
- **告警**：滑动窗口 30s 评估 5 条规则 (错误率/延迟/限流/降级/聊天错误)
- **链路追踪**：OTel OTLP 导出 (可选)，结构化 JSON 日志 + contextvars
- **回放工具**：`trace_tool.py list --low-score --failed` / `show` / `replay --rerun` / `diff`

### 6️⃣ 自我改进闭环 (P4)
```
低评分/负向反馈/升级/追问
        ↓
FeedbackStore 收集
        ↓
PromptRegistry 生成候选 (candidate → pending_approval → approved)
        ↓
Shadow Evaluation (自动跑测试集，达标才进 pending)
        ↓
人工审批 → 发布 / 回滚
        ↓
CLI: improvement_cycle.py / approve_prompt.py
```

### 7️⃣ 前端工作台
- **技术栈**：Vue3 + Vite + Pinia + TypeScript
- **核心功能**：聊天工作台、会话列表(搜索/导出)、⭐星级评分、👍emoji反应、语音I/O、暗色模式、中英双语
- **分析面板**：KPI 卡片、意图/情绪分布图表、评分可视化、自动刷新
- **SSE 流式**：token 级流式展示，断连自动取消

---

## 🚀 快速开始 / Quick Start

### 前置要求
- Python 3.11+
- PostgreSQL 15+ (with pgvector extension) — **生产必需**
- Redis 7+ (限流/缓存)
- 本地 LLM 服务 (llama.cpp / vLLM) 或云 API Key

### 1. 克隆与依赖
```bash
git clone https://github.com/your-org/langgraph-customer-service-agent.git
cd langgraph-customer-service-agent

# Python 依赖
pip install -r requirements.txt

# 前端依赖 (可选)
cd frontend && npm install && cd ..
```

### 2. 环境配置
```bash
cp .env.example .env
# 编辑 .env 必填项：
# OPENAI_BASE_URL=http://your-llm:8080/v1
# OPENAI_API_KEY=sk-xxx
# JWT_SECRET=your-secret-key
# POSTGRES_DSN=postgresql://user:pass@host:5432/db
```

### 3. 启动方式

#### A. Docker Compose (推荐，一键全栈)
```bash
mkdir -p data logs
docker compose -f docker-compose.prod.yml up -d --build
# 含监控：docker compose -f docker-compose.prod.yml --profile monitoring up -d
curl localhost:7860/healthz
```

#### B. 本地开发 (单进程)
```bash
# 终端 1: 启动后端
uvicorn app_fastapi:app --host 0.0.0.0 --port 7860 --reload

# 终端 2: 启动前端
cd frontend && npm run dev
# 访问 http://localhost:5173 (代理到 :7860)
```

#### C. 生产多 Worker
```bash
# 读取 $WORKERS (默认 2)
WORKERS=4 python app_fastapi.py
```

### 4. 验证
```bash
# 健康检查
curl http://localhost:7860/healthz
curl http://localhost:7860/api/ready

# 发送测试消息
curl -X POST http://localhost:7860/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "音箱怎么连 WiFi？", "stream": true}'
```

---

## ⚙️ 配置指南 / Configuration

### 必填环境变量 (生产环境)

| 变量 | 说明 | 示例 |
|------|------|------|
| `OPENAI_BASE_URL` | OpenAI 兼容推理端点 | `http://llama.cpp:8080/v1` |
| `OPENAI_API_KEY` | 推理 API Key | `sk-local` |
| `JWT_SECRET` | HS256 签名密钥，**必填**，否则管理端点 403 | `super-secret-key` |
| `POSTGRES_DSN` | PostgreSQL 连接串 (含 pgvector) | `postgresql://user:pass@pg:5432/langgraph` |

### 运行时参数

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `7860` | 监听端口 |
| `WORKERS` | `2` | Uvicorn worker 数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `GRAPH_TIMEOUT_SECONDS` | `120` | Graph 执行超时，反向代理需设更大 |
| `SHUTDOWN_TIMEOUT_SECONDS` | `30` | 优雅关闭排空窗口 |
| `CONCURRENCY_WAIT_SECONDS` | `10` | 并发闸等待后 429 |

### RAG 后端选择

| `RAG_BACKEND` | 说明 | 适用场景 |
|---------------|------|----------|
| `tfidf` | 纯 TF-IDF + BM25，无外部依赖 | 开发/测试/无 GPU 环境 |
| `hybrid` | 进程内双路 + RRF + rerank | 单机生产，无 pgvector |
| `pgvector` | PostgreSQL + pgvector，单 SQL 双路检索 | **生产推荐**，支持大规模 |

> **注意**：`pgvector` 模式需 PostgreSQL 安装 `pgvector` 扩展，并跑 `scripts/ingest_knowledge.py` 建立索引。

### 模型配置 (可选)
```bash
# 模型注册表 (JSON，优先级高于环境变量)
MODEL_REGISTRY_PATH=config/model_registry.json

# 价格版本 (用于成本统计)
PRICE_VERSION=2026-07
```

---

## 🔌 API 接口 / API Endpoints

### 核心聊天接口
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/chat` | POST | `{"message":"...", "session_id":"...", "stream":false}` → JSON 或 SSE 流式 |
| `/api/session/<id>` | GET | 会话详情 (消息、意图、情绪、满意度) |
| `/api/sessions` | GET | 会话列表 `?search=keyword&limit=20` |
| `/api/export/<id>` | GET | 导出会话 JSON |

### 反馈与评分
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/rating` | POST | `{"session_id":"...", "stars":5}` 星级评分 |
| `/api/reaction` | POST | `{"session_id":"...", "emoji":"👍"}` emoji 反应 |
| `/api/feedback` | POST | `{"session_id":"...", "query":"...", "answer":"..."}` 文本反馈 |

### 记忆与分析
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/memory` | GET | 当前用户长期记忆列表 |
| `/api/analytics` | GET | 统计数据 (用于前端分析面板) |
| `/api/observability` | GET | 可观测性指标 (用于前端分析面板) |

### 认证与管理
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/auth/login` | POST | `{"username":"...", "password":"..."}` → JWT |
| `/api/auth/register` | POST | 用户注册 |
| `/api/auth/me` | GET | 当前用户信息 |
| `/api/admin/prompts` | GET/POST | Prompt Registry 管理 (需 admin scope) |

### 监控与健康
| 端点 | 方法 | 说明 |
|------|------|------|
| `/healthz` | GET | 存活探针 (k8s liveness) |
| `/api/ready` | GET | 就绪探针 (k8s readiness，不可用返回 503) |
| `/api/health` | GET | 完整健康 (LLM/DB/Redis/限流) |
| `/api/metrics` | GET | Prometheus 指标文本格式 |

---

## 📊 评测体系 / Evaluation System

> 完整文档见 [`eval/README.md`](eval/README.md) 与 [`eval/EVAL_METRICS.md`](eval/EVAL_METRICS.md)

### 四层指标体系

| 层 | 核心指标 | 目标阈值 | 评测方式 |
|------|----------|----------|----------|
| **检索 Retrieval** | Recall@5, HitRate@5, MRR, Context Precision, Context Recall | ≥0.85/0.90/0.80/0.80/0.85 | 规则/Embedding/LLM Judge |
| **生成 Generation** | Faithfulness, Answer Relevance, Completeness, Context Usage, Noise Sensitivity, Refusal Correctness | ≥0.90/0.80/0.80/0.50/≤0.15/≥0.90 | LLM-as-Judge (逐句/去偏) |
| **Agent** | Tool Selection Acc, Param Acc, Task Completion, Error Recovery, Stability(pass@N) | ≥0.90/0.85/0.85/0.70/≥0.95 | 轨迹对比 |
| **工程 Engineering** | JSON合法率, Schema通过率, TTFT/E2E p95, 重试率, 幻觉率 | ≥0.98/0.95/观测/≤0.20/≤0.10 | 运行记录 |

### 评测数据集

| 数据集 | 样本数 | 特点 |
|--------|--------|------|
| `golden_set_v2.jsonl` | **114** | 生成层核心集，chunk级 golden、多轮上下文、sg/multi_hop 标注、24 noise_probe |
| `rag_eval_hard.jsonl` | **92** | 检索硬核集，27语义鸿沟题、6多跳、4拒答、12KB全覆盖 |
| `golden_set.jsonl` | **63** | 早期四层集：retrieval(22)+generation(15)+agent(14)+engineering(12) |

### 运行评测

```bash
# 1. 离线全量跑 (零费用，Mock 模式)
python scripts/run_eval.py --layer all --mock

# 2. 真实小跑 (先看费用)
set RAG_BACKEND=hybrid
python scripts/eval_real.py --mode both --limit 8

# 3. 真实全量跑 (需配置 .env)
python scripts/eval_real.py --mode both --backend pgvector --csv real_result.csv

# 4. 仅检索层 (便宜)
python scripts/eval_real.py --mode retrieval --backend pgvector
```

### 关键评测结果 (最新真实跑分)

| 指标 | 真实 pgvector 报告 | 离线 Mock 基线 |
|------|-------------------|----------------|
| HitRate@5 | 1.000 | 1.000 |
| MRR | 1.000 | 1.000 |
| Context Recall | 1.000 | 1.000 |
| Context Precision | 0.783 | 1.000 |
| Faithfulness | 1.000 | 1.000 |
| Answer Relevancy | 0.867 | 1.000 |
| Answer Correctness | 1.000 | 1.000 |
| Citation Accuracy | 0.783 | 1.000 |
| Citation Integrity Rate | 0.333 | — |

> **改进重点**：Context Precision / Citation Accuracy 需优化 rerank 阈值与引用纪律 Prompt。

---

## 📁 项目结构 / Project Structure

```
langgraph-customer-service-agent/
├── app_fastapi.py              # FastAPI 生产入口 (P2+)
├── app_original_sync.py        # 归档：同步版本 (仅作参考)
├── agent/
│   ├── __init__.py
│   ├── graph.py                # LangGraph 工作流定义
│   ├── state.py                # CustomerServiceState (唯一状态定义)
│   ├── nodes.py                # 节点实现 (intent/reply/satisfaction/escalate/finalize)
│   ├── runner.py               # Graph 执行封装 (超时/流式/错误分类)
│   ├── llm_gateway.py          # 多模型网关 (路由/重试/预算/缓存/熔断)
│   ├── hybrid_rag.py           # Hybrid RAG: 双路召回+RRF+rerank+parent-child
│   ├── agentic_rag.py          # Agentic RAG: query rewrite → sufficiency check → retry
│   ├── pgvector_hybrid.py      # PostgreSQL + pgvector 检索后端
│   ├── embedding_client.py     # OpenAI 兼容 embedding 客户端
│   ├── rate_limiter.py         # 4层 Redis 令牌桶限流
│   ├── auth.py                 # JWT + API Key 认证
│   ├── circuit_breaker.py      # 熔断器 (per provider/model)
│   ├── context_compaction.py   # 上下文压缩 (LLM 摘要替代截断)
│   ├── context_assembler.py    # 上下文组装 (RAG + Memory + Tone + Compaction)
│   ├── metrics.py              # Prometheus 指标 (prometheus_client 或内置降级)
│   ├── observability.py        # TraceSession + TraceService + AlertService
│   ├── prompt_registry.py      # 版本化 Prompt + 审批流
│   ├── feedback_store.py       # 反馈记录 (评分/反应/升级/追问)
│   ├── user_memory.py          # 长期用户记忆
│   ├── rag_backend.py          # 运行时检索后端路由 (tfidf/hybrid/pgvector)
│   ├── security/
│   │   ├── prompt_guard.py     # Prompt 注入检测
│   │   └── pii_redactor.py     # PII 扫描与脱敏
│   └── ... (其余工具模块)
├── frontend/                    # Vue3 + Vite 前端
│   ├── src/
│   │   ├── App.vue             # 主布局
│   │   ├── components/         # ChatInput/MessageList/SessionSidebar/AnalyticsPanel...
│   │   ├── stores/chat.ts      # 聊天状态 + SSE 流式
│   │   ├── stores/ui.ts        # UI 状态 (侧栏/主题/语言)
│   │   └── api/client.ts       # API 封装 + SSE 解析
│   └── package.json
├── knowledge/                   # 13 个领域 Markdown 知识库 (自动加载)
├── monitoring/                  # Prometheus + Grafana 配置
├── eval/                        # 评测系统
│   ├── harness.py              # 评测运行器 (GoldenCase/Scorer)
│   ├── metrics.py              # 四层纯函数指标实现
│   ├── EVAL_METRICS.md         # 指标速查表 (面试可背版)
│   ├── EVAL_REAL_README.md     # 真实评测说明
│   └── *.jsonl                 # 评测数据集
├── scripts/                     # 工具脚本
│   ├── eval_real.py            # 端到端真实评测
│   ├── eval_retrieval.py       # 检索基准测试
│   ├── improvement_cycle.py    # P4 自我改进周期
│   ├── approve_prompt.py       # Prompt 审批 CLI
│   ├── trace_tool.py           # Trace 回放/对比
│   ├── ingest_knowledge.py     # 知识库入库 pgvector
│   ├── validate_golden_set.py  # 数据集校验器
│   ├── fix_golden_sets.py      # 自动修补工具
│   └── gen_noise_probes.py     # 噪声 Probe 生成器
├── templates/                   # 旧版 HTML 模板 (兼容)
├── docker-compose.prod.yml      # 生产 Docker Compose
├── DEPLOYMENT.md               # 部署指南
├── HEALTHCHECK.md              # 健康检查 + 告警参考
└── README.md                    # 本文件
```

---

## 🐳 部署指南 / Deployment

### Docker Compose 生产部署
```yaml
# docker-compose.prod.yml 包含：
# - app (FastAPI 多 worker)
# - postgres (pgvector enabled)
# - redis (限流/缓存)
# - prometheus + grafana (可选 profile)
```

```bash
# 1. 准备目录
mkdir -p data logs monitoring/grafana/dashboards

# 2. 配置环境
cp .env.example .env
# 必改：POSTGRES_DSN, OPENAI_BASE_URL, OPENAI_API_KEY, JWT_SECRET

# 3. 启动
docker compose -f docker-compose.prod.yml up -d --build

# 4. 启动监控 (可选)
docker compose -f docker-compose.prod.yml --profile monitoring up -d
# Grafana: http://localhost:3000 (admin/admin)
```

### Kubernetes 部署要点
- **Checkpoint**：必须用 PostgreSQL (`USE_POSTGRES=1`)，禁用 SQLite
- **优雅关闭**：`preStop` hook 等待 `SHUTDOWN_TIMEOUT_SECONDS`，配合 `terminationGracePeriodSeconds`
- **并发控制**：`MAX_CONCURRENT_REQUESTS` + `RATE_LIMIT_*` 限制单 Pod 吞吐
- **指标聚合**：多副本需开启 Prometheus multiprocess mode (`prometheus_multiproc_dir`)

### 已知限制 (详见 [DEPLOYMENT.md](DEPLOYMENT.md))
- SSE token 粒度依赖 LangGraph 版本，旧版降级为节点级切片
- ContextOverflowError 压缩重试仅 1 次，仍溢出返回 413
- `/api/sessions` 直读 SQLite，百万行需分页/物化视图
- 人工升级挂起 Graph，暂无 HTTP resume 端点
- Trace/Feedback SQLite 无自动清理，需定期归档
- 多 worker `/api/metrics` 返回单进程值，精确聚合需 multiprocess mode

---

## 🤝 贡献指南 / Contributing

### 开发规范
```bash
# 语法检查
python -m py_compile agent/*.py

# 单测
python -m pytest tests/ -v

# 代码风格
ruff check .
ruff format .
```

### 提交信息规范
```
feat: 新增 Agentic RAG query rewrite 功能
fix: 修复 ContextOverflowError 压缩重试逻辑
docs: 更新评测指标文档
refactor: 重构 LLM Gateway 重试策略
chore: 清理临时文件
```

### 评测驱动开发
1. 修改检索/生成逻辑前，先跑 `python scripts/run_eval.py --layer all --mock`
2. 关注 Δ 对比，确保核心指标不回退
3. 真实评测前务必 `--limit` 小跑估算费用

---

## 📚 文档导航 / Documentation

| 文档 | 说明 |
|------|------|
| [DEPLOYMENT.md](DEPLOYMENT.md) | 多 worker 部署、优雅关闭、已知限制 |
| [HEALTHCHECK.md](HEALTHCHECK.md) | 健康端点、Prometheus 指标、告警规则 |
| [docs/TRACE_REPLAY.md](docs/TRACE_REPLAY.md) | Trace 回放、对比、排查 RAG 迭代 |
| [docs/PROMPT_SYSTEM.md](docs/PROMPT_SYSTEM.md) | Prompt Registry 工作流、自我改进周期 |
| [docs/RAG_UPGRADE.md](docs/RAG_UPGRADE.md) | pgvector 迁移运维手册 |
| [eval/README.md](eval/README.md) | 评测系统使用指南 |
| [eval/EVAL_METRICS.md](eval/EVAL_METRICS.md) | 四层指标定义、手算例、面试背诵版 |

---

## 📄 License

MIT License — 详见 [LICENSE](LICENSE)

---

## 🙏 致谢

- [LangGraph](https://github.com/langchain-ai/langgraph) — 状态化 Agent 编排框架
- [pgvector](https://github.com/pgvector/pgvector) — PostgreSQL 向量检索
- [SiliconFlow](https://siliconflow.cn) — Embedding & Reranker API
- [Vue.js](https://vuejs.org) / [Vite](https://vitejs.dev) — 前端框架

---

> **面试准备提示**：本项目把 "模型直出" 升级为 "可控系统"，核心在于 **LangGraph 编排** + **Hybrid/Agentic RAG** + **LLM Gateway 治理** + **安全/可观测/评测闭环**。建议结合 `INTERVIEW_GUIDE.md` 与 `EVAL_METRICS.md` 准备。

---

# 🌐 English Version (README_EN.md)

> The English version is available at [README_EN.md](README_EN.md)

---

## 📝 中文版已完整包含在上方

本 README 已包含完整中文文档。如需单独的英文版文件，请参考 `README_EN.md`。

---

**最后更新**：2026-08-20 | **版本**：3.0.0 | **维护者**：LangGraph Customer Service Agent Team