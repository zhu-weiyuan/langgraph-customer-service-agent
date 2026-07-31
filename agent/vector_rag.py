# -*- coding: utf-8 -*-
"""
Real Vector RAG — 真正的语义向量检索

使用 OpenRouter 免费 embedding API（nvidia/llama-nemotron-embed-vl-1b-v2）
+ FAISS 本地向量数据库

零 GPU 需求，纯 API 调用。
"""

import json
import math
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ── OpenRouter Embedding API ────────────────────────────────

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
EMBEDDING_MODEL = "nvidia/llama-nemotron-embed-vl-1b-v2:free"

# Read API key from environment or use default
OPENROUTER_API_KEY = os.environ.get(
    "OPENROUTER_API_KEY",
    ""  # Will be set from OpenClaw config at runtime
)


def _get_api_key() -> str:
    """Get API key: env（运行时读取，兼容 load_dotenv 晚于模块导入）→ OpenClaw config.

    修复：旧版只认模块导入时快照的 OPENROUTER_API_KEY；用户在 .env 里配的是
    OPENAI_API_KEY → 请求带着空 Bearer 打出 401。现在运行时兜底读取两者。
    """
    env_key = (os.environ.get("OPENROUTER_API_KEY", "").strip()
               or os.environ.get("OPENAI_API_KEY", "").strip())
    if env_key:
        return env_key
    if OPENROUTER_API_KEY:
        return OPENROUTER_API_KEY

    # Try to read from OpenClaw config
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            # Navigate to the embedding API key
            agents_defaults = config.get("agents", {}).get("defaults", {})
            memory_search = agents_defaults.get("memorySearch", {})
            remote = memory_search.get("remote", {})
            api_key = remote.get("apiKey", "")

            if api_key:
                return api_key
        except Exception as e:
            print(f"[Vector RAG] Error reading config: {e}")

    raise ValueError(
        "OpenRouter API key not found. "
        "Set OPENROUTER_API_KEY environment variable or configure in OpenClaw."
    )


def _get_embedding(text: str, max_retries: int = 3) -> Optional[List[float]]:
    """Get embedding vector from OpenRouter API.

    Args:
        text: Text to embed
        max_retries: Number of retry attempts on failure

    Returns:
        Embedding vector (list of floats) or None if all retries fail
    """
    # 优先走统一 EmbeddingClient（OPENAI_API_KEY/OPENAI_BASE_URL/EMBEDDING_MODEL，
    # 带 Authorization 头 + 批量/重试）；未配置或失败则继续原 OpenRouter 路径。
    try:
        from .embedding_client import EmbeddingClient
        _client = EmbeddingClient.from_env(strict=False)
    except Exception:
        _client = None
    if _client is not None:
        try:
            vec = _client.embed_one(text)
            if vec:
                return vec
        except Exception as e:
            print(f"[Vector RAG] EmbeddingClient failed, trying OpenRouter: {e}")

    import requests  # lazy import

    for attempt in range(max_retries):
        try:
            response = requests.post(
                f"{OPENROUTER_BASE_URL}/embeddings",
                headers={
                    "Authorization": f"Bearer {_get_api_key()}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "langgraph-customer-service-agent",
                    "X-Title": "Customer Service RAG",
                },
                json={
                    "model": EMBEDDING_MODEL,
                    "input": text[:8192],  # Truncate to max length
                },
                timeout=30,
            )

            if response.status_code == 200:
                data = response.json()
                embedding = data["data"][0]["embedding"]
                return embedding
            else:
                print(f"[Vector RAG] API error {response.status_code}: {response.text[:100]}")

        except Exception as e:
            print(f"[Vector RAG] Request failed (attempt {attempt + 1}): {e}")

    return None


# ── In-memory FAISS-like vector store ───────────────────────

class SimpleVectorStore:
    """Simple in-memory vector store with cosine similarity search.

    No external dependencies needed — pure Python implementation.
    """

    def __init__(self):
        self.vectors = []       # List of embedding vectors
        self.metadata = []      # List of metadata dicts (title, source, text)
        self._built = False

    def add(self, vector: List[float], metadata: Dict) -> None:
        """Add a vector with metadata."""
        self.vectors.append(vector)
        self.metadata.append(metadata)
        self._built = False

    def search(self, query_vector: List[float], top_k: int = 5) -> List[Dict]:
        """Search for most similar vectors using cosine similarity.

        Args:
            query_vector: Query embedding vector
            top_k: Number of results to return

        Returns:
            List of {"title", "text", "score", "source"} dicts sorted by relevance.
        """
        if not self.vectors:
            return []

        # Compute cosine similarity with all vectors
        scores = []
        for i, vector in enumerate(self.vectors):
            sim = _cosine_similarity(query_vector, vector)
            if sim > 0:
                scores.append((sim, i))

        # Sort by score descending
        scores.sort(key=lambda x: x[0], reverse=True)

        # Return top_k results with metadata
        results = []
        for sim, idx in scores[:top_k]:
            meta = self.metadata[idx]
            results.append({
                "title": meta["title"],
                "text": meta["text"],
                "score": round(sim, 4),
                "source": meta["source"],
            })

        return results


# ── Global state ───────────────────────────────────────────────

_vector_store = SimpleVectorStore()
_indexed = False
KB_DIR = Path(__file__).parent.parent / "knowledge"


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(vec_a) != len(vec_b):
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot_product / (norm_a * norm_b)


def _load_knowledge_base() -> List[Dict]:
    """Load and parse knowledge base markdown files."""
    global _indexed

    if _indexed:
        return []  # Already indexed

    if not KB_DIR.exists():
        print(f"[Vector RAG] Knowledge base not found: {KB_DIR}")
        return []

    all_sections = []

    for md_file in sorted(KB_DIR.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
            sections = _parse_markdown(text, md_file.stem)
            for s in sections:
                s["source"] = md_file.stem
            all_sections.extend(sections)
        except Exception as e:
            print(f"[Vector RAG] Error loading {md_file}: {e}")

    return all_sections


def _parse_markdown(text: str, source: str) -> List[Dict]:
    """Parse markdown into sections."""
    import re
    sections = []
    lines = text.split("\n")
    current_heading = source
    current_content = []

    for line in lines:
        if line.startswith("#"):
            if current_content:
                sections.append({
                    "title": current_heading,
                    "text": "\n".join(current_content).strip(),
                })
            heading_text = line.lstrip("# ").strip()
            if heading_text:
                current_heading = heading_text
            current_content = []
        else:
            current_content.append(line)

    if current_content:
        sections.append({
            "title": current_heading,
            "text": "\n".join(current_content).strip(),
        })

    return sections


def build_index():
    """Build vector index from knowledge base.

    This is a one-time operation that embeds all knowledge base sections
    and stores them in memory for fast similarity search.
    """
    global _indexed, _vector_store

    if _indexed:
        print("[Vector RAG] Index already built")
        return

    print("[Vector RAG] Building vector index...")
    sections = _load_knowledge_base()

    if not sections:
        print("[Vector RAG] No sections to index")
        return

    # Create new vector store
    _vector_store = SimpleVectorStore()

    # ── 磁盘缓存:避免每次进程重启后首个请求重打 64 次 embedding API ──
    import hashlib as _hl
    import json as _json
    import os as _os
    _cache_key = _hl.sha256(
        ("|".join(f"{s['title']}\x00{s['text']}" for s in sections)
         + _os.environ.get("EMBEDDING_MODEL", "")
         + _os.environ.get("EMBEDDING_DIMENSIONS", "")).encode("utf-8")
    ).hexdigest()[:16]
    _cache_path = _os.path.join("data", f"vector_index_{_cache_key}.json")
    try:
        with open(_cache_path, encoding="utf-8") as f:
            cached = _json.load(f)
        for entry in cached:
            _vector_store.add(entry["embedding"], entry["meta"])
        _indexed = True
        print(f"[Vector RAG] Index loaded from cache: {len(cached)} sections "
              f"({_cache_path})")
        return
    except FileNotFoundError:
        pass
    except Exception as exc:  # 缓存损坏则重建
        print(f"[Vector RAG] Cache unusable ({exc}); rebuilding")

    # Embed each section
    _cache_entries = []
    for i, section in enumerate(sections):
        # Combine title and text for better semantic representation
        text_to_embed = f"{section['title']} {section['text']}"

        print(f"  [{i+1}/{len(sections)}] Embedding: {section['title'][:40]}...")

        embedding = _get_embedding(text_to_embed)
        if embedding is None:
            print(f"    ⚠️  Failed to get embedding for section {i+1}")
            continue

        meta = {
            "title": section["title"],
            "text": section["text"],
            "source": section["source"],
        }
        _vector_store.add(embedding, meta)
        _cache_entries.append({"embedding": list(embedding), "meta": meta})

    _indexed = True
    print(f"[Vector RAG] Index built: {_vector_store.vectors.__len__()} sections indexed")
    try:
        _os.makedirs("data", exist_ok=True)
        with open(_cache_path, "w", encoding="utf-8") as f:
            _json.dump(_cache_entries, f, ensure_ascii=False)
        print(f"[Vector RAG] Index cached to {_cache_path}")
    except Exception as exc:
        print(f"[Vector RAG] Cache write failed (non-fatal): {exc}")


def vector_retrieve(query: str, top_k: int = 3) -> List[Dict]:
    """Retrieve relevant sections using semantic similarity.

    Args:
        query: Search query string
        top_k: Number of results to return

    Returns:
        List of {"title", "text", "score", "source"} dicts sorted by relevance.
    """
    if not _indexed:
        build_index()

    # Get query embedding
    print(f"[Vector RAG] Embedding query: {query[:50]}...")
    query_embedding = _get_embedding(query)

    if query_embedding is None:
        print("[Vector RAG] Failed to get query embedding, returning empty results")
        return []

    # Search for similar sections
    results = _vector_store.search(query_embedding, top_k=top_k * 2)

    return results[:top_k]


def rebuild_index():
    """Force rebuild the vector index."""
    global _indexed
    _indexed = False
    return build_index()


if __name__ == "__main__":
    # Quick test
    print("Testing real vector retrieval with OpenRouter embedding API...")

    # Build index
    build_index()

    # Test queries
    test_queries = [
        "设备连不上WiFi怎么办",
        "保修期多久",
        "怎么退货",
    ]

    for query in test_queries:
        print(f"\nQuery: {query}")
        results = vector_retrieve(query, top_k=3)
        for i, r in enumerate(results, 1):
            print(f"  [{i}] {r['source']} - {r['title']} (score: {r['score']})")
