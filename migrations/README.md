# migrations/：PostgreSQL 数据库结构

按编号阅读：

| 文件 | 内容 |
|---|---|
| 001_pgvector_knowledge.sql | rag_documents、rag_chunks、embedding、全文检索和索引 |
| 001_hybrid_rag.sql | 早期 Hybrid RAG 结构，主要用于理解演进 |
| 002_runtime_postgres.sql | 运行时会话、消息、反馈、记忆等表 |
| 003_complete_postgres_runtime.sql | 完整运行时补充结构和兼容修复 |

当前架构重点：知识库向量、会话/消息/长期记忆都走 PostgreSQL；稀疏检索使用 PostgreSQL 全文检索，向量与全文结果在应用层做 RRF 融合。修改迁移后，同时检查 agent/pgvector_store.py、agent/runtime_db.py 和对应测试。
