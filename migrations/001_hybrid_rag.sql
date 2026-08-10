-- ============================================================================
-- 001_hybrid_rag.sql — 混合检索（pgvector + 全文）基础表结构
-- 幂等：可重复执行。与 agent/pgvector_hybrid.py 中 SCHEMA_SQL_TEMPLATE 保持一致。
-- 向量维度默认 1024（nvidia/llama-nemotron-embed-vl-1b-v2）；
-- 换模型时同步修改 vector(N) 与 env RAG_EMBED_DIM。
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- 中文全文检索配置：优先 zhparser（需镜像内已装扩展，如 abcfy2/zhparser 镜像），
-- 缺失时降级 simple 配置 + 应用层 jieba 预分词（content_tokens 列）。
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

-- 文档表（原始全文，便于重建分块）
CREATE TABLE IF NOT EXISTS rag_documents (
    doc_id        TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    tenant_id     TEXT,
    tags          TEXT[] NOT NULL DEFAULT '{}',
    content       TEXT NOT NULL DEFAULT '',
    index_version INT  NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 分块表（parent-child 两级；检索命中 child，输出映射回 parent）
CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id       TEXT PRIMARY KEY,
    doc_id         TEXT NOT NULL REFERENCES rag_documents(doc_id) ON DELETE CASCADE,
    parent_id      TEXT,                      -- child 行指向 parent 的 chunk_id
    is_parent      BOOLEAN NOT NULL DEFAULT FALSE,
    title          TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    section        TEXT NOT NULL DEFAULT '',   -- 小节标题（markdown heading），section 级命中评测用
    tenant_id      TEXT,
    tags           TEXT[] NOT NULL DEFAULT '{}',
    content        TEXT NOT NULL,
    content_tokens TEXT NOT NULL DEFAULT '',  -- 应用层预分词（simple 降级路径）
    embedding      vector(1024),
    index_version  INT  NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- tsvector 生成列（标题权重 A，正文 B）。
    -- 注：生成列表达式必须 IMMUTABLE，故固定 'simple' 配置 + 预分词列；
    -- 若集群装有 zhparser 并希望原生中文分词，参见文件末尾附录 A 的重建步骤。
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(content_tokens, '')), 'B')
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_tsv    ON rag_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_tenant ON rag_chunks (tenant_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_parent ON rag_chunks (parent_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_tags   ON rag_chunks USING GIN (tags);
-- HNSW 向量索引（pgvector >= 0.5；余弦距离，与查询 <=> 一致）
CREATE INDEX IF NOT EXISTS idx_rag_chunks_vec    ON rag_chunks
    USING hnsw (embedding vector_cosine_ops);

COMMIT;

-- ============================================================================
-- 附录 A：zhparser 原生分词（可选优化，装有 zhparser 时执行）
-- 生成列不能引用非 IMMUTABLE 的 to_tsvector(config, text) 自定义配置组合时，
-- 可改为普通列 + 触发器维护：
--
--   ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS tsv_zh tsvector;
--   UPDATE rag_chunks SET tsv_zh =
--       setweight(to_tsvector('rag_zh', coalesce(title,'')), 'A') ||
--       setweight(to_tsvector('rag_zh', coalesce(content,'')), 'B');
--   CREATE INDEX IF NOT EXISTS idx_rag_chunks_tsv_zh
--       ON rag_chunks USING GIN (tsv_zh);
--   -- 触发器：
--   CREATE OR REPLACE FUNCTION rag_chunks_tsv_zh_update() RETURNS trigger AS $f$
--   BEGIN
--       NEW.tsv_zh :=
--           setweight(to_tsvector('rag_zh', coalesce(NEW.title,'')), 'A') ||
--           setweight(to_tsvector('rag_zh', coalesce(NEW.content,'')), 'B');
--       RETURN NEW;
--   END $f$ LANGUAGE plpgsql;
--   DROP TRIGGER IF EXISTS trg_rag_chunks_tsv_zh ON rag_chunks;
--   CREATE TRIGGER trg_rag_chunks_tsv_zh BEFORE INSERT OR UPDATE
--       ON rag_chunks FOR EACH ROW EXECUTE FUNCTION rag_chunks_tsv_zh_update();
--
-- 查询侧相应把 to_tsquery('simple', ...) 换成 to_tsquery('rag_zh', ...)、
-- tsv 换成 tsv_zh。
-- ============================================================================
