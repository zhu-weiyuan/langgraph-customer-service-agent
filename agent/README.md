# agent/：后端核心业务源码

这是最重要的学习目录，先读主链路，再读支撑能力。

## 主链路

| 文件 | 重点 |
|---|---|
| state.py | 唯一的 CustomerServiceState、消息 reducer、意图/情绪/会话字段 |
| graph.py | LangGraph 图构建、节点连接、满意度重试和人工升级路由 |
| nodes.py | 意图识别、情绪识别、RAG、回复生成、满意度处理 |
| runner.py | 图执行、astream 真流式事件、PostgreSQL checkpoint |
| context_assembler.py | 历史、长期记忆、RAG 证据如何组装为上下文 |
| context_compaction.py | 上下文过长时保留首条消息和最近轮次 |

## RAG

- rag_backend.py：选择 pgvector / hybrid / tfidf，严格模式和 fallback。
- pgvector_hybrid.py：PostgreSQL 向量检索、全文检索、RRF 融合。
- hybrid_rag.py：parent-child 切分、BM25/TF-IDF、重排和上下文截断。
- agentic_rag.py：查询改写、充分性判断和再次检索。
- embedding_client.py：Embedding 客户端、维度和降级观测。
- pgvector_store.py：pgvector 数据访问。
- rag.py、rag_legacy.py：早期本地检索，了解历史即可。

## 模型网关和可靠性

- llm_gateway.py：路由、重试、fallback、熔断、Token 预算、缓存、成本。
- llm_client.py：统一 LLM 客户端。
- circuit_breaker.py、resilience.py：错误隔离、退避和熔断。
- rate_limiter.py、redis_cache.py：Redis 限流、缓存和并发保护。
- token_estimator.py：输入/输出预算估算。

## 持久化、观测和改进

- runtime_db.py：运行时 PostgreSQL 连接、会话、消息、反馈等。
- user_memory.py：长期记忆写入、向量检索和软删除。
- observability.py、metrics.py、otel_setup.py：Trace、Prometheus、OTel。
- logging_setup.py、logging_config.py：结构化日志。
- prompt_registry.py：Prompt 版本、灰度、提升、回退、运行审计。
- feedback_store.py、self_improve.py、shadow_eval.py：反馈到评测再到 Prompt 改进。

*_new.py、*_legacy.py、*.bak、nodes.py.* 多为迭代或兼容实现，先掌握主线后再比较。

子目录 security/：PII 脱敏和 Prompt Injection 防护。
