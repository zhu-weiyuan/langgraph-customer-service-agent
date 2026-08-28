from __future__ import annotations

from contextlib import contextmanager

from agent.knowledge_graph import PostgresKnowledgeGraph


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.sql = ""
        self.params = None

    def execute(self, sql, params):
        self.sql = sql
        self.params = params
        if self.error:
            raise self.error
        return _Result(self.rows)


@contextmanager
def _connection(conn):
    yield conn


def _hit(chunk_id="c1", tenant="tenant-a"):
    return {
        "chunk_id": chunk_id,
        "parent_id": "p1",
        "doc_id": "d1",
        "title": "manual",
        "source": "manual",
        "section": "sec",
        "content": "AGV 内容",
        "relation_id": "rel-1",
        "subject_name": "AGV",
        "object_name": "电量",
        "predicate": "has_status",
        "quote": "AGV 电量",
        "tenant_id": tenant,
    }


def test_expand_enforces_same_tenant_across_graph_and_evidence(monkeypatch):
    conn = _Conn([_hit()])
    monkeypatch.setattr("agent.runtime_db.connection", lambda: _connection(conn))

    hits = PostgresKnowledgeGraph().expand("AGV 电量", tenant_id="tenant-a")

    assert hits and hits[0]["id"] == "c1"
    assert "r.tenant_id = w.tenant_id" in conn.sql
    assert "se.tenant_id = w.tenant_id" in conn.sql
    assert "oe.tenant_id = r.tenant_id" in conn.sql
    assert "e.tenant_id = r.tenant_id" in conn.sql
    assert "c.tenant_id = r.tenant_id" in conn.sql
    assert conn.params == ("tenant-a", ["AGV", "电量"], ["AGV", "电量"], 2, "tenant-a", 8)


def test_expand_allows_public_and_current_tenant_scope(monkeypatch):
    conn = _Conn([])
    monkeypatch.setattr("agent.runtime_db.connection", lambda: _connection(conn))

    PostgresKnowledgeGraph().expand("AGV", tenant_id="tenant-a", max_hops=1, limit=3)

    assert "tenant_id IN ('public', %s)" in conn.sql
    assert "c.tenant_id IN ('public', %s)" in conn.sql
    assert conn.params[-2:] == ("tenant-a", 3)


def test_expand_fails_closed_when_database_query_fails(monkeypatch):
    conn = _Conn(error=RuntimeError("db unavailable"))
    monkeypatch.setattr("agent.runtime_db.connection", lambda: _connection(conn))

    assert PostgresKnowledgeGraph().expand("AGV", tenant_id="tenant-a") == []
