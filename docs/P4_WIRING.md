# P4 半自动自我改进闭环 — app 层接线说明

本阶段只交付模块/脚本/文档，**未改 app_fastapi.py / nodes.py / graph.py**。
以下是 app 层代理需要做的最小接线（全部为增量调用，不改变现有行为）。

统一存储：所有 P4 表在同一个 SQLite（WAL），默认
`data/p4_self_improve.db`，可用环境变量 `P4_DB_PATH` 覆盖。
建议在 app 启动时构造单例：

```python
# app 启动处（lifespan / module level 均可；对象无状态，每操作独立短连接）
from agent.feedback_store import FeedbackStore
from agent.prompt_registry import PromptRegistry, seed_default_prompts

feedback_store = FeedbackStore()
prompt_registry = PromptRegistry()
seed_default_prompts(prompt_registry)   # 幂等:首版 system_prompt 取自 nodes.py 常量
```

## 1. 显式反馈端点 → feedback_store

三个端点在写完各自现有表之后，追加一行调用（同步 API，包在
`asyncio.to_thread` 里以免阻塞事件循环）：

- `POST /api/rating`（`RatingRequest{session_id, message_index, stars}`）：
  ```python
  await asyncio.to_thread(feedback_store.record_rating,
                          session_id, data.stars,
                          request_id=f"msg-{data.message_index}")
  ```
  只有 stars ∈ {1,2} 才会入库（阈值 `LOW_RATING_THRESHOLD=3`），高分返回 None。

- `POST /api/reaction`（`ReactionRequest{session_id, message_id, emoji, active}`）：
  ```python
  await asyncio.to_thread(feedback_store.record_reaction,
                          session_id, data.emoji, data.active,
                          request_id=data.message_id)
  ```
  只有负向 emoji（👎/😡/💔）且 active 时入库。

- `POST /api/feedback`（`FeedbackRequest{session_id, query, answer, rating, comment}`）：
  ```python
  await asyncio.to_thread(feedback_store.record_feedback,
                          session_id, data.query, data.answer,
                          data.rating, data.comment)
  ```
  低分或带 comment 才入库。query/answer/comment 入库前自动经
  `agent.security.pii_redactor` 脱敏（守卫导入，缺席时降级原文）。

## 2. 隐式信号

- **转人工（escalation）**：信号源是 `agent/nodes.py::escalate_to_human`
  （graph 节点，`interrupt(...)` 之前）。P4 不改 nodes.py，推荐在 app 层
  chat handler 里检测 graph 输出 `state["escalate"] is True`（或捕获
  `human_intervention_required` interrupt payload）后调用：
  ```python
  await asyncio.to_thread(feedback_store.record_escalation,
                          session_id, query=user_message,
                          answer=last_ai_reply, reason=f"retries={retry_count}")
  ```
  若日后允许改 nodes.py，直接放进 `escalate_to_human` 的 print 之后一行即可。

- **连续追问（repeat_question）**：在 chat 端点每轮收到用户消息时调用
  （同会话与上一问 difflib 相似度 > 0.6 自动判定并入库，否则只更新游标）：
  ```python
  await asyncio.to_thread(feedback_store.record_repeat_question,
                          session_id, user_message)
  ```

## 3. get_active prompt 在 nodes 的调用点

`agent/nodes.py` 现状：模块级常量 `SYSTEM_PROMPT`（第 175 行）被
`_call_llm` 默认参数、`RAG_SYSTEM_PROMPT_TEMPLATE`（178 行）及
546/642 行的两处子调用引用。接线方式（不必一次到位）：

1. 最小侵入：在 `_call_llm` 内把 `system=SYSTEM_PROMPT` 的默认改为运行期解析：
   ```python
   from .prompt_registry import PromptRegistry, seed_default_prompts
   _registry = PromptRegistry(); seed_default_prompts(_registry)

   def _active_system_prompt(state=None) -> str:
       session_id = (state or {}).get("session_id")
       tenant = (state or {}).get("tenant_id")
       return _registry.get_active("system_prompt", tenant=tenant,
                                   session_seed=session_id, log_run=True).content
   ```
   session_seed 用 session_id（app_fastapi 的 `_session_id_for_request` 产物），
   保证同一会话稳定落同一灰度桶；tenant 用 JWT 的 `auth_tenant_id`。
2. `RAG_SYSTEM_PROMPT_TEMPLATE` 是 `SYSTEM_PROMPT + 后缀`，改为函数
   `rag_system_prompt(state)` 现拼接即可。
3. `reinforce_system_prompt`（prompt_guard 安全加固）继续包在
   registry 返回的内容外层：`reinforce_system_prompt(_active_system_prompt(state))`。
4. judge prompt：`prompt_registry` 已 seed `judge_prompt`（kind=judge）；
   shadow_eval 自动从 registry 取。工具描述可用 `kind="tool_desc"` 注册。

注意：`agent/context_assembler.py` 旧接口（`register/get/render/_versions`）
已在新 registry 中保留兼容层，无需改动。

## 4. 闭环运维

```bash
python scripts/improvement_cycle.py            # 收集→分析→候选→影子评测→待审批摘要
python scripts/improvement_cycle.py --dry-run  # 无 LLM 环境验证链路
python scripts/approve_prompt.py list          # 查看版本/状态/灰度
python scripts/approve_prompt.py approve 3 --percent 10
python scripts/approve_prompt.py promote
python scripts/approve_prompt.py rollback      # 一键回滚上一版本
```

cron（脚本 docstring 内亦有）：
`0 3 * * * cd <repo> && python3 scripts/improvement_cycle.py >> logs/improvement_cycle.log 2>&1`

状态机：`candidate → (shadow eval) → pending_approval | rejected →
(人工 approve) → approved → released(percent<100 灰度 → promote_full 全量)`；
`rollback` 把被回滚版本标记 `retired` 并恢复上一全量版本。

## 5. golden set

`eval/golden_set.jsonl`：50 条（normal 25 / edge 13 / adversarial 7 /
high_weight 5,权重 3），字段
`id/category/difficulty/query/expected_keywords/should_refuse/weight`。
业务面取自 README/eval.py 的知识域（WiFi/蓝牙/退货/保修/物流/发票/价格/
优惠/云服务/智能家居）。staged 内容中无 knowledge/ 与旧 bad_cases.jsonl，
故按 README 描述的业务生成；若线上存在旧 bad_cases.jsonl，可把高频失败
query 追加为新的 golden 行（同 schema）。
