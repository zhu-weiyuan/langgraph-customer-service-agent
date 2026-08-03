# 源码学习地图

## 一、一次请求的调用链

Vue App.vue
  -> frontend/src/stores/chat.ts
  -> frontend/src/api/client.ts
  -> POST /api/chat (SSE)
  -> app_fastapi.py
  -> agent/runner.py
  -> agent/graph.py
  -> agent/nodes.py
       -> agent/rag_backend.py
            -> agent/pgvector_hybrid.py
       -> agent/context_assembler.py
       -> agent/llm_gateway.py
  -> SSE event
  -> chat store 拼接消息并刷新会话

## 二、后端关键文件

### 必看

- app_fastapi.py：路由、中间件、SSE 输出、健康检查、指标接口。
- agent/state.py：状态如何在节点之间传递。
- agent/graph.py：什么时候结束、重试、升级人工。
- agent/nodes.py：业务判断在哪里发生。
- agent/runner.py：流式和 checkpoint 为什么能恢复。

### 第二轮

- agent/context_assembler.py、agent/context_compaction.py
- agent/rag_backend.py、agent/pgvector_hybrid.py、agent/hybrid_rag.py
- agent/llm_gateway.py、agent/resilience.py、agent/rate_limiter.py
- agent/runtime_db.py、agent/user_memory.py
- agent/observability.py、agent/metrics.py

## 三、前端关键文件

- frontend/src/App.vue：页面布局和模块组合。
- frontend/src/stores/chat.ts：会话、消息、流式增量、刷新恢复。
- frontend/src/api/client.ts：类型定义、HTTP 包装、SSE 解析。
- frontend/src/components/MessageList.vue：消息展示和流式状态。
- frontend/src/components/ChatInput.vue：输入和发送。
- frontend/src/components/SessionSidebar.vue：会话列表和切换。
- frontend/src/components/AnalyticsPanel.vue：观测/分析展示。

## 四、评测关键文件

- eval/intent_emotion_rag_dataset.jsonl：意图、情绪、RAG 期望信息。
- scripts/run_intent_emotion_rag_eval.py：读取数据集、调用意图/情绪/RAG、生成报告。
- eval/harness.py：通用评测运行器和目标阈值。
- eval/metrics.py：准确率、F1、Hit@K、MRR 等纯函数。
- eval/golden_set.jsonl：基础黄金集。
- eval/rag_eval_hard.jsonl：困难 RAG 样本。

## 五、数据库和观测关键文件

- migrations/001_pgvector_knowledge.sql：知识库、chunk、embedding、全文检索结构。
- migrations/002_runtime_postgres.sql、003_complete_postgres_runtime.sql：会话、消息、记忆、反馈表。
- agent/pgvector_store.py：向量数据访问。
- agent/runtime_db.py：运行时写入和读取。
- agent/observability.py：Trace 分区和落盘。
- agent/metrics.py：指标状态和 Prometheus 输出。
- monitoring/：Prometheus/Grafana 配置。
