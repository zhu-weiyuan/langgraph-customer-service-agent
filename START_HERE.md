# 学习入口：LangGraph Customer Service Agent

项目包含运行代码、数据库迁移、评测、压测、前端和历史调试文件。不要从根目录按文件名乱看，按下面的路径学习。

## 1. 推荐学习顺序

### 第一阶段：先跑通主链路
1. app_fastapi.py：后端 HTTP/SSE 入口。
2. agent/state.py：LangGraph 状态结构。
3. agent/graph.py：工作流节点和路由。
4. agent/nodes.py：意图、情绪、RAG、回复和满意度处理。
5. agent/runner.py：调用图、流式事件和 checkpoint。
6. frontend/src/api/client.ts：前端 API 和 SSE 协议。
7. frontend/src/stores/chat.ts：消息、会话恢复和流式拼接。

### 第二阶段：理解 RAG 和记忆
1. agent/rag_backend.py：选择 pgvector / hybrid / tfidf 后端。
2. agent/pgvector_hybrid.py：PostgreSQL + pgvector + 全文检索 + RRF。
3. agent/hybrid_rag.py：parent-child 切分、召回、重排和上下文截断。
4. agent/embedding_client.py：Embedding 请求和降级记录。
5. agent/context_assembler.py：组装 RAG、会话历史、长期记忆。
6. agent/user_memory.py、agent/runtime_db.py：长期记忆和运行时持久化。
7. knowledge/、migrations/：知识源和数据库结构。

### 第三阶段：模型网关和工程能力
1. agent/llm_gateway.py：路由、重试、fallback、熔断、预算、缓存和成本。
2. circuit_breaker.py、resilience.py：故障保护。
3. rate_limiter.py、redis_cache.py：Redis 限流和缓存。
4. prompt_registry.py：Prompt 版本、灰度、回退和审计。
5. observability.py、metrics.py、otel_setup.py：Trace、Prometheus 和 OpenTelemetry。

### 第四阶段：评测与自我改进
1. eval/intent_emotion_rag_dataset.jsonl：意图、情绪、RAG 综合评测集。
2. scripts/run_intent_emotion_rag_eval.py：综合评测入口。
3. eval/harness.py、eval/metrics.py：评测框架和指标。
4. agent/feedback_store.py、agent/self_improve.py、agent/shadow_eval.py：反馈闭环和 Prompt 改进。

## 2. 当前真正的运行入口

- 后端：app_fastapi.py，默认端口 7860。
- 前端：frontend/，Vite 默认端口 5173。
- 数据库：PostgreSQL + pgvector；Redis 用于限流/缓存。
- 当前运行时长期记忆不是 SQLite；SQLite 文件只是历史调试或旧数据痕迹。

## 3. 评测优先看什么

- 结果目录：eval/reports/。
- 综合数据集：eval/intent_emotion_rag_dataset.jsonl。
- 评测脚本：scripts/run_intent_emotion_rag_eval.py。
- 指标公式：eval/EVAL_METRICS.md。
- 历史对比：docs/RAG_COMPARISON_REPORT.md。

已验证的一批综合评测：意图准确率 100%，情绪准确率 100%，RAG 正样本 Hit@1 96.88%、Hit@3/5 100%，负样本误命中率 0%。这批结果中情绪采用 keyword 模式，不能直接等同于完整生产 LLM 分类器效果。

## 4. 根目录哪些先不要学

archive/legacy_backend/ 中的旧 app 实现、根目录备份文件、_*.py、debug 脚本和日志多数是历史兼容、一次性排障或运行输出。详见 docs/ROOT_FILES.md。
