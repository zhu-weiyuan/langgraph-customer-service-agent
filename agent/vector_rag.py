# -*- coding: utf-8 -*-
"""
Vector RAG Module — ChromaDB + Embedding-based semantic retrieval.

Uses sentence-transformers for Chinese text embeddings and ChromaDB for
persistent vector storage. Designed to work alongside BM25 keyword retrieval
for hybrid search (RRF fusion in rag.py).

Model: shibing624/text2vec-base-chinese (lightweight, ~100MB)
"""

import os
from pathlib import Path
from typing import List, Dict, Optional

# ChromaDB
import chromadb
from chromadb.config import Settings

# Embedding model — lazy load to avoid startup cost
_embedding_model = None


def _get_embedding_model():
    """Lazy-load the Chinese embedding model."""
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        model_name = os.environ.get(
            "EMBEDDING_MODEL",
            "shibing624/text2vec-base-chinese"
        )
        print(f"[Vector RAG] Loading embedding model: {model_name}")
        _embedding_model = SentenceTransformer(model_name)
        print("[Vector RAG] Embedding model loaded")
    return _embedding_model


# ChromaDB persistence path
_CHROMA_DIR = Path(__file__).parent.parent / "chroma_db"
_COLLECTION_NAME = "knowledge_base"


def _get_collection():
    """Get or create the ChromaDB collection."""
    chroma_client = chromadb.PersistentClient(
        path=str(_CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )
    return chroma_client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},  # cosine similarity for Chinese
    )


def _parse_markdown_to_chunks(md_path: Path) -> List[Dict[str, str]]:
    """Parse a markdown file into searchable chunks."""
    text = md_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    chunks = []
    current_heading = md_path.stem
    current_lines = []

    for line in lines:
        if line.startswith("#"):
            if current_lines:
                chunks.append({
                    "title": current_heading,
                    "text": "\n".join(current_lines).strip(),
                    "source": md_path.stem,
                })
            heading_text = line.lstrip("# ").strip()
            if heading_text:
                current_heading = heading_text
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        chunks.append({
            "title": current_heading,
            "text": "\n".join(current_lines).strip(),
            "source": md_path.stem,
        })

    return chunks


def _build_chunk_id(title: str, source: str, index: int) -> str:
    """Build a unique ID for a chunk."""
    return f"{source}::{title.replace(' ', '_')}::{index}"


def index_knowledge_base(force_reindex: bool = False) -> int:
    """Index all knowledge base documents into ChromaDB.

    Args:
        force_reindex: If True, delete existing collection and rebuild.

    Returns:
        Number of chunks indexed.
    """
    kb_dir = Path(__file__).parent.parent / "knowledge"
    if not kb_dir.exists():
        print(f"[Vector RAG] Knowledge base not found: {kb_dir}")
        return 0

    collection = _get_collection()

    # Check if already indexed
    if not force_reindex and collection.count() > 0:
        print(f"[Vector RAG] Already indexed {collection.count()} chunks, skipping")
        return collection.count()

    if force_reindex:
        # Delete and recreate
        chroma_client = chromadb.PersistentClient(
            path=str(_CHROMA_DIR),
            settings=Settings(anonymized_telemetry=False),
        )
        try:
            chroma_client.delete_collection(_COLLECTION_NAME)
        except Exception:
            pass
        collection = chroma_client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    model = _get_embedding_model()
    all_ids = []
    all_embeddings = []
    all_documents = []
    all_metadatas = []
    chunk_count = 0

    for md_file in sorted(kb_dir.glob("*.md")):
        chunks = _parse_markdown_to_chunks(md_file)
        for i, chunk in enumerate(chunks):
            if not chunk["text"].strip():
                continue
            # Combine title + text for embedding
            embed_text = f"{chunk['title']} {chunk['text']}"
            all_ids.append(_build_chunk_id(chunk["title"], chunk["source"], i))
            all_embeddings.append(model.encode(embed_text).tolist())
            all_documents.append(chunk["text"])
            all_metadatas.append({
                "title": chunk["title"],
                "source": chunk["source"],
            })
            chunk_count += 1

    if all_ids:
        collection.add(
            ids=all_ids,
            embeddings=all_embeddings,
            documents=all_documents,
            metadatas=all_metadatas,
        )

    print(f"[Vector RAG] Indexed {chunk_count} chunks from {kb_dir}")
    return chunk_count


def vector_retrieve(query: str, top_k: int = 3) -> List[Dict[str, any]]:
    """Retrieve relevant sections using vector similarity search.

    Args:
        query: Search query string
        top_k: Number of results to return

    Returns:
        List of {"title", "text", "score", "source"} dicts sorted by relevance.
    """
    collection = _get_collection()
    if collection.count() == 0:
        # Auto-index if empty
        index_knowledge_base()
        collection = _get_collection()
        if collection.count() == 0:
            return []

    model = _get_embedding_model()
    query_embedding = model.encode(query).tolist()

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, collection.count()),
        include=["documents", "metadatas"],
    )

    output = []
    if results["distances"] and results["distances"][0]:
        for i, (distance, metadata) in enumerate(
            zip(results["distances"][0], results["metadatas"][0])
        ):
            # ChromaDB returns distance; convert to similarity score
            score = round(1 - distance, 4)  # cosine distance → similarity
            output.append({
                "title": metadata.get("title", ""),
                "text": results["documents"][0][i],
                "score": score,
                "source": metadata.get("source", ""),
            })

    return output


def rebuild_index():
    """Force rebuild the vector index."""
    return index_knowledge_base(force_reindex=True)
