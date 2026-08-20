# RAG 升级：PostgreSQL + pgvector 混合检索（用户实操 Runbook）

本升级把客服 RAG 切到 Postgres + pgvector（单容器即可）。检索后端由 env
`RAG_BACKEND=tfidf|hybrid|pgvector` 选择，**运行期失败自动降级 TF-IDF**，
随时可回滚（改回 env 即可，无数据风险）。

## 0. 快速 Runbook（Windows cmd）

```bat
:: (a) 启动 pgvector 容器（本机 Docker，单容器）
docker run -d --name pgvector -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agent pgvector/pgvector:pg16

:: (b) 在项目根目录的 .env 中追加（见下方 §2）后：

:: (c) 导入知识库（knowledge\*.md → 分块 → 嵌入 → 入库；先 dry-run 看分块统计）
python scripts\ingest_knowledge.py --dry-run
python scripts\ingest_knowledge.py

:: (d) 重启后端
python app_fastapi.py

:: (e) 验证：pgvector vs 现有 TF-IDF 检索质量对比（HitRate@5 / MRR）
python scripts\eval_retrieval.py --backend tfidf
python scripts\eval_retrieval.py --backend pgvector
```

## 1. 前置修复（本次已内置，无需操作）

- **.env 现在会被加载**：`app_fastapi.py` 顶部及 `scripts/ingest_knowledge.py`、
  `scripts/eval_retrieval.py` 均在读取任何配置前执行守卫式
  `load_dotenv()`（未装 python-dotenv 则静默跳过）。之前
  "OPENAI_API_KEY is not set" / embedding 401 的根因即 .env 从未被读取。
- **embedding 认证修复**：新增 `agent/embedding_client.py`（OpenAI 兼容
  `POST {OPENAI_BASE_URL}/embeddings`，`Authorization: Bearer {OPENAI_API_KEY}`，
  ≤32 条/批，超时+2 次重试，httpx 优先/requests 降级）。`vector_rag.py` 也已
  修复为运行时读取 OPENROUTER_API_KEY/OPENAI_API_KEY 并优先走该客户端。
- **RAG_BACKEND 已接入实际检索路径**：`agent/agentic_rag.py` 经
  `agent/rag_backend.py` 选择后端；`RAG_BACKEND=pgvector` 时查询走
  `PgHybridStore.hybrid_search`（单 SQL 双路 CTE + RRF），**不会**触发
  vector_rag 的全量索引 eager build；PG 挂/依赖缺失时该次请求自动回落
  TF-IDF（日志出 warning）。

## 2. .env 追加项

```ini
# embedding（OpenAI 兼容网关均可）
OPENAI_API_KEY=sk-xxxx
OPENAI_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=text-embedding-3-small

# Postgres（与上面 docker run 参数一致）
PG_DSN=postgresql://postgres:postgres@localhost:5432/agent

# 检索后端切换（先别设，等 §5 验证达标后再设）
RAG_BACKEND=pgvector

# 可选
RAG_INDEX_VERSION=v1
RAG_EMBED_DIM=1024        # 换 embedding 模型时与 migrations 里 vector(N) 同步
```

依赖：`pip install "psycopg[binary]" pgvector python-dotenv httpx`

## 3. 数据导入细节（scripts/ingest_knowledge.py）

- 自动加载 .env → 连接 PG_DSN → `PgHybridStore.ensure_schema()`（等价执行
  `migrations/001_hybrid_rag.sql`，幂等）→ 读取 `knowledge/*.md` →
  `hybrid_rag.chunk_document`（child≈300 / parent≈1200 字，段落/句子边界）→
  child 经 EmbeddingClient 批量嵌入 → `upsert_document`（重复导入按 doc_id
  全量重建，`index_version` 取自 RAG_INDEX_VERSION）。
- `--dry-run`：不连 PG、不嵌入，仅打印每文件分块统计（无依赖环境也可跑）。
- 中文分词：容器有 zhparser 扩展则迁移自动建 `rag_zh` 配置（切原生分词见
  迁移文件附录 A）；**没有也没关系**——默认降级 `simple` 配置 + 应用层
  jieba 预分词（`content_tokens` 列），已实现，零额外操作。

## 4. 运行期行为（agent/rag_backend.py）

| RAG_BACKEND | 行为 |
|---|---|
| 未设置 / `tfidf` / 非法值 | 与现状完全一致（rag.py BM25/TF-IDF + 原 vector 融合） |
| `hybrid` | 进程内双路（rag.py + vector_rag）+ RRF + 重排 |
| `pgvector` | PgHybridStore.hybrid_search（DB 内 RRF）→ RuleReranker → parent 映射 |
| 任一新后端运行期失败 | warning 日志 + 本次请求回落 TF-IDF（use_vector=False，不刷 401） |

自定义注入（LLM 改写、CrossEncoder 重排）仍走 `agent/hybrid_rag.py` 的
`HybridRetriever(...)`，见模块 docstring。

## 5. 操作顺序：迁移前先跑 retrieval eval 对比

**先留基线，再对比，达标才把 RAG_BACKEND 切到 pgvector：**

```bat
:: 0) 评估管线自检（零依赖）
python scripts\eval_retrieval.py --backend mock

:: 1) 基线：现有 TF-IDF/BM25
python scripts\eval_retrieval.py --backend tfidf --json > eval\baseline_tfidf.json

:: 2) 候选：pgvector（需已完成 §3 导入）
python scripts\eval_retrieval.py --backend pgvector --json > eval\candidate_pg.json

:: 3) 对比 HitRate@5 / MRR（含分类别：精确码/口语/多义/对抗）
::    准入建议：总体 HitRate@5 不降、"精确码"类不降、MRR 提升 >= 0.02
```

达标后在 .env 设 `RAG_BACKEND=pgvector` 并重启后端；回滚 = 删掉该行重启。

## 6. 文件清单与门槛

新增：`agent/hybrid_rag.py`、`agent/rag_backend.py`、`agent/embedding_client.py`、
`agent/pgvector_hybrid.py`、`agent/synonyms.json`、`migrations/001_hybrid_rag.sql`、
`scripts/ingest_knowledge.py`、`scripts/eval_retrieval.py`、
`eval/retrieval_eval.jsonl`、`tests/test_rag_pure.py`、`tests/test_rag_backend_pure.py`。
修改：`app_fastapi.py`（.env 加载）、`agent/agentic_rag.py`（后端接线）、
`agent/vector_rag.py`（认证修复）。

```bat
python -m py_compile app_fastapi.py agent\hybrid_rag.py agent\rag_backend.py agent\embedding_client.py agent\pgvector_hybrid.py agent\agentic_rag.py agent\vector_rag.py scripts\ingest_knowledge.py scripts\eval_retrieval.py
python -m unittest discover -s tests -v
```
