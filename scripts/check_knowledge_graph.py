#!/usr/bin/env python3
"""Check PostgreSQL Graph-RAG integrity without modifying data.

The checker validates only structural invariants that can be verified from the
three graph tables and ``rag_chunks``.  It never treats an unavailable database
as a clean graph: the report status is ``skipped`` or ``unavailable`` instead.

Usage:
    python scripts/check_knowledge_graph.py
    python scripts/check_knowledge_graph.py --output eval/reports/knowledge_graph_quality_latest.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _row_value(row: Any, key: str, default: Any = 0) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _issue(code: str, severity: str, count: int, description: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "count": int(count), "description": description}


def check_graph(conn: Any) -> dict[str, Any]:
    """Run read-only integrity queries on an open psycopg connection."""
    required = ["kg_entities", "kg_relations", "kg_evidence", "rag_chunks"]
    missing = []
    for table in required:
        row = conn.execute("SELECT to_regclass(%s) AS name", (f"public.{table}",)).fetchone()
        if not row or not _row_value(row, "name"):
            missing.append(table)
    if missing:
        return {
            "status": "skipped",
            "reason": "missing_tables",
            "missing_tables": missing,
            "issues": [],
            "counts": {},
        }

    counts = {}
    for table in required[:3]:
        row = conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()
        counts[table] = int(_row_value(row, "count", 0))
    row = conn.execute("SELECT count(*) AS count FROM rag_chunks WHERE is_parent = false").fetchone()
    counts["rag_child_chunks"] = int(_row_value(row, "count", 0))

    checks = [
        ("empty_entity_name", "high", "实体 canonical_name 为空", """
            SELECT count(*) AS count FROM kg_entities
            WHERE btrim(coalesce(canonical_name, '')) = ''
        """),
        ("empty_relation_predicate", "high", "关系 predicate 为空", """
            SELECT count(*) AS count FROM kg_relations
            WHERE btrim(coalesce(predicate, '')) = ''
        """),
        ("relation_without_evidence", "medium", "关系没有任何可追溯证据", """
            SELECT count(*) AS count FROM kg_relations r
            WHERE NOT EXISTS (SELECT 1 FROM kg_evidence e WHERE e.relation_id = r.relation_id)
        """),
        ("evidence_missing_chunk", "critical", "证据指向不存在的 RAG chunk", """
            SELECT count(*) AS count FROM kg_evidence e
            LEFT JOIN rag_chunks c ON c.chunk_id = e.chunk_id
            WHERE c.chunk_id IS NULL
        """),
        ("relation_missing_entity", "critical", "关系 subject/object 指向不存在实体", "")
    ]
    issues = []
    for code, severity, description, sql in checks:
        if not sql:
            row = conn.execute("""
                SELECT count(*) AS count
                FROM kg_relations r
                LEFT JOIN kg_entities s ON s.entity_id = r.subject_id
                LEFT JOIN kg_entities o ON o.entity_id = r.object_id
                WHERE s.entity_id IS NULL OR o.entity_id IS NULL
            """).fetchone()
        else:
            row = conn.execute(sql).fetchone()
        count = int(_row_value(row, "count", 0))
        if count:
            issues.append(_issue(code, severity, count, description))

    tenant_checks = [
        ("entity_tenant_mismatch", "critical", "关系与其实体 tenant_id 不一致", """
            SELECT count(*) AS count FROM kg_relations r
            JOIN kg_entities s ON s.entity_id = r.subject_id
            JOIN kg_entities o ON o.entity_id = r.object_id
            WHERE r.tenant_id <> s.tenant_id OR r.tenant_id <> o.tenant_id
        """),
        ("evidence_tenant_mismatch", "critical", "证据与关系 tenant_id 不一致", """
            SELECT count(*) AS count FROM kg_evidence e
            JOIN kg_relations r ON r.relation_id = e.relation_id
            WHERE e.tenant_id <> r.tenant_id
        """),
        ("evidence_chunk_tenant_mismatch", "high", "证据与 chunk tenant_id 不一致", """
            SELECT count(*) AS count FROM kg_evidence e
            JOIN rag_chunks c ON c.chunk_id = e.chunk_id
            JOIN kg_relations r ON r.relation_id = e.relation_id
            WHERE c.tenant_id IS NOT NULL AND c.tenant_id <> r.tenant_id
        """),
        ("orphan_entity", "low", "实体没有参与任何关系（可能是可接受的词条，也可能是孤儿数据）", """
            SELECT count(*) AS count FROM kg_entities e
            WHERE NOT EXISTS (SELECT 1 FROM kg_relations r
                              WHERE r.subject_id = e.entity_id OR r.object_id = e.entity_id)
        """),
        ("duplicate_alias", "low", "同一 tenant 内不同实体存在重复 alias（需要人工确认）", """
            SELECT count(*) AS count FROM (
                SELECT tenant_id, lower(alias) AS alias
                FROM kg_entities, unnest(aliases) AS alias
                WHERE btrim(coalesce(alias, '')) <> ''
                GROUP BY tenant_id, lower(alias)
                HAVING count(DISTINCT entity_id) > 1
            ) duplicates
        """),
    ]
    for code, severity, description, sql in tenant_checks:
        row = conn.execute(sql).fetchone()
        count = int(_row_value(row, "count", 0))
        if count:
            issues.append(_issue(code, severity, count, description))

    return {
        "status": "ok" if not any(i["severity"] in {"critical", "high"} for i in issues) else "issues_found",
        "counts": counts,
        "issues": issues,
        "issue_count": sum(i["count"] for i in issues),
    }


def run() -> dict[str, Any]:
    try:
        from agent.runtime_db import connection, is_postgres_available
        if not is_postgres_available():
            return {"status": "unavailable", "reason": "postgres_dsn_not_configured", "issues": [], "counts": {}}
        with connection(autocommit=True) as conn:
            result = check_graph(conn)
        return result
    except Exception as exc:
        return {"status": "unavailable", "reason": type(exc).__name__, "error": str(exc), "issues": [], "counts": {}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Graph-RAG integrity check")
    parser.add_argument("--output", type=Path, help="write JSON report to this path")
    args = parser.parse_args(argv)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), **run()}
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0 if report["status"] in {"ok", "skipped", "unavailable"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

