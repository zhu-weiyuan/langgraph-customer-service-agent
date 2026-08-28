-- 007_knowledge_graph.sql - 可追溯的知识图谱层
BEGIN;
CREATE TABLE IF NOT EXISTS kg_entities (
    entity_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'public',
    entity_type TEXT NOT NULL DEFAULT 'concept',
    canonical_name TEXT NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, canonical_name)
);
CREATE TABLE IF NOT EXISTS kg_relations (
    relation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'public',
    subject_id TEXT NOT NULL REFERENCES kg_entities(entity_id) ON DELETE CASCADE,
    predicate TEXT NOT NULL,
    object_id TEXT NOT NULL REFERENCES kg_entities(entity_id) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, subject_id, predicate, object_id)
);
CREATE TABLE IF NOT EXISTS kg_evidence (
    evidence_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'public',
    relation_id TEXT NOT NULL REFERENCES kg_relations(relation_id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES rag_chunks(chunk_id) ON DELETE CASCADE,
    parent_id TEXT,
    doc_id TEXT,
    quote TEXT NOT NULL DEFAULT '',
    UNIQUE (relation_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_entities_tenant_name ON kg_entities(tenant_id, canonical_name);
CREATE INDEX IF NOT EXISTS idx_kg_entities_aliases ON kg_entities USING GIN(aliases);
CREATE INDEX IF NOT EXISTS idx_kg_relations_subject ON kg_relations(tenant_id, subject_id);
CREATE INDEX IF NOT EXISTS idx_kg_relations_object ON kg_relations(tenant_id, object_id);
CREATE INDEX IF NOT EXISTS idx_kg_evidence_chunk ON kg_evidence(chunk_id);
COMMIT;
