# -*- coding: utf-8 -*-
"""
pgvector 混合检索 — Postgres 侧实现（psycopg / pgvector / jieba 全守卫）

功能：
  * 建表 SQL：chunks 表含 parent_id / tenant_id / tags / created_at /
    index_version、vector 列、tsvector 生成列 + GIN 索引；
    zhparser 可用则用 zhparser 分词配置，否则 simple + 应用层预分词。
  * 单条 SQL 内完成 向量 + tsquery 双路 CTE + RRF 融合。
  * upsert 文档 API（配合 hybrid_rag.chunk_document 的 parent-child 分块）。
  * 迁移文件见 migrations/001_hybrid_rag.sql（与本模块 SQL 保持一致）。

依赖不可用时：模块可 import、可 py_compile；实例化 PgHybridStore 时才报错。
"""

from __future__ import annotations

import json
import os
import re
import time
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

# ── 三方守卫导入 ─────────────────────────────────────────────

try:
    import psycopg  # psycopg3
    _PSYCOPG_OK = True
except Exception:
    psycopg = None
    _PSYCOPG_OK = False

try:
    from pgvector.psycopg import register_vector
    _PGVECTOR_OK = True
except Exception:
    register_vector = None
    _PGVECTOR_OK = False

try:
    import jieba
    _JIEBA_OK = True
except Exception:
    jieba = None
    _JIEBA_OK = False


EMBED_DIM = int(os.environ.get("RAG_EMBED_DIM", "1024"))
logger = logging.getLogger(__name__)

# ── SQL 定义（与 migrations/001_hybrid_rag.sql 一致）─────────

SCHEMA_SQL_TEMPLATE = """
CREATE EXTENSION IF NOT EXISTS vector;

-- 文本检索配置：优先 zhparser，缺失时降级 simple（应用层预分词补偿）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'zhparser') THEN
        IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'rag_zh') THEN
            CREATE TEXT SEARCH CONFIGURATION rag_zh (PARSER = zhparser);
            ALTER TEXT SEARCH CONFIGURATION rag_zh
                ADD MAPPING FOR n,v,a,i,e,l,j,x,q,t,m WITH simple;
        END IF;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS rag_documents (
    doc_id        TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    tenant_id     TEXT,
    tags          TEXT[] NOT NULL DEFAULT '{{}}',
    content       TEXT NOT NULL DEFAULT '',
    index_version INT  NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id      TEXT PRIMARY KEY,
    doc_id        TEXT NOT NULL REFERENCES rag_documents(doc_id) ON DELETE CASCADE,
    parent_id     TEXT,                       -- parent-child：child 行指向 parent chunk_id
    is_parent     BOOLEAN NOT NULL DEFAULT FALSE,
    title         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    tenant_id     TEXT,
    tags          TEXT[] NOT NULL DEFAULT '{{}}',
    content       TEXT NOT NULL,
    content_tokens TEXT NOT NULL DEFAULT '',  -- 应用层预分词（simple 配置降级用）
    embedding     vector({dim}),
    index_version INT  NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- tsvector 生成列：zhparser 存在时建议改用 rag_zh 配置重建；simple 用预分词列
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(content_tokens, '')), 'B')
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_tsv    ON rag_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_tenant ON rag_chunks (tenant_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_parent ON rag_chunks (parent_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_tags   ON rag_chunks USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_vec    ON rag_chunks
    USING hnsw (embedding vector_cosine_ops);
"""

# 单条 SQL：向量路 + 全文路 双 CTE + RRF 融合（k 可传参）
HYBRID_SEARCH_SQL = """
WITH vec AS (
    SELECT chunk_id, parent_id, title, source, content, tenant_id, tags, created_at,
           ROW_NUMBER() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rnk
    FROM rag_chunks
    WHERE embedding IS NOT NULL
      AND is_parent = FALSE
      AND (%(tenant_id)s::text IS NULL OR tenant_id IS NULL OR tenant_id = %(tenant_id)s)
      AND (%(tags)s::text[] IS NULL OR tags = '{}' OR tags && %(tags)s::text[])
    ORDER BY embedding <=> %(qvec)s::vector
    LIMIT %(limit_each)s
),
kw AS (
    SELECT chunk_id, parent_id, title, source, content, tenant_id, tags, created_at,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank(tsv, to_tsquery('simple', %(tsquery)s)) DESC
           ) AS rnk
    FROM rag_chunks
    WHERE tsv @@ to_tsquery('simple', %(tsquery)s)
      AND is_parent = FALSE
      AND (%(tenant_id)s::text IS NULL OR tenant_id IS NULL OR tenant_id = %(tenant_id)s)
      AND (%(tags)s::text[] IS NULL OR tags = '{}' OR tags && %(tags)s::text[])
    LIMIT %(limit_each)s
),
fused AS (
    SELECT chunk_id, parent_id, title, source, content, tenant_id, tags, created_at,
           SUM(1.0 / (%(rrf_k)s + rnk)) AS rrf_score
    FROM (
        SELECT * FROM vec
        UNION ALL
        SELECT * FROM kw
    ) u
    GROUP BY chunk_id, parent_id, title, source, content, tenant_id, tags, created_at
)
SELECT chunk_id, parent_id, title, source, content, rrf_score, created_at
FROM fused
ORDER BY rrf_score DESC
LIMIT %(top_k)s;
"""

VECTOR_ONLY_SQL = """
SELECT chunk_id, parent_id, title, source, content, created_at,
       1 - (embedding <=> %(qvec)s::vector) AS score
FROM rag_chunks
WHERE embedding IS NOT NULL AND is_parent = FALSE
  AND (%(tenant_id)s::text IS NULL OR tenant_id IS NULL OR tenant_id = %(tenant_id)s)
  AND (%(tags)s::text[] IS NULL OR tags = '{}' OR tags && %(tags)s::text[])
ORDER BY embedding <=> %(qvec)s::vector
LIMIT %(top_k)s;
"""

KEYWORD_ONLY_SQL = """
SELECT chunk_id, parent_id, title, source, content, created_at,
       ts_rank(tsv, to_tsquery('simple', %(tsquery)s)) AS score
FROM rag_chunks
WHERE tsv @@ to_tsquery('simple', %(tsquery)s) AND is_parent = FALSE
  AND (%(tenant_id)s::text IS NULL OR tenant_id IS NULL OR tenant_id = %(tenant_id)s)
  AND (%(tags)s::text[] IS NULL OR tags = '{}' OR tags && %(tags)s::text[])
ORDER BY score DESC
LIMIT %(top_k)s;
"""


# ── 纯函数：预分词 / tsquery 构造（无 DB 也可测）─────────────

def pretokenize(text: str) -> str:
    """中文预分词（jieba 优先，降级字符 bigram），供 simple 配置的 tsvector 用。"""
    if not text:
        return ""
    if _JIEBA_OK:
        tokens = [t.strip().lower() for t in jieba.cut(text) if t.strip()]
    else:
        tokens = []
        for run in re.findall(r"[一-鿿]+|[a-zA-Z0-9]+", text):
            if re.match(r"[a-zA-Z0-9]", run):
                tokens.append(run.lower())
            else:
                tokens.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return " ".join(tokens)


def build_tsquery(query: str) -> str:
    """query → tsquery 字符串（OR 连接，转义特殊字符）。"""
    tokens = pretokenize(query).split()
    safe = [re.sub(r"[^0-9a-zA-Z一-鿿]", "", t) for t in tokens]
    safe = [t for t in safe if t]
    if not safe:
        return "empty_query_no_match"
    return " | ".join(dict.fromkeys(safe))  # 去重保序


def _vec_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"


# ── Store 实现 ───────────────────────────────────────────────

class PgHybridStore:
    """Postgres 混合检索存储。

    Args:
        dsn: postgres 连接串（如 postgresql://user:pass@localhost:5432/rag）
        embed_fn: fn(text) -> List[float]，向量路需要；None 时向量路返回空
        dim: 向量维度（须与建表一致）
    """

    def __init__(self, dsn: str,
                 embed_fn: Optional[Callable[[str], List[float]]] = None,
                 dim: int = EMBED_DIM):
        if not _PSYCOPG_OK:
            raise RuntimeError(
                "psycopg 未安装：pip install 'psycopg[binary]' pgvector")
        self.dsn = dsn
        self.embed_fn = embed_fn
        self.dim = dim
        self._conn = None
        # ?? embedding ?????????????????????
        # ???????? PostgreSQL ???????? SQLite/TF-IDF?
        try:
            self._embedding_failure_cooldown = max(
                0.0, float(os.getenv("RAG_EMBEDDING_FAILURE_COOLDOWN_SECONDS", "60")))
        except (TypeError, ValueError):
            self._embedding_failure_cooldown = 60.0
        self._embedding_failed_until = 0.0
        self._embedding_failure_logged = False

    @classmethod
    def from_env(cls, embed_fn: Optional[Callable] = None) -> "PgHybridStore":
        dsn = (os.environ.get("PG_DSN")
               or os.environ.get("RAG_PG_DSN")
               or os.environ.get("DATABASE_URL")
               or "postgresql://postgres:postgres@localhost:5432/agent")
        if embed_fn is None:
            embed_fn = _default_embed_fn()
        return cls(dsn, embed_fn=embed_fn)

    # -- connection --

    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True)
            if _PGVECTOR_OK:
                try:
                    register_vector(self._conn)
                except Exception:
                    pass
        return self._conn

    def close(self):
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # -- schema --

    def ensure_schema(self):
        """建表（幂等）。生产环境请走 migrations/001_hybrid_rag.sql。"""
        with self._connect().cursor() as cur:
            cur.execute(SCHEMA_SQL_TEMPLATE.format(dim=self.dim))

    # -- write path --

    def upsert_document(self, doc_id: str, text: str,
                        title: str = "", source: str = "",
                        tenant_id: Optional[str] = None,
                        tags: Optional[List[str]] = None,
                        index_version: int = 1,
                        child_size: int = 300,
                        parent_size: int = 1200) -> Dict[str, int]:
        """文档 → parent/child 分块 → 嵌入 child → upsert。

        Returns: {"parents": n, "children": m}
        """
        from .hybrid_rag import chunk_document
        chunked = chunk_document(text, child_size=child_size,
                                 parent_size=parent_size, doc_id=doc_id)
        tags = tags or []
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rag_documents
                    (doc_id, title, source, tenant_id, tags, content, index_version, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (doc_id) DO UPDATE SET
                    title = EXCLUDED.title, source = EXCLUDED.source,
                    tenant_id = EXCLUDED.tenant_id, tags = EXCLUDED.tags,
                    content = EXCLUDED.content,
                    index_version = EXCLUDED.index_version, updated_at = now()
                """,
                (doc_id, title, source, tenant_id, tags, text, index_version))
            # 重建该文档的 chunk（简单可靠：先删后插）
            cur.execute("DELETE FROM rag_chunks WHERE doc_id = %s", (doc_id,))

            for pid, parent in chunked["parents"].items():
                self._insert_chunk(cur, pid, doc_id, None, True, title, source,
                                   tenant_id, tags, parent["text"],
                                   embedding=None, index_version=index_version)
            for child in chunked["children"]:
                emb = None
                if self.embed_fn is not None:
                    try:
                        emb = self.embed_fn(child["text"])
                    except Exception:
                        emb = None
                self._insert_chunk(cur, child["child_id"], doc_id,
                                   child["parent_id"], False, title, source,
                                   tenant_id, tags, child["text"],
                                   embedding=emb, index_version=index_version)
        return {"parents": len(chunked["parents"]),
                "children": len(chunked["children"])}

    @staticmethod
    def _insert_chunk(cur, chunk_id, doc_id, parent_id, is_parent, title,
                      source, tenant_id, tags, content, embedding, index_version):
        emb_literal = _vec_literal(embedding) if embedding else None
        cur.execute(
            """
            INSERT INTO rag_chunks
                (chunk_id, doc_id, parent_id, is_parent, title, source,
                 tenant_id, tags, content, content_tokens, embedding, index_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
            ON CONFLICT (chunk_id) DO UPDATE SET
                content = EXCLUDED.content,
                content_tokens = EXCLUDED.content_tokens,
                embedding = EXCLUDED.embedding,
                index_version = EXCLUDED.index_version
            """,
            (chunk_id, doc_id, parent_id, is_parent, title, source,
             tenant_id, tags, content, pretokenize(content),
             emb_literal, index_version))

    # -- read path --

    def _query_embedding(self, query: str) -> Optional[List[float]]:
        """???????????????????????? PG ??????"""
        if self.embed_fn is None:
            return None
        now = time.monotonic()
        if now < self._embedding_failed_until:
            return None
        try:
            qvec = self.embed_fn(query)
            if not qvec:
                raise RuntimeError("embedding service returned an empty vector")
            self._embedding_failure_logged = False
            return list(qvec)
        except Exception as exc:
            self._embedding_failed_until = now + self._embedding_failure_cooldown
            if not self._embedding_failure_logged:
                logger.warning(
                    "[pgvector] embedding unavailable; using PostgreSQL keyword search "
                    "for %.1fs: %s", self._embedding_failure_cooldown, exc)
                self._embedding_failure_logged = True
            return None

    def hybrid_search(self, query: str, top_k: int = 50,
                      tenant_id: Optional[str] = None,
                      tags: Optional[List[str]] = None,
                      rrf_k: int = 60) -> List[Dict[str, Any]]:
        """单条 SQL 内向量 + tsquery 双路 CTE + RRF 融合。"""
        qvec = self._query_embedding(query)
        if not qvec:
            return self.keyword_search(query, top_k=top_k,
                                       tenant_id=tenant_id, tags=tags)
        params = {
            "qvec": _vec_literal(qvec),
            "tsquery": build_tsquery(query),
            "tenant_id": tenant_id,
            "tags": tags,
            "limit_each": max(top_k, 50),
            "rrf_k": rrf_k,
            "top_k": top_k,
        }
        with self._connect().cursor() as cur:
            cur.execute(HYBRID_SEARCH_SQL, params)
            rows = cur.fetchall()
        return [self._row_to_result(r, score_idx=5) for r in rows]

    def vector_search(self, query: str, top_k: int = 50,
                      tenant_id: Optional[str] = None,
                      tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """dense 单路（供 HybridRetriever 的 vector_search_fn 注入）。"""
        qvec = self._query_embedding(query)
        if not qvec:
            return []
        params = {"qvec": _vec_literal(qvec), "tenant_id": tenant_id,
                  "tags": tags, "top_k": top_k}
        with self._connect().cursor() as cur:
            cur.execute(VECTOR_ONLY_SQL, params)
            rows = cur.fetchall()
        return [self._row_to_result(r, score_idx=6) for r in rows]

    def keyword_search(self, query: str, top_k: int = 50,
                       tenant_id: Optional[str] = None,
                       tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """sparse 单路（供 HybridRetriever 的 keyword_search_fn 注入）。"""
        params = {"tsquery": build_tsquery(query), "tenant_id": tenant_id,
                  "tags": tags, "top_k": top_k}
        with self._connect().cursor() as cur:
            cur.execute(KEYWORD_ONLY_SQL, params)
            rows = cur.fetchall()
        return [self._row_to_result(r, score_idx=6) for r in rows]

    def load_parent_map(self, tenant_id: Optional[str] = None) -> Dict[str, Dict]:
        """加载 parent 块映射，供 HybridRetriever(parent_map=...) 注入。"""
        sql = ("SELECT chunk_id, title, source, content FROM rag_chunks "
               "WHERE is_parent = TRUE")
        args: tuple = ()
        if tenant_id is not None:
            sql += " AND (tenant_id IS NULL OR tenant_id = %s)"
            args = (tenant_id,)
        with self._connect().cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        return {r[0]: {"parent_id": r[0], "title": r[1],
                       "source": r[2], "text": r[3]} for r in rows}

    @staticmethod
    def _row_to_result(row, score_idx: int) -> Dict[str, Any]:
        created = row[6] if score_idx == 5 else row[5]
        return {
            "id": row[0],
            "parent_id": row[1],
            "title": row[2] or "",
            "source": row[3] or "",
            "content": row[4] or "",
            "text": row[4] or "",
            "score": round(float(row[score_idx] or 0.0), 6),
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        }


def _default_embed_fn() -> Optional[Callable[[str], List[float]]]:
    """默认 embedding：优先统一 EmbeddingClient（OPENAI_*），降级 vector_rag。"""
    try:
        from .embedding_client import EmbeddingClient
        client = EmbeddingClient.from_env(strict=False)
        if client is not None:
            return client.embed_one
    except Exception:
        pass
    try:
        from .vector_rag import _get_embedding
        return lambda text: _get_embedding(text)
    except Exception:
        return None


if __name__ == "__main__":
    # 冒烟：仅打印 SQL 与分词结果，不连库
    print(build_tsquery("咋连WiFi 没声音"))
    print(json.dumps({"psycopg": _PSYCOPG_OK, "pgvector": _PGVECTOR_OK,
                      "jieba": _JIEBA_OK}))
