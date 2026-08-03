# knowledge/：知识库原文

这里的 12 个 Markdown 文件是客户服务知识源，覆盖账号安全、API、账单、错误码、安装、产品手册、促销、退换货、物流、故障排查和售后。

## 导入链路

knowledge/*.md
  -> scripts/migrate_knowledge_to_pgvector.py
  -> agent/hybrid_rag.py 的 parent-child 切分
  -> agent/embedding_client.py 生成 1024 维向量
  -> PostgreSQL rag_documents / rag_chunks
  -> agent/pgvector_hybrid.py 检索

## 当前切分方式

- 先按约 1200 个字符生成 parent。
- 再在每个 parent 内按约 300 个字符生成 child。
- _split_spans() 优先寻找段落、句子和 Markdown 边界，找不到时才硬切。
- child 用于精确检索，命中后通过 parent_id 回收较完整的 parent 作为上下文。
- 当前数据库已验证：49 个 parent、224 个 child，224 个 child 有 1024 维 embedding。

这里的 1200/300 是目标字符数，不是严格固定长度。当前实现按整篇文档的文本边界切分，没有把章节标题做成独立数据库实体；如果章节边界落在目标附近，parent 可能包含相邻章节的一小段内容。改进章节隔离时，应先修改切分函数并重新导入/重建 embedding，再比较评测结果。

先打开一篇 Markdown 原文，再对照 agent/hybrid_rag.py 的 chunk_document()，最后看 migrations/001_pgvector_knowledge.sql。
