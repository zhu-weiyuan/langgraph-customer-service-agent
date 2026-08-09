# tests/：自动化测试

按“纯函数 -> 组件集成 -> 外部服务”理解。

## 先看

- test_health_check.py：健康检查和就绪状态。
- test_rag_pure.py、test_rag_backend_pure.py：RAG 纯逻辑和后端选择。
- test_rag_grounding.py：RAG 证据约束。
- test_context_compaction.py、test_context_assembler_integration.py：上下文整理。
- test_user_memory_pure.py、test_memory_v2.py：长期记忆。
- test_metrics.py、test_logging.py：观测。
- test_eval_metrics_pure.py、test_eval_harness.py：评测系统。
- test_p1a_pure.py 到 test_p4_pure.py：分阶段回归。

## 运行

    pytest
    pytest tests/test_rag_backend_pure.py -q
    pytest -m integration

默认命令按 pytest.ini 排除 integration；需要 PostgreSQL/Redis/Embedding/LLM 的集成测试要显式运行并先确认服务状态。
