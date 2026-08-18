-- PostgreSQL + pgvector knowledge retrieval schema.
-- Apply only after provisioning PostgreSQL with the pgvector extension.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id BIGSERIAL PRIMARY KEY,
    chunk_key CHAR(64) NOT NULL UNIQUE,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS knowledge_chunks_source_idx ON knowledge_chunks(source);
-- HNSW gives approximate nearest-neighbor search at scale. Tune ef_search per
-- deployment after measuring recall and latency against the current ENN baseline.
CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
