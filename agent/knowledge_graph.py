"""Small, conservative Graph-RAG augmentation layer.

The graph never invents facts: every returned hit is joined to a source RAG
chunk.  Extraction is intentionally rule-based and runs at ingest time; online
requests only resolve aliases and expand one or two hops in PostgreSQL.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger("agent.knowledge_graph")

ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "空调": ("空调", "冷气", "空调设备"),
    "音箱": ("音箱", "扬声器", "智能音箱"),
    "网关": ("网关",),
    "智能插座": ("智能插座", "插座"),
    "智能灯": ("智能灯", "灯泡", "灯具"),
    "wifi": ("wifi", "wi-fi", "无线网络", "无线连接"),
    "蓝牙": ("蓝牙",),
    "发票": ("发票", "开票"),
    "退款": ("退款", "退货", "退换货"),
    "保修": ("保修", "维修", "售后"),
    "AGV": ("agv", "无人搬运车", "搬运车"),
    "电量": ("电量", "电池", "电池电量"),
    "错误码": ("错误码", "故障码"),
}


def normalize_entity(value: str) -> str:
    value = re.sub(r"\s+", "", str(value or "")).lower()
    for canonical, aliases in ENTITY_ALIASES.items():
        if value == canonical.lower() or value in {a.lower() for a in aliases}:
            return canonical
    return value


def extract_entities(text: str) -> list[str]:
    """Resolve known aliases in stable longest-first order."""
    raw = str(text or "")
    found: list[str] = []
    for canonical, aliases in sorted(ENTITY_ALIASES.items(), key=lambda item: -max(map(len, item[1]))):
        if any(alias.lower() in raw.lower() for alias in aliases):
            found.append(canonical)
    return found


def entity_id(tenant_id: str, canonical_name: str) -> str:
    """Return a deterministic entity key scoped to one tenant."""
    raw = f"{tenant_id}\0{canonical_name}".encode("utf-8")
    return "ent-" + hashlib.sha256(raw).hexdigest()[:32]


def relation_id(tenant_id: str, subject: str, predicate: str, object_: str) -> str:
    raw = f"{tenant_id}\0{subject}\0{predicate}\0{object_}".encode("utf-8")
    return "rel-" + hashlib.sha256(raw).hexdigest()[:32]


def entity_aliases(canonical_name: str) -> list[str]:
    """Return normalized, stable aliases kept with a graph entity."""
    aliases = ENTITY_ALIASES.get(canonical_name, (canonical_name,))
    return list(dict.fromkeys([canonical_name, *aliases]))


def build_document_graph_records(
    children: Iterable[Mapping[str, Any]], *, tenant_id: str = "public", doc_id: str = ""
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Extract deterministic graph records from already-created child chunks.

    This is intentionally pure: ingestion tests can verify graph extraction without
    a running PostgreSQL instance.  Only child chunks become evidence, so every
    online graph hit remains traceable to a real retrieval chunk.
    """
    scoped_tenant = str(tenant_id or "public")
    entities: dict[str, dict[str, Any]] = {}
    relations: list[dict[str, Any]] = []
    for child in children:
        text = str(child.get("text") or child.get("content") or "")
        chunk_id = str(child.get("child_id") or child.get("chunk_id") or child.get("id") or "")
        if not text.strip() or not chunk_id:
            continue
        parent_id = str(child.get("parent_id") or "")
        child_doc_id = str(child.get("doc_id") or doc_id or "")
        for name in extract_entities(text):
            entities[name] = {
                "entity_id": entity_id(scoped_tenant, name),
                "tenant_id": scoped_tenant,
                "canonical_name": name,
                "aliases": entity_aliases(name),
            }
        relations.extend(extract_relations(
            text,
            tenant_id=scoped_tenant,
            chunk_id=chunk_id,
            parent_id=parent_id,
            doc_id=child_doc_id,
        ))
    return entities, relations


def sync_document_graph(cur: Any, *, doc_id: str, children: Iterable[Mapping[str, Any]],
                        tenant_id: str = "public") -> dict[str, int]:
    """Replace one document's graph evidence while preserving shared facts.

    ``rag_chunks`` are rebuilt before this function is called.  The child IDs in
    ``children`` therefore refer to the current index, and foreign keys prevent
    graph evidence from ever pointing at deleted or invented content.
    """
    scoped_tenant = str(tenant_id or "public")
    child_rows = list(children)
    entities, relations = build_document_graph_records(
        child_rows, tenant_id=scoped_tenant, doc_id=doc_id)

    # Deleting only the document evidence is safe: a relation can be supported by
    # multiple documents and must survive when another source still cites it.
    cur.execute("DELETE FROM kg_evidence WHERE tenant_id = %s AND doc_id = %s",
                (scoped_tenant, doc_id))

    for entity in entities.values():
        cur.execute(
            """
            INSERT INTO kg_entities (entity_id, tenant_id, entity_type, canonical_name, aliases)
            VALUES (%s, %s, 'concept', %s, %s)
            ON CONFLICT (tenant_id, canonical_name) DO UPDATE SET
                aliases = EXCLUDED.aliases
            """,
            (entity["entity_id"], scoped_tenant, entity["canonical_name"], entity["aliases"]),
        )

    for relation in relations:
        subject = normalize_entity(str(relation["subject"]))
        object_ = normalize_entity(str(relation["object"]))
        cur.execute(
            """
            INSERT INTO kg_relations
                (relation_id, tenant_id, subject_id, predicate, object_id, confidence)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, subject_id, predicate, object_id) DO UPDATE SET
                confidence = GREATEST(kg_relations.confidence, EXCLUDED.confidence)
            """,
            (relation["relation_id"], scoped_tenant,
             entity_id(scoped_tenant, subject), relation["predicate"],
             entity_id(scoped_tenant, object_), relation["confidence"]),
        )
        cur.execute(
            """
            INSERT INTO kg_evidence
                (tenant_id, relation_id, chunk_id, parent_id, doc_id, quote)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (relation_id, chunk_id) DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id, parent_id = EXCLUDED.parent_id,
                doc_id = EXCLUDED.doc_id, quote = EXCLUDED.quote
            """,
            (scoped_tenant, relation["relation_id"], relation["chunk_id"],
             relation["parent_id"] or None, relation["doc_id"] or doc_id,
             relation["quote"]),
        )

    # Avoid unbounded stale graph records after a KB document is rebuilt or
    # removed.  Relations/entries still referenced by any document are retained.
    cur.execute(
        "DELETE FROM kg_relations r WHERE r.tenant_id = %s "
        "AND NOT EXISTS (SELECT 1 FROM kg_evidence e WHERE e.relation_id = r.relation_id)",
        (scoped_tenant,),
    )
    cur.execute(
        "DELETE FROM kg_entities e WHERE e.tenant_id = %s "
        "AND NOT EXISTS (SELECT 1 FROM kg_relations r "
        "WHERE r.subject_id = e.entity_id OR r.object_id = e.entity_id)",
        (scoped_tenant,),
    )
    return {"entities": len(entities), "relations": len(relations), "evidence": len(relations)}


def extract_relations(text: str, *, tenant_id: str = "public", chunk_id: str = "", parent_id: str = "", doc_id: str = "") -> list[dict[str, Any]]:
    """Extract only high-precision co-occurrence relations from one chunk."""
    entities = extract_entities(text)
    if len(entities) < 2:
        return []
    lowered = str(text or "").lower()
    predicates = [("supports", ("支持", "可以", "能够", "兼容")),
                  ("requires", ("需要", "前提", "必须")),
                  ("troubleshoots", ("排查", "解决", "处理", "故障")),
                  ("related_to", ("相关", "包括", "以及"))]
    predicate = "related_to"
    for candidate, hints in predicates:
        if any(h in lowered for h in hints):
            predicate = candidate
            break
    out = []
    for subject, object_ in zip(entities, entities[1:]):
        rid = relation_id(tenant_id, subject, predicate, object_)
        out.append({"relation_id": rid, "tenant_id": tenant_id,
                    "subject": subject, "predicate": predicate, "object": object_,
                    "chunk_id": chunk_id, "parent_id": parent_id, "doc_id": doc_id,
                    "quote": str(text or "")[:800], "confidence": 0.75 if predicate != "related_to" else 0.55})
    return out


def merge_evidence(base_hits: Sequence[Mapping[str, Any]], graph_hits: Sequence[Mapping[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    """Merge and deduplicate by source chunk, preserving graph provenance."""
    merged: dict[str, dict[str, Any]] = {}
    for item in list(base_hits or []) + list(graph_hits or []):
        hit = dict(item)
        key = str(hit.get("id") or hit.get("chunk_id") or hit.get("source") or hashlib.sha1(str(hit).encode()).hexdigest())
        if key not in merged:
            merged[key] = hit
        else:
            current = merged[key]
            current["score"] = max(float(current.get("score") or 0), float(hit.get("score") or 0))
            if hit.get("graph_evidence"):
                current["graph_evidence"] = hit["graph_evidence"]
            current["text"] = current.get("text") or hit.get("text") or hit.get("content", "")
    result = list(merged.values())
    result.sort(key=lambda x: float(x.get("rerank_score", x.get("score", 0)) or 0), reverse=True)
    return result[: max(1, int(limit))]


class PostgresKnowledgeGraph:
    """Read-only online graph expansion; unavailable DB cleanly returns []."""
    def expand(self, query: str, *, tenant_id: str = "default", max_hops: int = 2, limit: int = 8) -> list[dict[str, Any]]:
        entities = extract_entities(query)
        if not entities:
            return []
        try:
            from .runtime_db import connection
            with connection() as conn:
                rows = conn.execute(
                    """
                    WITH RECURSIVE seed AS (
                      SELECT entity_id, tenant_id, canonical_name
                      FROM kg_entities
                      WHERE tenant_id IN ('public', %s)
                        AND (canonical_name = ANY(%s) OR aliases && %s)
                    ), walk(entity_id, tenant_id, depth) AS (
                      SELECT entity_id, tenant_id, 0 FROM seed
                      UNION
                      SELECT
                        CASE WHEN r.subject_id = w.entity_id THEN r.object_id ELSE r.subject_id END,
                        w.tenant_id,
                        w.depth + 1
                      FROM walk w
                      JOIN kg_relations r
                        ON r.tenant_id = w.tenant_id
                       AND (r.subject_id = w.entity_id OR r.object_id = w.entity_id)
                      WHERE w.depth < %s
                    )
                    SELECT DISTINCT c.chunk_id AS chunk_id, c.parent_id AS parent_id,
                           c.doc_id AS doc_id, c.title AS title, c.source AS source,
                           c.section AS section, c.content AS content,
                           r.relation_id AS relation_id,
                           se.canonical_name AS subject_name,
                           oe.canonical_name AS object_name,
                           r.predicate AS predicate, e.quote AS quote
                    FROM walk w
                    JOIN kg_entities se
                      ON se.entity_id = w.entity_id
                     AND se.tenant_id = w.tenant_id
                    JOIN kg_relations r
                      ON r.tenant_id = w.tenant_id
                     AND (r.subject_id = w.entity_id OR r.object_id = w.entity_id)
                    JOIN kg_entities oe
                      ON oe.entity_id = CASE WHEN r.subject_id = w.entity_id THEN r.object_id ELSE r.subject_id END
                     AND oe.tenant_id = r.tenant_id
                    JOIN kg_evidence e
                      ON e.relation_id = r.relation_id
                     AND e.tenant_id = r.tenant_id
                    JOIN rag_chunks c
                      ON c.chunk_id = e.chunk_id
                    WHERE (c.tenant_id IS NULL OR c.tenant_id = r.tenant_id)
                      AND (c.tenant_id IS NULL OR c.tenant_id IN ('public', %s))
                    LIMIT %s
                    """,
                    (tenant_id, entities, entities, max(1, min(2, max_hops)), tenant_id, limit),
                ).fetchall()
            return _rows_to_hits(rows)
        except Exception:
            logger.info("Graph-RAG unavailable; using hybrid RAG fallback", exc_info=True)
            return []


def _rows_to_hits(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize dict rows from ``runtime_db`` into grounded graph RAG hits.

    ``runtime_db.connection`` deliberately uses psycopg's ``dict_row`` factory.
    Keeping this conversion separate makes that contract explicit and prevents a
    database error from being swallowed as a misleading "no graph evidence".
    """
    hits: list[dict[str, Any]] = []
    for row in rows:
        content = str(row.get("content") or "")
        if not content:
            continue
        hits.append({
            "id": row.get("chunk_id"),
            "parent_id": row.get("parent_id"),
            "doc_id": row.get("doc_id"),
            "title": row.get("title") or "",
            "source": row.get("source") or "",
            "section": row.get("section") or "",
            "content": content,
            "text": content,
            # Graph traversal augments, rather than replaces, normal hybrid
            # retrieval.  Its lower prior score prevents it from crowding out
            # directly relevant vector/keyword hits.
            "score": 0.35,
            "graph_evidence": {
                "relation_id": row.get("relation_id"),
                "subject": row.get("subject_name") or "",
                "object": row.get("object_name") or "",
                "predicate": row.get("predicate") or "related_to",
                "quote": row.get("quote") or "",
            },
        })
    return hits


def augment_hits(query: str, base_hits: Sequence[Mapping[str, Any]], *, tenant_id: str = "default", store: Any = None, limit: int = 8) -> list[dict[str, Any]]:
    store = store or PostgresKnowledgeGraph()
    graph_hits = store.expand(query, tenant_id=tenant_id, max_hops=2, limit=limit)
    return merge_evidence(base_hits, graph_hits, limit=limit)
