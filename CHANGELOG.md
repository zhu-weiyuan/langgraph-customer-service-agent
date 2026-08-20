# CHANGELOG

生产化改造历程，按模块分阶段记录。

---

## P0 — Compose 基线

- 建立 `docker-compose.prod.yml`：app + redis + pgvector（注释）+ prometheus + grafana
- `.env` 模板与全量环境变量文档化
- Dockerfile 多阶段构建（frontend build → python run）

---

## P1-A — LangGraph 图与状态

**文件**: `agent/graph.py`, `agent/state.py`, `agent/nodes.py`

- `CustomerServiceState` 唯一定义收敛到 `agent/state.py`，删除 graph.py 内嵌套 reducer 重复定义
- 修复条件边缺少 `finalize` 目标导致重试轮必崩的 bug
- `escalate_to_human` 经 `should_resolve`（连续 2 次不满意或强负面情绪）真实可达，内部走 `langgraph.types.interrupt`
- Checkpointer 工厂化：`make_checkpointer()`（async，AsyncSqliteSaver@`$CHECKPOINT_DB`），`USE_POSTGRES=1` 时切 AsyncPostgresSaver
- Legacy sync SqliteSaver 修复 `check_same_thread` 跨线程崩溃（`check_same_thread=False` + module-level 创建锁）
- 所有三方 import 守卫；路由函数纯化（stdlib 可独立单测）

---

## P1-B — LLM Gateway 与限流

**文件**: `agent/llm_gateway.py`, `agent/rate_limiter.py`, `agent/auth.py`

- **LLMGateway**：多模型路由 + tier whitelist + fallback 链；重试策略（指数退避 + full jitter，总 deadline 约束）；熔断器 per provider/model；精确 SHA256 响应缓存（内存 LRU / Redis 后端）；Token 预算管理器（自然日重置，reserve→reconcile）；400 上下文超限识别抛 `ContextOverflowError`（不重试，上层触发 compaction）；幂等键 replay
- **RedisTokenBucketLimiter**：Lua 原子四层令牌桶（global/ip/user/session）；Redis 故障 fail-closed 降级本地保守限流（50%）；真实 `asyncio.Semaphore` 并发闸；统一 `RateLimitExceeded(layer, retry_after)`
- **Auth**：JWT + API key，query 参数解析改用 `parse_qs`；新增 `create_access_token`；校验后写 `request.state.auth_subject/auth_tenant_id/auth_scheme`

---

## P2 — FastAPI 生产入口

**文件**: `app_fastapi.py`, `agent/runner.py`

- 废弃旧 `app_original_sync.py`（ThreadingHTTPServer），新建 `app_fastapi:app`
- lifespan：logging + otel → graph prewarm → vector index prebuild（后台）→ aiosqlite pool → redis probe → trace service + prompt registry seed → alert rules + background loop
- 优雅关闭：cancel alert task → drain inflight（≤30s）→ shutdown runner → close pool
- SSE 流式：每帧检查 `request.is_disconnected()`，断连取消 runner；finally aclose generator
- ContextOverflowError 压缩重试一次，仍溢出返回 413
- SQLitePool：aiosqlite LifoQueue 连接池（WAL + busy_timeout）；缺席降级 sqlite3 + to_thread
- `/api/auth/register` / `/api/auth/login` / `/api/auth/me` 完整用户体系
- Session 归属校验（JWT user 只能访问自己名下会话 + 未登记新会话）

---

## P3 — 可观测性

**文件**: `agent/metrics.py`, `agent/observability.py`, `agent/logging_setup.py`, `agent/otel_setup.py`, `monitoring/`

- 统一指标层 `metrics.py`：prometheus_client 优先，内置合法文本格式降级；11 个指标（见 [HEALTHCHECK.md](HEALTHCHECK.md)）
- `TraceSession`：8 个结构化分区（prompt/retrieval/memory/tool/model/cost/result/latency），每请求创建，finally `finalize_and_save` 落盘到 `$TRACE_DB`，PII 脱敏后写入
- `AlertService`：真滑动窗口，后台 30s 周期评估；5 条规则（http_error_rate, latency_p_avg, rate_limit_burst, limiter_degraded, chat_errors）
- JSON 结构化日志 + contextvars（request_id / session_id）；可选 OTel（OTLP exporter，全守卫）
- 监控物料：`monitoring/prometheus.yml`, `monitoring/alerts.yml`, Grafana provisioning + dashboard

---

## P4 — 半自动自我改进闭环

**文件**: `agent/feedback_store.py`, `agent/prompt_registry.py`, `scripts/improvement_cycle.py`

- `FeedbackStore`：记录低评分(≤3)、负向 emoji、escalation、连续追问（difflib >0.6）
- `PromptRegistry`：版本化 prompt，状态机 `candidate → pending_approval → approved → released`；`rollback` 标记 retired 并恢复上一全量版本
- 影子评测：候选 prompt 自动走 shadow eval，达标才进 pending_approval
- CLI：`improvement_cycle.py`（收集→分析→候选→影子评测→摘要），`approve_prompt.py list/approve/promote/rollback`
- cron：`0 3 * * * improvement_cycle.py >> logs/improvement_cycle.log`

---

## P5 — Trace 回放与深度可观测

**文件**: `agent/observability.py`, `agent/trace_replay.py`, `scripts/trace_tool.py`

- `TraceSession.to_dict(redact=True)`：落盘前深度 PII 脱敏
- 业务列（total_ms/cost/failed/low_score/tenant/scene/created_at）独立成列并建索引
- 大字段（完整 prompt/答案）只进 trace_json，不污染查询性能
- `trace_replay.py`：`load_trace / replay(inspect|rerun|diff) / list_traces`
- CLI：`trace_tool.py list --low-score --failed`、`show <id>`、`replay <id> --rerun`、`diff <id1> <id2>`

---

## RAG 升级 — PgVector 混合检索

**文件**: `agent/hybrid_rag.py`, `agent/rag_backend.py`, `agent/embedding_client.py`, `agent/pgvector_hybrid.py`, `scripts/ingest_knowledge.py`, `scripts/eval_retrieval.py`

- `HybridRetriever`：双路召回（dense + sparse）+ RRF 融合（k=60）+ rule rerank + parent-child（300→1200字）
- `PgHybridStore`：DB 内单 SQL CTE 双路检索，RRF 后 RuleReranker → parent 映射
- `embedding_client.py`：OpenAI 兼容 `POST /embeddings`，≤32条/批，超时 +2 次重试
- `rag_backend.py`：`RAG_BACKEND=pgvector/hybrid/tfidf` 路由，运行期失败自动回落 TF-IDF（warning 日志）
- 中文分词降级：容器无 zhparser → `simple` 配置 + jieba 预分词到 `content_tokens` 列

---

## 其他变更

### Prompt 分析
- 发现 RAG 上下文注入方式不明确、安全加固太简单、意图识别缺少 Few-Shot
- 提出改进版本：增强 System Prompt 结构、强化 anti-injection、意图分类加边界规则 + Few-Shot、Agentic RAG rewrite 加错误码/型号/多跳处理

### Eval 体系完善（eval/）
- 四层指标速查表（EVAL_METRICS.md）：检索(Recall@k/MRR) / 生成(Faithfulness/Answer Relevance) / Agent(Tool Accuracy/Task Completion) / 工程(JSON合法率/TTFT/p95)
- 真实评测系统（eval_real.py）：检索层调真实 embedding + hybrid/pgvector；生成层用真实 LLM，LLM-as-Judge 打分
- Golden set：50条（normal 25/edge 13/adversarial 7/high_weight 5）

### Frontend
- Vue3 + Vite 前后端分离重建
- SSE streaming、会话搜索/导出、⭐星级评分、emoji反应、语音I/O、暗色模式、中英双语
- Analytics dashboard：KPI cards、意图/情绪分布图表、评分可视化、自动刷新

---

## 已知限制

- 流式 token 粒度依赖 langgraph `astream(stream_mode=["messages","updates"])`；旧版守卫降级为节点级切片（分段吐字）
- `ContextOverflowError` 压缩重试只做一次，仍溢出返回 413
- `/api/sessions` / `/api/analytics` 直读 `user_memory.db`，超大历史库（>百万行）需分页/物化
- Escalation 挂起 graph，尚无 resume HTTP 端点（人工介入需直接操作 checkpointer）
- Trace/feedback SQLite 不自动清理，需 cron 归档
- 多 worker 部署 `/api/metrics` 返回单 worker 数值（已知限制，精确聚合需 multiprocess mode）
