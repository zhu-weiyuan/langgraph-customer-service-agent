"""PostgreSQL + pgvector repository for the knowledge-base retrieval path.

It is opt-in through PGVECTOR_ENABLED=1. The in-memory exact store remains the
safe fallback until PostgreSQL has been migrated and verified in an environment.
"""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from typing import Any, Iterable


def enabled() -> bool:
    return os.getenv("PGVECTOR_ENABLED", "0") == "1"


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required when PGVECTOR_ENABLED=1")
    return url


def _dimension() -> int:
    try:
        dimension = int(os.getenv("PGVECTOR_DIM", "1024"))
    except ValueError as exc:
        raise RuntimeError("PGVECTOR_DIM must be an integer") from exc
    if not 1 <= dimension <= 16000:
        raise RuntimeError("PGVECTOR_DIM must be between 1 and 16000")
    return dimension


@contextmanager
def _connection():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary] to enable PostgreSQL + pgvector") from exc
    with psycopg.connect(_database_url()) as conn:
        yield conn


def _vector_literal(values: Iterable[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


def _chunk_key(source: str, title: str, text: str) -> str:
    return hashlib.sha256(f"{source}\n{title}\n{text}".encode("utf-8")).hexdigest()


def upsert_chunks(chunks: list[dict[str, Any]]) -> int:
    """Insert/update embedded knowledge chunks after validating vector size."""
    if not enabled():
        return 0
    dimension = _dimension()
    rows = []
    for chunk in chunks:
        embedding = chunk["embedding"]
        if len(embedding) != dimension:
            raise RuntimeError(
                f"Embedding dimension {len(embedding)} does not match PGVECTOR_DIM={dimension}. "
                "Set PGVECTOR_DIM to the Qwen endpoint's output dimension before migrating."
            )
        rows.append((
            _chunk_key(chunk["source"], chunk["title"], chunk["text"]),
            chunk["source"], chunk["title"], chunk["text"], _vector_literal(embedding),
        ))
    if not rows:
        return 0
    with _connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO knowledge_chunks (chunk_key, source, title, content, embedding)
               VALUES (%s, %s, %s, %s, %s::vector)
               ON CONFLICT (chunk_key) DO UPDATE SET
                   source = EXCLUDED.source, title = EXCLUDED.title,
                   content = EXCLUDED.content, embedding = EXCLUDED.embedding,
                   updated_at = NOW()""",
            rows,
        )
    return len(rows)


def search(query_embedding: list[float], top_k: int = 10) -> list[dict[str, Any]]:
    """Return nearest chunks by cosine distance from pgvector's persistent HNSW index."""
    if not enabled():
        return []
    dimension = _dimension()
    if len(query_embedding) != dimension:
        raise RuntimeError(f"Query embedding dimension does not match PGVECTOR_DIM={dimension}")
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT title, content, source, 1 - (embedding <=> %s::vector) AS score
               FROM knowledge_chunks
               ORDER BY embedding <=> %s::vector
               LIMIT %s""",
            (_vector_literal(query_embedding), _vector_literal(query_embedding), max(1, min(top_k, 100))),
        )
        return [
            {"title": title, "text": content, "source": source, "score": round(float(score), 4)}
            for title, content, source, score in cur.fetchall()
        ]
