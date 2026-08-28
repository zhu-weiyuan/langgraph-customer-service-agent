from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_knowledge_graph.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_knowledge_graph", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConn:
    def __init__(self, rows):
        self.rows = iter(rows)

    def execute(self, *_args):
        return FakeResult(next(self.rows))


def test_unconfigured_database_is_not_reported_as_clean(monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "ROOT", ROOT)
    monkeypatch.setenv("DATABASE_URL", "")
    # run() imports the runtime helper, so directly patch its availability.
    import agent.runtime_db as runtime_db
    monkeypatch.setattr(runtime_db, "is_postgres_available", lambda: False)
    assert module.run()["status"] == "unavailable"


def test_missing_graph_tables_are_skipped():
    module = _load()
    conn = FakeConn([{"name": None}, {"name": "kg_relations"}, {"name": "kg_evidence"}, {"name": "rag_chunks"}])
    result = module.check_graph(conn)
    assert result["status"] == "skipped"
    assert "kg_entities" in result["missing_tables"]


def test_structural_issues_are_reported():
    module = _load()
    # table existence, counts, then each query count; provide one issue for
    # empty entities and zero for all remaining checks.
    rows = [
        {"name": "kg_entities"}, {"name": "kg_relations"},
        {"name": "kg_evidence"}, {"name": "rag_chunks"},
        {"count": 1}, {"count": 2}, {"count": 3}, {"count": 4}, {"count": 3},
        {"count": 1}, {"count": 0}, {"count": 0}, {"count": 0}, {"count": 0},
        {"count": 0}, {"count": 0}, {"count": 0}, {"count": 0}, {"count": 0},
    ]
    result = module.check_graph(FakeConn(rows))
    assert result["status"] == "issues_found"
    assert result["issues"][0]["code"] == "empty_entity_name"


