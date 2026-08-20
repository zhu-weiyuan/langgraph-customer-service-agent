# TRACE_WIRING — 可回放 Trace 接线说明

> 目标:**没有回放就没有优化**。一次请求的 Trace 要能完整记录并回放。
> 本文说明 `runner`/`nodes`/`gateway` 各阶段在哪调 `record_*`,以及请求结束怎么落盘。
> **不改 `app_fastapi.py` 主体**——现有的 `TraceSession` 创建、`add_event`、
> `finally: await get_trace_service(TRACE_DB).finalize_and_save(trace)` 全部保持不变,
> 新增的 `record_*` 调用是**纯增量**接线点。

相关文件:
- `agent/observability.py` — `TraceSession` + 八分区 + `record_*` + `TraceService` 落盘
- `agent/trace_replay.py` — `load_trace` / `replay(inspect|rerun|diff)` / `list_traces`
- `scripts/trace_tool.py` — CLI (`list` / `show` / `replay` / `diff`)

---

## 0. 数据模型速览

`TraceSession` 在保留旧的 `events` 事件流之外,新增八个结构化分区:

| 分区 | 便捷方法 | 结构化字段 |
|------|----------|-----------|
| Prompt | `record_prompt` | 模板名、版本、变量摘要、渲染后消息结构、渲染 hash |
| 检索 | `record_retrieval` | query、召回片段(摘要)、分数、来源、ACL 过滤结果、rerank 排名 |
| Memory | `record_memory` | 命中记忆、来源、更新时间、置信度 |
| Tool | `record_tool` | 工具名、参数、权限结果、耗时、返回摘要、错误 |
| 模型 | `record_model` | 供应商、模型、采样参数、输入/输出 token、finish、TTFT、stage |
| 延迟 | `record_latency` | 入口、检索、模型 TTFT、工具、总耗时 |
| 成本 | `record_cost` | 输入成本、输出成本、缓存命中、租户+场景归因 |
| 结果 | `record_result` | 最终答案、结构化解析、用户反馈、评测分数 |

所有可能含 PII 的字段在**落盘前**统一过 `redact_pii` 深度脱敏(`to_dict(redact=True)`,
`TraceService._prepare_row` 自动调用)。业务列(`total_ms`/`cost`/`failed`/`low_score`/
`tenant`/`scene`/`created_at`)独立成列并建索引,大字段(完整 prompt/答案)只进 `trace_json`。

`TraceSession` 从 `state` 传递:已在 `runner.build_initial_state(...,
trace_session=trace_session, ...)` 中注入到 State,节点内通过
`state.get("trace_session")` 取回(它是请求级对象,**不要**放进 checkpoint 序列化)。

---

## 1. 各阶段接线点

下面按请求流经的阶段列出「在哪调哪个 `record_*`」。取 trace 对象:

```python
trace = state.get("trace_session")          # nodes 内
# 或 runner 形参里的 trace_session
if trace is not None:
    trace.record_xxx(...)                    # 永远守空,trace 缺失不阻断主流程
```

### 1.1 入口 / 网关(app_fastapi 请求处理)
- 已有:创建 `TraceSession(request_id=..., user_id=, session_id=, input_text=)`。
  **建议补** `tenant=`、`scene=`(用于成本归因和 `list` 过滤)。
- 入口耗时:请求进入到进入 graph 之间 `trace.record_latency(entry_ms=...)`。

### 1.2 意图识别(`nodes.identify_intent` → `_call_llm_json`)
- LLM 调用返回后:
  ```python
  trace.record_model(provider="dashscope", model=<model>,
                     params={"temperature":..., "max_tokens":...},
                     in_tok=<prompt_tokens>, out_tok=<completion_tokens>,
                     finish=<finish_reason>, ttft_ms=<ttft>, stage="intent")
  ```
  → 对应清单「模型」。多次模型调用会**追加**到 `model` 列表。

### 1.3 RAG 检索(`nodes.build_reply_context` / `rag_backend` / `hybrid_rag`)
- 拿到召回结果(rerank 之后)时:
  ```python
  trace.record_retrieval(
      query=<检索 query>,
      chunks=[c.text for c in hits], scores=[c.score for c in hits],
      sources=[c.source for c in hits],
      acl=[c.allowed for c in hits],        # 权限过滤结果(True/False)
      rerank=[c.rerank_rank for c in hits]) # rerank 排名
  ```
  → 对应清单「检索」(Query/片段/分数/来源/权限/Rerank)。
- 检索耗时:`trace.record_latency(retrieval_ms=<ms>)`。

### 1.4 记忆(`nodes.build_reply_context` → `user_memory` / `memory`)
- 命中用户记忆后:
  ```python
  trace.record_memory([
      {"content": m.content, "source": m.source,
       "updated_at": m.updated_at, "confidence": m.confidence}
      for m in mem_hits])
  ```
  → 对应清单「Memory」。

### 1.5 生成回复(`nodes.generate_reply` / `_generate_reply_inner`)
- 渲染 prompt 后(`prompt_registry` 拿到模板):
  ```python
  trace.record_prompt(template_name=<name>, version=<ver>,
                      variables={...摘要用变量...},
                      rendered_messages=[{"role":..., "content":...}, ...])
  ```
  → 对应清单「Prompt」。
- 生成 LLM 调用返回后:再次 `trace.record_model(..., stage="generate", ttft_ms=<TTFT>)`
  → 模型 TTFT 也写 `record_latency(model_ttft_ms=<TTFT>)`。
- 有工具调用时(`tool_registry`):每次工具执行
  ```python
  trace.record_tool(name=<tool>, args={...}, acl=<允许/拒绝>,
                    ms=<耗时>, result=<返回>, error=<异常或 None>)
  ```
  → 对应清单「Tool」;工具总耗时汇总进 `record_latency(tool_ms=...)`。

### 1.6 结果(`nodes.finalize` / runner `parse_result`)
- 最终答案就绪后:
  ```python
  trace.record_result(answer=<最终答案>,
                      parsed={"intent":..., "emotion":..., "reply_type":...},
                      feedback=None,              # 用户反馈异步回填(见下)
                      eval_score=<shadow_eval 分数或 None>)
  ```
  → 对应清单「结果」。

### 1.7 成本网关(`llm_gateway`)
- 请求结束前(累计所有模型调用的 token/价格):
  ```python
  trace.record_cost(input_cost=<¥>, output_cost=<¥>,
                    cache_hit=<bool>, tenant=<租户>, scene=<场景>)
  ```
  → 对应清单「成本」。`tenant`/`scene` 落成独立索引列,支持按租户+场景归因。

### 1.8 用户反馈 / 评测(异步回填)
- 反馈接口收到 thumbs_up/down 时,可 `load_trace(request_id)` 后重写,或在反馈
  存储(`feedback_store`)侧单独落库。当前实现:反馈/评测分在 `record_result`
  一次写入;若晚到,按 `request_id` `INSERT OR REPLACE` 覆盖即可(主键幂等)。

---

## 2. 请求结束:finalize + 落盘

**保持现状**——`app_fastapi` 已经在 `finally` / BackgroundTask 里调:

```python
await get_trace_service(TRACE_DB).finalize_and_save(trace)
```

`finalize_and_save` 会:`finalize()`(补总耗时)→ `to_dict(redact=True)`(深度 PII
脱敏)→ 写 `traces` 表(aiosqlite 真异步;无则 `asyncio.to_thread` 降级 sqlite3 WAL)。
任何持久化异常都被吞掉并记日志——**trace 落盘失败绝不影响用户请求**。

流式(`_chat_sse`)与非流式两条路径各有一处 `finalize_and_save`,均无需改动。

---

## 3. 与已有 metrics / 日志的关系

| 维度 | 载体 | 粒度 | 用途 |
|------|------|------|------|
| **Trace** | `observability.TraceSession` → `traces` 表 | **单请求全量** | **可回放**:重现一次请求的 prompt/检索/模型/成本/结果,做 RAG 迭代对比、差评复盘 |
| Metrics | `agent/metrics.py`(Prometheus) | **聚合** | 大盘/告警:QPS、P95 延迟、token/成本总量、命中率 |
| Log | `agent/logging_setup.py`(JSON) | **事件流** | 排障:按 `request_id` 串联一次请求的离散日志行 |

三者用 **`request_id`** 关联:一条 trace ↔ 若干 log 行 ↔ 贡献若干 metric 样本。
- metrics 回答「**整体**现在健康吗」;
- log 回答「这条请求**发生了什么**(时序事件)」;
- trace 回答「这条请求**能不能重放**、当时召回了什么、换新 RAG 会不会更好」。

`AlertService`(滑动窗口告警)仍在本模块,由后台任务周期调 `check_and_alert()`,
与 trace 记录互不耦合。

---

## 4. 回放与排障工作流(运维/算法自查)

```bash
# 捞出所有差评请求
python -m scripts.trace_tool list --low-score
# 捞出所有失败请求
python -m scripts.trace_tool list --failed --user u_123

# 看一次请求的结构化时间线(每个 span 的耗时/输入/输出)
python -m scripts.trace_tool show <request_id>

# 用当时的 query 重新走一遍检索,对比"当时召回 vs 现在召回"(RAG 迭代前后)
python -m scripts.trace_tool replay <request_id> --rerun

# 对比两个 trace(如改 prompt 版本前后)
python -m scripts.trace_tool diff <id_before> <id_after>
```

程序内(如批量复盘脚本):

```python
from agent import trace_replay
bad = trace_replay.list_traces({"low_score": True, "scene": "returns"})
for row in bad:
    diff = trace_replay.replay(row["request_id"], "rerun",
                               retriever=my_new_retriever, echo=False)
    # 比对 diff["sources_added"] / ["sources_removed"] 评估新 RAG 是否改善
```
