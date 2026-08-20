"""Unit tests for the opt-in pgvector repository contract."""
import importlib


def _module(monkeypatch, enabled="1", dimension="3"):
    monkeypatch.setenv("PGVECTOR_ENABLED", enabled)
    monkeypatch.setenv("PGVECTOR_DIM", dimension)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/test")
    import agent.pgvector_store as store
    return importlib.reload(store)


def test_pgvector_disabled_is_a_noop(monkeypatch):
    store = _module(monkeypatch, enabled="0")
    assert store.upsert_chunks([]) == 0
    assert store.search([1.0, 2.0, 3.0]) == []


def test_pgvector_validates_embedding_dimension_before_connecting(monkeypatch):
    store = _module(monkeypatch)
    try:
        store.upsert_chunks([{"source": "kb", "title": "t", "text": "x", "embedding": [1.0, 2.0]}])
    except RuntimeError as exc:
        assert "does not match PGVECTOR_DIM=3" in str(exc)
    else:
        raise AssertionError("dimension mismatch must fail before database access")


def test_vector_literal_is_stable(monkeypatch):
    store = _module(monkeypatch)
    assert store._vector_literal([1, 2.5, -3]) == "[1.0,2.5,-3.0]"
