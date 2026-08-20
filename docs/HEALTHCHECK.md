# 健康检查与监控 — LangGraph Customer Service Agent

本文档列出的端点与指标名与代码一一对应（端点：`app_fastapi.py`；
指标：`agent/metrics.py`；告警：`agent/observability.py` + `monitoring/alerts.yml`）。

## 1. 健康端点

### `GET /healthz` — 存活探针

纯存活，不触碰 LLM/DB/Redis。docker healthcheck 与 k8s livenessProbe 用它。

```json
{"status": "ok", "version": "3.0.0", "uptime_seconds": 123}
```

### `GET /api/ready` — 就绪探针

检查项：SQLite 可写（真实写入 `_readiness_probe` 表）、redis（**可降级**，
不可用只置 `degraded_mode:true` 不拉低就绪）、graph 已构建、关键配置存在。
不就绪返回 **503**。

```json
{"ready": true, "checks": {
  "sqlite": {"ok": true, "path": ".../user_memory.db"},
  "redis": {"ok": false, "degraded_mode": true},
  "graph": {"ok": true},
  "config": {"ok": true, "openai_base_url": true,
             "jwt_configured": true, "api_keys_configured": true}}}
```

### `GET /api/health` — 详情健康

含 LLM 连通性探测（httpx GET `$OPENAI_BASE_URL/models`，3s 超时，
失败不致命）、DB 汇总、限流器状态（degraded/active_concurrency 等）。

## 2. `GET /api/metrics` — Prometheus 指标

由 `agent/metrics.py` 统一输出（prometheus_client 可用时用它，否则内置
降级实现输出合法文本格式）。**真实指标集**：

| 指标 | 类型 | labels | 来源 |
|---|---|---|---|
| `http_requests_total` | counter | method, endpoint, status | 观测中间件（每请求） |
| `http_request_duration_seconds` | histogram | method, endpoint | 观测中间件 |
| `llm_tokens_total` | counter | model, scene, direction | llm_gateway |
| `llm_cost_yuan_total` | counter | model | llm_gateway |
| `llm_requests_total` | counter | model, outcome | llm_gateway |
| `node_duration_seconds` | histogram | node | graph 节点计时 |
| `rag_hit_ratio` | gauge | — | RAG 检索 |
| `rate_limit_events_total` | counter | tier | 429 handler（tier=触发层：global/ip/user/session/concurrency） |
| `circuit_breaker_state` | gauge | name | 熔断器（0=closed,1=half_open,2=open） |
| `cache_events_total` | counter | cache, result | 缓存 hit/miss |
| `feedback_events_total` | counter | kind | /api/rating、/api/reaction、/api/feedback |

> 旧文档中的 `langgraph_graph_executions_total`、`langgraph_state_size_bytes`
> 等指标**不存在**，勿在 dashboard/告警中引用。
> 多 worker 部署时本端点返回单 worker 数值（见 DEPLOYMENT.md §4）。

## 3. 应用内告警（AlertService）

lifespan 注册规则，后台任务每 30s 评估滑动窗口，命中打 `ALERT fired` 日志：

| 规则 | 指标(内部窗口名) | 条件 |
|---|---|---|
| `high_http_error_rate` | `http_error` | 5 分钟内 5xx > 10 |
| `high_latency_p_avg_ms` | `http_latency_ms` | 5 分钟均值 > 5000ms（≥5 样本） |
| `rate_limit_burst` | `rate_limited` | 5 分钟 429 > 30 |
| `limiter_degraded` | `limiter_degraded` | Redis 限流降级发生 |
| `chat_errors` | `chat_error` | 5 分钟 chat 5xx/超时 > 5 |

## 4. Prometheus / Grafana

- 抓取配置：`monitoring/prometheus.yml`（job `app` → `customer-service:7860`
  `/api/metrics`）。
- 告警规则：`monitoring/alerts.yml`（compose 已挂载到
  `/etc/prometheus/alerts.yml`）。
- Dashboard：`monitoring/grafana/dashboards/agent-overview.json`
  （provisioning 自动加载）。

## 5. Trace 与日志

- 每请求 `TraceSession`（request_id 取 `X-Request-ID` 头或生成，并回写响应
  头），请求结束（含 SSE 断连）落盘到 `$TRACE_DB`（默认 `data/trace.db`）
  的 `traces` 表；input_text 落盘前过 PII 脱敏。
- 日志为 JSON 结构化（`agent/logging_setup.py`），自动携带
  `request_id`/`session_id` 上下文字段，可按 request_id 与 trace 关联。
- OTel：设置 `OTEL_EXPORTER_OTLP_ENDPOINT` 后启用（`agent/otel_setup.py`），
  未设置时静默关闭。

## 6. 探针配置建议

```yaml
livenessProbe:  { httpGet: {path: /healthz,   port: 7860}, periodSeconds: 30 }
readinessProbe: { httpGet: {path: /api/ready, port: 7860}, periodSeconds: 10 }
```

docker compose healthcheck 已指向 `/healthz`（interval 30s / retries 3）。
