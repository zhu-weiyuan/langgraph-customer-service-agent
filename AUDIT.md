# 智能客服 Agent 项目审计报告

**审计日期**：2026-07-21
**审计范围**：`agent/` 全量核心模块、`app_fastapi.py`、`app.py`（已废弃）、`config.py`、`tests/`
**审计目标**：找出逻辑错误、架构不一致、安全隐患、性能瓶颈，给出可落地的改进建议
**审计方法**：逐文件静态阅读 + 调用链追踪（HTTP → 限流 → runner → graph → nodes → gateway/RAG/memory）

---

## 一、执行摘要

| 维度 | 评分 | 说明 |
|------|------|------|
| 架构一致性 | ⚠️ 中 | 主链路已统一到 LLM Gateway，但仍有 3 处旧路径残留 |
| 安全性 | ⚠️ 中 | 多租户隔离在 JWT 路径正确；匿名路径存在会话归属盲区 |
| 性能 | ✅ 良好 | 限流、并发闸门、连接池、流式 coalesce 设计合理 |
| 可维护性 | ⚠️ 中 | 模块职责清晰，但存在重复实现和死代码 |
| 测试覆盖 | ⚠️ 中 | 核心路径有测试，但异步路径覆盖不足 |

**总体判断**：这是一个经过多轮"修复版"迭代的中期项目，主链路（`/api/chat` → 限流 → runner → LangGraph → LLM Gateway → pgvector RAG → PostgreSQL）设计成熟、防御性编程到位。主要问题集中在**新旧路径未完全收敛**、**匿名流量下的多租户隔离盲区**、**若干同步阻塞点**和**死代码/重复实现**。没有发现会导致数据损坏或严重安全漏洞的致命缺陷。

---

## 二、调用链概览

```
POST /api/chat
  ├─ _guard_chat (长度/速率预检)
  ├─ _session_id_for_request (JWT subject 哈希 / IP 哈希)
  ├─ limiter.acquire (Redis 四层令牌桶: global/ip/user/session)
  ├─ [stream?] _chat_sse  :  runner.run_stream
  └─ [non-stream] limiter.concurrency → _run_with_overflow_retry → runner.run
        └─ graph.invoke (sync PostgresSaver, run_in_executor)
             ├─ identify_intent   → LLM JSON (intent + ending) + sentiment
             ├─ generate_reply    → build_reply_context → agentic_rag (pgvector)
             │                        → ContextAssembler → LLM stream (tokens)
             │                        → save_conversation (PG)
             │                        → _schedule_long_term_memory_extraction
             ├─ route_after_reply (ending? / escalate? → check_satisfaction)
             ├─ check_satisfaction → LLM (满意度询问)
             ├─ process_satisfaction → LLM JSON (satisfied?)
             ├─ should_resolve (retry/escalate/finalize)
             ├─ escalate_to_human → interrupt() (人工介入)
             └─ finalize → LLM (结束语) + generate_summary (工单) + save_ticket
```

---

## 三、发现的问题

### 🔴 高优先级

#### H1. 匿名流量的会话归属与多租户隔离盲区

**位置**：`app_fastapi.py:853-873` `_owns_session`、`app_fastapi.py:845-850` `_session_id_for_request`

**问题**：`_owns_session` 对非 JWT 请求（API Key / 匿名）直接 `return True`，即**任何调用方都可以访问任意 session_id**。同时匿名用户的 session_id 由 IP 哈希生成（`ip-{sha1(ip)[:16]}`），同一 NAT 网关下的所有匿名用户共享同一个 session_id，导致：
1. 不同匿名用户的对话历史混在同一 thread 里（LangGraph checkpoint 按 `thread_id` 隔离）。
2. 任何持有 API Key 的调用方可以读取/操作任意 session 的数据（`/api/session/{id}`、`/api/export/{id}` 等）。

**影响**：NAT 环境下多用户串话；API Key 持有者越权访问。

**建议**：
- 为匿名用户引入 `X-Session-Id` 请求头（客户端生成 UUID），服务端校验其格式并持久化归属，而非依赖 IP 哈希。
- `_owns_session` 对 API Key 路径至少校验 session 的 `user_id` 字段与请求方一致（若 API Key 绑定了 subject）。

---

#### H2. 流式路径下 `tenant_id` 未传递到 runner

**位置**：`app_fastapi.py:1289-1295`（`_chat_sse` 调用）、`agent/runner.py:423-487`（`run_stream`）

**问题**：
- 非流式路径：`_run_with_overflow_retry(session_id, message, trace, idem_key, memory_user_id, tenant_id)` → `runner.run(..., tenant_id=tenant_id)` ✅
- 流式路径：`_chat_sse(request, session_id, message, trace, idem_key, memory_user_id, tenant_id)` 内部调用 `runner.run_stream(session_id, message, trace_session=trace, idempotency_key=idem_key, user_id=user_id, tenant_id=tenant_id)` ✅（已传）

但 `runner.run`（非流式）在 `build_initial_state` 调用中**漏传 `tenant_id`**：
```python
# runner.py:351-355
state = build_initial_state(session_id, user_message,
                            prev_values=snap["values"],
                            trace_session=trace_session,
                            idempotency_key=idempotency_key,
                            user_id=user_id)   # ← 缺少 tenant_id=tenant_id
```
而 `run_stream`（`runner.py:483-487`）同样漏传：
```python
state = build_initial_state(session_id, user_message,
                            prev_values=snap["values"],
                            trace_session=trace_session,
                            idempotency_key=idempotency_key,
                            user_id=user_id)   # ← 缺少 tenant_id=tenant_id
```
`tenant_id` 仅通过 `set_gateway_context` 传入 LLM Gateway 上下文，**不进入 graph state**。若未来有节点需要从 state 读取 tenant_id（如按租户切换 RAG 索引/价格表），将取不到。

**建议**：在 `runner.run` 和 `runner.run_stream` 的 `build_initial_state` 调用中补传 `tenant_id=tenant_id`。

---

#### H3. `identify_intent` 使用 `LLMClient.chat_json`（旧路径）而非 Gateway

**位置**：`agent/nodes.py:260-265` `_call_llm_json`、`agent/nodes.py:306-309` `identify_intent`

**问题**：`_call_llm_json` 直接调用 `get_llm_client().chat_json(...)`，绕过了 LLM Gateway 的：
- 多模型路由 / Fallback 链
- Token 预算（reserve/reconcile）
- 响应缓存
- 幂等
- 熔断

意图识别是**每个请求必经**的节点，且返回 JSON，是 Gateway 最擅长的场景（`_MOCK_JSON_SCENES` 已包含 `intent_classification`）。当前实现意味着意图分类不享受任何 Gateway 保护。

**建议**：将 `_call_llm_json` 改为通过 Gateway 的 `chat_json` 路径（若 Gateway 已有 JSON 场景支持），或至少走 `gateway.chat` + `parse_json`，使其纳入预算/熔断/缓存。

---

#### H4. `check_satisfaction` 和 `finalize` 节点使用非流式 `_call_llm`（阻塞）

**位置**：`agent/nodes.py:680-690` `check_satisfaction`、`agent/nodes.py:770-806` `finalize`

**问题**：这两个节点调用 `_call_llm(..., stream=False)`（默认），内部走 `gateway.chat_sync(request)`。由于 graph 以 `graph.invoke`（同步）运行在 `run_in_executor` 线程中，`chat_sync` 内部用 `asyncio.run` 或同步 httpx 调用，**在 worker 线程中是阻塞的**——这对单请求无碍，但：
1. `check_satisfaction` 在**每个 ending 意图**后都会触发一次额外的非流式 LLM 调用（满意度询问），增加 1 次 RTT。
2. `finalize` 的结束语生成也是非流式，用户要等完整响应。
3. 若 Gateway 的 `chat_sync` 内部使用了与主事件循环不同的 asyncio 策略，可能在某些部署下产生嵌套事件循环问题。

**建议**：将 `check_satisfaction` 和 `finalize` 改为 `stream=True`，通过 `_emit_stream_event` 向前端流式输出，减少用户感知延迟并统一走流式路径。

---

### 🟡 中优先级

#### M1. `agentic_rag` 默认 fast 模式下的实体证据检查过于保守

**位置**：`agent/agentic_rag.py:183-232` `_has_requested_entity_evidence`

**问题**：fast 模式下，若用户问题包含 `entity_aliases` 中的关键词（如"空调"、"音箱"、"wifi"），则要求检索结果中该实体**至少有一处非否定提及**，否则 `sufficient=False` 且 `context=""`。这导致：
1. 知识库中确实没有该实体信息时，RAG 返回空上下文 → 系统提示"未检索到足够证据" → LLM 回答"暂时无法确认"。这是**正确的 fail-closed** 行为。
2. 但 `entity_aliases` 是硬编码的 10 个产品词，**未随知识库扩展而更新**。新增产品（如"智能门锁"）不在别名表中，fast 模式不会对其做证据检查，可能返回弱相关上下文。
3. 否定短语列表（`negative_phrases`）也是硬编码，中文否定表达多样（"没"、"不"、"无"、"没有"），当前列表可能漏判或误判。

**建议**：
- 将 `entity_aliases` 和 `negative_phrases` 提取到配置文件（`config.py` 或 YAML），支持热更新。
- 考虑用 NER 或关键词提取替代硬编码别名表。

---

#### M2. `runner.run_stream` 同步路径的超时取消不完整

**位置**：`agent/runner.py:634-710`（同步 graph 的流式路径）

**问题**：
```python
result_future = loop.run_in_executor(None, _run_in_thread)
# ...
if remaining <= 0:
    result_future.cancel()   # ← asyncio Future.cancel() 对 run_in_executor 无效
    raise asyncio.TimeoutError()
```
`asyncio.Future.cancel()` 对 `run_in_executor` 返回的 future **不会终止底层线程**（Python 3.x 已知行为）。超时后：
1. worker 线程继续运行 `graph.invoke`，占用线程池槽位。
2. `sync_q` 中的残余数据无人消费。
3. 若线程池耗尽（默认 `min(32, cpu+4)`），后续请求排队。

**影响**：高并发 + 慢 LLM 响应时，线程池可能逐渐耗尽。

**建议**：
- 使用 `concurrent.futures.ThreadPoolExecutor` 的 `shutdown(wait=False, cancel_futures=True)`（Python 3.9+）在应用关闭时清理。
- 或在 `_run_in_thread` 中传入一个 `threading.Event` 作为取消信号，节点在关键检查点（如 RAG 检索前、LLM 调用前）检查该事件并提前返回。
- 短期缓解：增大 `GRAPH_TIMEOUT_SECONDS` 或限制并发（`MAX_CONCURRENT_REQUESTS`）。

---

#### M3. `memory.py` 的 `save_ticket` 重试逻辑有缺陷

**位置**：`agent/memory.py:359-381`

**问题**：
```python
def save_ticket(ticket: Dict[str, Any], max_attempts: int = 5) -> None:
    ticket_id = ticket["ticket_id"]
    for _ in range(max_attempts):
        try:
            with get_connection() as conn:
                conn.execute("INSERT INTO tickets ...", (...))
            ticket["ticket_id"] = ticket_id   # ← 赋值给局部变量，无意义
            return
        except IntegrityError:
            ticket_id = f"{ticket['ticket_id']}-{uuid.uuid4().hex[:8]}"
            # ↑ 但 ticket["ticket_id"] 仍是旧值，下一次循环 ticket_id 基于旧值拼接
```
1. 第 377 行 `ticket["ticket_id"] = ticket_id` 在 `try` 块内、`return` 前，实际无效果（局部变量赋值）。
2. 冲突重试时，`ticket_id` 基于**原始** `ticket["ticket_id"]` 拼接，但 `ticket["ticket_id"]` 本身未被更新，导致多次冲突后 ID 变为 `orig-xxxx-xxxx-xxxx`（链式拼接），而非 `orig-xxxx`。
3. 若 5 次都冲突（极小概率但可能），抛 `RuntimeError` 但**不记录日志**，调用方 `finalize` 节点会 catch 并 `print`，但工单丢失。

**建议**：
```python
def save_ticket(ticket, max_attempts=5):
    base_id = ticket["ticket_id"]
    for attempt in range(max_attempts):
        ticket_id = base_id if attempt == 0 else f"{base_id}-{uuid.uuid4().hex[:8]}"
        try:
            with get_connection() as conn:
                conn.execute("INSERT ...", (ticket_id, ...))
            ticket["ticket_id"] = ticket_id
            return
        except IntegrityError:
            logger.warning("ticket id conflict, retrying: %s", ticket_id)
    raise RuntimeError(f"failed to save ticket after {max_attempts} attempts")
```

---

#### M4. `app.py` 已废弃但未删除，存在误导风险

**位置**：`app.py`（整个文件，596 行）

**问题**：文件头已标注"已废弃"，但：
1. 仍包含完整的 `http.server` 实现、旧的 `Memory` 类（SQLite）、旧的 `LLMClient` 调用路径。
2. `run()` 函数仍可被 `python app.py` 直接执行，启动一个功能不完整的服务（无认证、无限流、无 Gateway）。
3. 新开发者可能误以为这是活跃入口。

**建议**：将 `app.py` 移入 `archive/` 目录（与 `archive/legacy-frontend-7860.html` 一致），或在文件头添加 `raise SystemExit("app.py is deprecated; use app_fastapi.py")`。

---

#### M5. `nodes.py` 中大量 `print()` 而非 `logger`

**位置**：`agent/nodes.py` 全文（约 20 处 `print`）

**问题**：生产环境使用 `print` 输出日志：
1. 无法被日志收集器（如 ELK、Datadog）统一采集。
2. 无法按级别过滤（调试信息混入错误信息）。
3. 在 Windows 上 `print` 中文可能因编码问题抛异常（`UnicodeEncodeError`）。

**建议**：将所有 `print` 替换为 `logger.info/debug/warning`（`nodes.py` 已 import `logging`）。

---

#### M6. `context_assembler.py` 的 `assemble` 方法签名与 `build_reply_context` 的调用不完全匹配

**位置**：`agent/context_assembler.py:85-106`、`agent/nodes.py:536-540`

**问题**：`ContextAssembler.assemble` 接受 `state` 字典，期望其中包含 `task_goal`、`constraints`、`memory_summary`、`rag_results`、`available_tools` 等键。`build_reply_context` 构造了这些键，但：
1. `state["messages"]` 传入的是 `trimmed`（LangChain 消息对象），而 assembler 内部可能期望 dict 格式。需确认 assembler 是否处理了 `HumanMessage`/`AIMessage` 对象。
2. `rag_results` 传入 `[rag_info]`（agentic_rag 的完整结果字典），但 assembler 的 `rag_results` 组件可能期望的是 `[{title, text, score}]` 格式的检索结果列表，而非包含 `context`/`rounds`/`queries_tried` 的包装字典。

**建议**：确认 `ContextAssembler` 的 `rag_results` 组件对输入格式的预期，确保 `build_reply_context` 传入的是检索结果列表而非 agentic_rag 包装字典。

---

### 🟢 低优先级 / 代码质量

#### L1. `token_estimator.py` 的 `estimate_tokens` 与 `nodes.py:335` 的 `estimate_tokens` 重复

**位置**：`agent/token_estimator.py:18-44`、`agent/nodes.py:335-343`

**问题**：两处实现了几乎相同的 token 估算逻辑（中文 1.5 tokens/字，英文 1.3 tokens/词）。`nodes.py` 的版本还额外处理了 `AIMessage`/`HumanMessage` 对象。

**建议**：统一使用 `token_estimator.estimate_messages_tokens`，`nodes.py` 删除本地 `estimate_tokens`，改为调用 `estimate_messages_tokens`。

---

#### L2. `llm_client.py` 的 `chat_sync` 路径与 Gateway 的 `chat_sync` 并存

**位置**：`agent/llm_client.py:100-150`（`chat` 方法）、`agent/llm_gateway.py`

**问题**：`LLMClient.chat` 在 `_use_gateway=True` 时委托给 Gateway，但 `LLMClient` 仍保留完整的直接 HTTP 调用路径（`_chat_direct`）。`_call_llm_json`（`nodes.py:260`）直接调用 `get_llm_client().chat_json`，若 `chat_json` 不走 Gateway，则绕过了所有 Gateway 保护（同 H3）。

**建议**：确认 `LLMClient.chat_json` 是否也委托给 Gateway；若不是，统一改为走 Gateway。

---

#### L3. `rate_limiter.py` 的 `LocalConservativeLimiter` 降级限额为 50%

**位置**：`agent/rate_limiter.py`（降级逻辑）

**问题**：Redis 不可用时降级到本地限流器，限额取正常值的 50%。这是 fail-closed 的正确设计，但：
1. 多 worker 部署时，每个 worker 独立限流，**总限额 = 50% × worker 数**，可能远超或远低于预期。
2. 降级状态通过 `degrade_callback` 上报，但未见告警集成（仅 log）。

**建议**：在降级时触发 `alert_service.record("rate_limiter_degraded", 1)`，接入现有告警链路。

---

#### L4. `auth.py` 的 `_jwt_secret` 在开发环境允许空 secret

**位置**：`agent/auth.py:32-42`

**问题**：`_jwt_secret` 仅在 `APP_ENV=prod/production` 时强制最小长度。开发环境（`APP_ENV` 未设置或 `dev`）允许空 `JWT_SECRET`，此时 `create_access_token` 会抛 `ValueError`（`PyJWT` 未安装或未配置 secret）。但 `check_api_key` 路径不依赖 JWT，所以开发环境用 API Key 仍可工作。

**影响**：低。开发环境用 API Key 即可；若误用 JWT 路径会得到明确的 `ValueError`。

**建议**：在 `APP_ENV=dev` 时，若 `JWT_SECRET` 为空，自动使用一个固定的开发 secret（如 `"dev-only-secret-not-for-production"`）并 log warning，避免开发时 JWT 路径直接报错。

---

#### L5. `graph.py` 的 `make_sync_postgres_checkpointer` 打开长连接但不管理生命周期

**位置**：`agent/graph.py:222-250`

**问题**：`make_sync_postgres_checkpointer` 创建 `psycopg.connect` 长连接，注释说"调用方需持有返回的 saver 引用；saver 被 GC 时连接自动关闭"。但：
1. `PostgresSaver` 的 `__del__` 是否真的关闭连接取决于 langgraph 实现，不可靠。
2. 应用关闭时未见显式 `saver.close()` 调用。

**建议**：在 FastAPI lifespan 的 shutdown 钩子中显式关闭 checkpointer 连接。

---

#### L6. `tests/` 目录覆盖不足

**位置**：`tests/`（34 个文件）

**问题**：
1. 异步路径（`run_stream` 的 async-only graph 分支、`_chat_sse`）缺乏集成测试。
2. `agentic_rag` 的 fast 模式实体证据检查（`_has_requested_entity_evidence`）无专项测试。
3. 多租户隔离（`_owns_session` 的 JWT vs 匿名路径）无测试。
4. `save_ticket` 的重试逻辑无测试。

**建议**：优先补充上述 4 类测试。

---

## 四、架构一致性评估

### 已统一的路径 ✅
| 组件 | 状态 |
|------|------|
| 主回复生成（`generate_reply`） | 走 LLM Gateway（`_call_llm` → `gateway.chat_sync/stream_sync`） |
| 限流 | 统一 `RedisTokenBucketLimiter`（四层令牌桶 + 降级） |
| 会话持久化 | 统一 PostgreSQL（`memory.py` + `runtime_db.py`） |
| Checkpointer | 统一 `PostgresSaver`（SQLite 已禁用） |
| RAG 后端 | 统一 `rag_backend.py`（tfidf/hybrid/pgvector 选择 + 降级） |
| 认证 | 统一 `AuthMiddleware`（JWT + API Key） |
| 流式输出 | 统一 `coalesce_stream_tokens` + SSE |

### 未完全收敛的路径 ⚠️
| 组件 | 问题 | 建议 |
|------|------|------|
| 意图识别（`identify_intent`） | 走 `LLMClient.chat_json`（旧路径） | 改走 Gateway（H3） |
| 满意度判断（`process_satisfaction`） | 走 `LLMClient.chat_json`（旧路径） | 改走 Gateway |
| 满意度询问（`check_satisfaction`） | 非流式 `_call_llm` | 改流式（H4） |
| 结束语（`finalize`） | 非流式 `_call_llm` + `generate_summary` | 改流式（H4） |
| `app.py` | 完整旧实现仍在 | 移入 `archive/`（M4） |
| `LLMClient` 直接 HTTP 路径 | 保留 `_chat_direct` 作为 fallback | 确认是否仍需要；若仅测试用，加注释 |

---

## 五、性能评估

### 设计良好的部分 ✅
1. **四层令牌桶限流**：global/ip/user/session 分层，Redis Lua 脚本原子操作，降级 fail-closed。
2. **并发闸门**：`asyncio.Semaphore` 限制 in-flight 模型调用，避免 LLM 过载。
3. **流式 token coalesce**：`coalesce_stream_tokens` 将 1-char 级 delta 合并为 24-char/120ms 块，减少前端 DOM 更新。
4. **pgvector 单 SQL 双路检索**：`PgHybridStore.hybrid_search` 用 CTE 一次查询完成向量+关键词检索 + RRF，避免多次 round-trip。
5. **RAG 搜索缓存**：`RAG_SEARCH_CACHE_TTL`（默认 60s）对相同查询复用结果。
6. **Context Compaction**：对话过长时用 LLM 压缩早期消息，避免上下文爆炸。
7. **Token 预算**：`TokenBudgetManager` 的 reserve/reconcile 四步，防止超预算调用。

### 潜在瓶颈 ⚠️
1. **`identify_intent` 的 LLM 调用**：每个请求必经，且是 JSON 调用（`max_tokens=256`），增加 1 次 RTT。若 LLM 延迟 500ms，则每个请求至少 500ms 的意图识别开销。
   - **建议**：对闲聊/结束意图用规则预判（`should_skip_retrieval` 已有类似逻辑），减少对 LLM 的依赖。
2. **`check_satisfaction` 的额外 LLM 调用**：每个 ending 意图后触发，增加 1 次 RTT。
   - **建议**：用规则判断（如用户说"谢谢"/"好的"/"再见" → 满意），仅在不确定时调用 LLM。
3. **同步 graph 运行在 `run_in_executor`**：worker 线程池默认 `min(32, cpu+4)`，高并发下可能成为瓶颈。
   - **建议**：考虑迁移到 `AsyncPostgresSaver` + `graph.ainvoke`，完全异步化。
4. **`save_conversation` 同步写入 PostgreSQL**：在 `generate_reply` 节点内同步执行，增加节点延迟。
   - **建议**：改为异步写入（`asyncio.to_thread`）或批量写入。

---

## 六、安全性评估

### 已到位的措施 ✅
1. **JWT + API Key 双认证**：`AuthMiddleware` 支持两种方案，JWT 使用 HS256 + 生产环境强制 secret 长度。
2. **Refresh Token 加盐哈希**：`hash_refresh_token` 使用 HMAC-SHA256 + pepper，DB 泄露不可重放。
3. **密码哈希**：`hash_password`/`verify_password`（推测为 bcrypt/argon2，需确认）。
4. **会话归属校验**：JWT 路径下 `_owns_session` 校验 session 归属，fail-closed。
5. **Prompt 注入防护**：`reinforce_system_prompt` 加固系统提示，引用纪律约束幻觉。
6. **PII 扫描**：`_pii_scan` 非阻塞审计。
7. **SQL 参数化**：所有 SQL 使用 `%s` 参数化（psycopg），无字符串拼接。
8. **限流**：四层令牌桶 + 并发闸门，防 DDoS。
9. **CORS**：FastAPI 默认不设置 CORS（需确认是否配置了 `CORSMiddleware`）。

### 需要关注的问题 ⚠️
1. **匿名会话隔离**（H1）：NAT 下多用户串话，API Key 越权。
2. **`_client_ip` 信任 `X-Forwarded-For`**：`app_fastapi.py:838-842` 直接取 `X-Forwarded-For` 第一个值，若部署在不可信代理后，客户端可伪造 IP 绕过 IP 限流。
   - **建议**：仅在 `TRUST_PROXY=1` 时信任 `X-Forwarded-For`，否则使用 `request.client.host`。
3. **`/api/auth/register` 允许无密码注册**：`RegisterRequest.password` 为 `Optional`，`create_user` 接受 `password=None`，创建无密码用户。若 JWT 路径允许无密码登录，则存在账户接管风险。
   - **建议**：生产环境强制注册时必须提供密码，或禁用公开注册。
4. **CORS 配置**：需确认 `app_fastapi.py` 是否配置了 `CORSMiddleware` 及其 `allow_origins`。若为 `*`，则任意站点可发起跨域请求（配合 CSRF 风险）。

---

## 七、改进路线图

### 第一阶段（1-2 周）— 安全与正确性
| # | 任务 | 优先级 | 工作量 |
|---|------|--------|--------|
| 1 | 修复 H1：匿名会话隔离（引入 `X-Session-Id`） | 🔴 | 3d |
| 2 | 修复 H2：`runner.run/run_stream` 补传 `tenant_id` | 🔴 | 0.5d |
| 3 | 修复 H3：`_call_llm_json` 改走 Gateway | 🔴 | 1d |
| 4 | 修复 M3：`save_ticket` 重试逻辑 | 🟡 | 0.5d |
| 5 | 修复 `_client_ip` 的 `X-Forwarded-For` 信任问题 | 🟡 | 0.5d |
| 6 | 确认并加固 CORS 配置 | 🟡 | 0.5d |
| 7 | 确认注册是否强制密码 | 🟡 | 0.5d |

### 第二阶段（2-4 周）— 性能与一致性
| # | 任务 | 优先级 | 工作量 |
|---|------|--------|--------|
| 8 | 修复 H4：`check_satisfaction`/`finalize` 改流式 | 🟡 | 2d |
| 9 | 修复 M2：同步 graph 超时取消（传入取消信号） | 🟡 | 2d |
| 10 | 统一 `estimate_tokens`（L1） | 🟢 | 0.5d |
| 11 | `nodes.py` 的 `print` 改 `logger`（M5） | 🟢 | 0.5d |
| 12 | `app.py` 移入 `archive/`（M4） | 🟢 | 0.5d |
| 13 | 确认 `ContextAssembler.rag_results` 输入格式（M6） | 🟡 | 1d |
| 14 | 降级时触发告警（L3） | 🟢 | 0.5d |

### 第三阶段（4-8 周）— 架构演进
| # | 任务 | 优先级 | 工作量 |
|---|------|--------|--------|
| 15 | 意图识别规则预判（减少 LLM 调用） | 🟡 | 3d |
| 16 | 满意度判断规则预判 | 🟡 | 2d |
| 17 | 迁移到 `AsyncPostgresSaver` + `graph.ainvoke` | 🟡 | 5d |
| 18 | `save_conversation` 异步化 | 🟢 | 1d |
| 19 | `entity_aliases`/`negative_phrases` 配置化（M1） | 🟢 | 2d |
| 20 | 补充测试（L6） | 🟡 | 5d |

---

## 八、附录：关键文件清单

| 文件 | 行数 | 职责 | 主要问题 |
|------|------|------|----------|
| `app_fastapi.py` | 1854 | FastAPI 入口、路由、认证、限流 | H1 匿名隔离、`_client_ip` 信任 |
| `agent/runner.py` | 802 | 图执行、流式、超时 | H2 tenant_id、M2 超时取消 |
| `agent/graph.py` | 284 | 图拓扑、checkpointer | L5 连接生命周期 |
| `agent/nodes.py` | 806 | 6 个图节点 | H3/H4 旧路径、M5 print |
| `agent/llm_gateway.py` | 1816 | 统一模型接入 | 设计良好，无重大问题 |
| `agent/llm_client.py` | 580 | 旧 LLM 客户端 | L2 旧路径残留 |
| `agent/agentic_rag.py` | 390 | Agentic RAG 循环 | M1 硬编码别名 |
| `agent/rag_backend.py` | 271 | 检索后端选择 | 设计良好 |
| `agent/memory.py` | 427 | PG 持久化 | M3 save_ticket |
| `agent/auth.py` | 420 | JWT + API Key | L4 开发环境 secret |
| `agent/rate_limiter.py` | 455 | 四层限流 | L3 降级告警 |
| `agent/context_assembler.py` | ~200 | 上下文组装 | M6 输入格式 |
| `agent/token_estimator.py` | ~50 | Token 估算 | L1 重复 |
| `app.py` | 596 | 旧入口（废弃） | M4 未删除 |
