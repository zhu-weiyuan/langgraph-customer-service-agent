-- Keep PostgreSQL user memories compatible with the superseding/active-memory queries.
-- Idempotent so it is safe to apply during startup and during deployment.
ALTER TABLE user_memories
    ADD COLUMN IF NOT EXISTS superseded_at DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_um_active_kind
    ON user_memories (user_id, tenant_id, kind)
    WHERE is_deleted = FALSE AND superseded_at IS NULL;
