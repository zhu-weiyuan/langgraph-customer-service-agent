# 部署指南 — LangGraph Customer Service Agent（生产版）

本文档只描述**当前代码真实存在**的启动方式、环境变量与行为。
健康端点与指标详情见 `HEALTHCHECK.md`；压测与混沌演练见 `scripts/loadtest/README.md`。

## 1. 架构与入口

- 生产入口：`app_fastapi.py`（FastAPI + uvicorn 多 worker）。
- 业务执行：`agent/runner.py` → `graph.ainvoke/astream`（langgraph，
  AsyncSqliteSaver 或 AsyncPostgresSaver checkpointer）。
- `app_original_sync.py` 为旧 sync 服务器**归档**，不再是启动路径。

```
client → uvicorn(workers) → app_fastapi
           ├─ auth (JWT / API key)             agent/auth.py
           ├─ 限流(4层令牌桶, Redis Lua)        agent/rate_limiter.py
           ├─ runner (ainvoke/astream + 120s 超时，$GRAPH_TIMEOUT_SECONDS)  agent/runner.py
           │    └─ graph: intent → reply → satisfaction → escalate/finalize
           ├─ 观测 (metrics / trace / alerts)   agent/metrics|observability
           └─ 反馈闭环 (feedback_store / prompt_registry)  P4
```

## 2. 启动

### 本地开发（单 worker）

```bash
pip install -r requirements.txt
export OPENAI_BASE_URL=http://localhost:8080/v1 OPENAI_API_KEY=sk-local
uvicorn app_fastapi:app --host 0.0.0.0 --port 7860
# 或: python app_fastapi.py   （读 $WORKERS，默认 2）
```

### 生产（docker compose）

```bash
cp .env.example .env   # 填 OPENAI_*, API_KEYS, JWT_SECRET
mkdir -p data logs     # 目录挂载（trace.db 等落在 ./data）
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml --profile monitoring up -d  # +Prometheus/Grafana
curl -s localhost:7860/healthz && curl -s localhost:7860/api/ready
```

compose 中 app 的 command 显式为
`uvicorn app_fastapi:app --host 0.0.0.0 --port 7860 --workers ${WORKERS:-2}
--timeout-graceful-shutdown 35`。

## 3. 环境变量清单

### 必填（生产）

| 变量 | 说明 |
|---|---|
| `OPENAI_BASE_URL` | OpenAI 兼容推理端点（如 `http://host:8080/v1`） |
| `OPENAI_API_KEY` | 推理端点密钥 |
| `API_KEYS` | 逗号分隔的合法 API key；**为空 = 所有端点公开（仅限开发）** |
| `JWT_SECRET` | HS256 签名密钥；未配置时 `/api/auth/token` 返回 503、`/api/admin/prompts*` 恒 403 |

### 服务与运行时

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `7860` | 监听端口 |
| `WORKERS` | `2` | uvicorn worker 数 |
| `LOG_LEVEL` | `INFO` | JSON 结构化日志级别 |
| `GRAPH_TIMEOUT_SECONDS` | `120` | 单请求 graph 执行总超时；反向代理/LB 超时应设置得更大 |
| `SHUTDOWN_TIMEOUT_SECONDS` | `30` | 关闭时在途请求 drain 上限 |
| `CONCURRENCY_WAIT_SECONDS` | `10` | 并发闸等待上限，超时返回 429 |
| `CORS_ALLOW_ORIGINS` | 空（=`*`） | 逗号分隔白名单 |

### 持久化

| 变量 | 默认 | 说明 |
|---|---|---|
| `CHECKPOINT_DB` | `checkpoints/checkpoints.db` | AsyncSqliteSaver 路径（compose 卷 `/app/checkpoints`） |
| `USE_POSTGRES` / `POSTGRES_DSN` | `0` / — | `1` 时改用 AsyncPostgresSaver（compose 有注释掉的 pgvector 服务） |
| `TRACE_DB` | `data/trace.db` | 请求 trace SQLite（compose 目录挂载 `./data`） |
| `P4_DB_PATH` | `data/p4_self_improve.db` | 反馈/prompt registry 存储 |
| `REDIS_URL` | `redis://localhost:6379/0` | 限流/缓存；不可用时限流 fail-closed 本地降级（50% 限额） |

### 认证 / 观测（可选）

`JWT_TTL_SECONDS`(3600)、`OTEL_EXPORTER_OTLP_ENDPOINT`（不设 = OTel 关闭）、
`OTEL_SERVICE_NAME`。

## 4. 多 worker 注意事项

worker 之间**不共享进程内存**，以下状态各 worker 独立：

1. **限流**：Redis 可用时四层令牌桶全局一致；Redis 挂掉后每个 worker
   独立进入本地保守限流（各自 50% 限额 → 集群总放行 ≈ workers × 50%，
   仍收紧于 Redis 正常值）。
2. **并发闸**（`asyncio.Semaphore`，默认 10）是每 worker 的：全局并发上限
   ≈ `workers × max_concurrency`。
3. **/api/metrics** 返回**当前应答 worker** 的指标，Prometheus 抓取数值会在
   worker 间"跳动"——已知限制；需要精确聚合时用 `WORKERS=1` 或引入
   prometheus_client multiprocess 模式（未内置）。
4. **SQLite**：所有写路径 WAL + busy_timeout，多 worker 并发写安全但吞吐
   有限；高写入量切 `USE_POSTGRES=1`。
5. **AlertService** 滑动窗口 per-worker，同一规则可能被多个 worker 各触发一次。
6. gateway 的 in-process 幂等缓存 per-worker：同一 `X-Idempotency-Key`
   落到不同 worker 不去重；跨实例幂等以 Redis 缓存层为准。

## 5. 优雅关闭

SIGTERM → lifespan shutdown：取消 alert 后台任务 → 等在途请求归零
（≤ `SHUTDOWN_TIMEOUT_SECONDS`）→ 关闭 checkpointer / aiosqlite 池。
compose 的 `stop_grace_period: 30s` 与 uvicorn
`--timeout-graceful-shutdown 35` 已对齐。

## 6. 已知限制

- **流式 token 粒度**依赖 langgraph `astream(stream_mode=["messages","updates"])`；
  旧版 langgraph 守卫降级为节点级切片（观感为"分段吐字"而非逐 token）。
- `ContextOverflowError` 的压缩重试只做一次；仍溢出返回 413，需开新会话。
- `/api/sessions`、`/api/analytics`、`/api/session/{id}` 直读
  `user_memory.db`（graph 节点写入）；读路径 `sqlite3 + to_thread`，
  写路径 aiosqlite 池优先——超大历史库（>百万行）需加分页/物化。
- escalation（转人工）挂起 graph（langgraph interrupt）并记入
  feedback_store；**尚无** resume 的 HTTP 端点（人工介入需直接操作
  checkpointer，或后续补 `/api/admin/resume`）。
- trace/feedback SQLite 不自动清理，需 cron 归档。
- `/api/chat` 非流式路径不再有旧版的 Redis 响应缓存（缓存下沉到
  llm_gateway 层，按 tenant+prompt_version+model 维度）。
