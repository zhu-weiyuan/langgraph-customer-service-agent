"""Backfill the current Markdown knowledge base into PostgreSQL + pgvector.

Usage (after PostgreSQL is healthy):
  PGVECTOR_ENABLED=1 DATABASE_URL=... PGVECTOR_DIM=<actual-dimension> \
  python scripts/migrate_knowledge_to_pgvector.py

The command is idempotent: chunk_key is content-addressed and existing chunks
are updated. It does not delete SQLite data or the in-memory fallback.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import vector_rag
from agent.pgvector_store import enabled, upsert_chunks


def main() -> int:
    if not enabled():
        print("Set PGVECTOR_ENABLED=1 before running this migration.")
        return 2
    sections = vector_rag._load_knowledge_base()
    chunks = []
    for index, section in enumerate(sections, start=1):
        text = f"{section['title']} {section['text']}"
        print(f"[{index}/{len(sections)}] embedding {section['title'][:60]}")
        embedding = vector_rag._get_embedding(text)
        if embedding is None:
            print("Embedding failed; aborting without claiming migration success.")
            return 1
        chunks.append({**section, "embedding": embedding})
    count = upsert_chunks(chunks)
    print(f"Upserted {count} knowledge chunks into pgvector.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
