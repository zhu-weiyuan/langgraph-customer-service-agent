# scripts/：操作脚本

脚本按用途理解，不要和核心业务源码混在一起。

## 常用脚本

| 文件 | 用途 |
|---|---|
| run_intent_emotion_rag_eval.py | 综合意图、情绪、RAG 评测 |
| eval_retrieval.py | 只评测检索，不调用生成 LLM |
| run_eval.py、eval_real.py | 通用/真实评测入口 |
| ingest_knowledge.py | 知识库导入 |
| migrate_knowledge_to_pgvector.py | 迁移并生成 pgvector 知识库 |
| init_runtime_postgres.py | 初始化运行时 PostgreSQL |
| improvement_cycle.py、approve_prompt.py | Prompt 自我改进与审批 |
| run_load_test.py、load_test.py | 压测入口，当前阶段不优先运行 |

目录：loadtest/ 是 Locust/Mock 负载测试，data/ 是脚本样例输入。

check_*、debug_*、diagnose*、clean_*、final_*、trace_* 和 _gen_* 多为特定问题排障或数据生成脚本，运行前先看顶部说明并确认不会写入业务数据。
