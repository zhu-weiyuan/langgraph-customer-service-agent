"""Remote SiliconFlow reranker with a deterministic local fallback.

The client uses the SiliconFlow/OpenAI-compatible ``/rerank`` endpoint and
keeps provider details out of the RAG backend.  A failed remote call never
breaks chat retrieval: callers receive the configured fallback ranking.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def _transport(url: str, headers: Dict[str, str], payload: Dict[str, Any],
               timeout: float) -> Tuple[int, Dict[str, Any]]:
    """POST JSON using the project's preferred HTTP client, with fallback."""
    try:
        import httpx
        response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        return response.status_code, response.json()
    except ImportError:
        pass
    import requests
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    return response.status_code, response.json()


class RemoteReranker:
    """SiliconFlow reranker implementing the shared ``Reranker`` contract."""

    def __init__(self, api_key: str, base_url: str, model: str,
                 timeout: float = 15.0, fallback: Optional[Any] = None,
                 transport: Optional[Any] = None):
        self.api_key = api_key
        self.base_url = (base_url or "https://api.siliconflow.cn/v1").rstrip("/")
        self.model = model
        self.timeout = max(0.1, float(timeout))
        self.fallback = fallback
        self.transport = transport or _transport
        self.last_error: Optional[str] = None
        self.provider = "siliconflow"

    @classmethod
    def from_env(cls, fallback: Optional[Any] = None) -> Optional["RemoteReranker"]:
        api_key = (os.getenv("RERANKER_API_KEY", "").strip()
                   or os.getenv("EMBEDDING_API_KEY", "").strip())
        if not api_key:
            return None
        base_url = (os.getenv("RERANKER_BASE_URL", "").strip()
                    or os.getenv("EMBEDDING_BASE_URL", "").strip()
                    or "https://api.siliconflow.cn/v1")
        model = os.getenv("RERANKER_MODEL", "Qwen/Qwen3-Reranker-8B").strip()
        try:
            timeout = float(os.getenv("RERANKER_TIMEOUT_SECONDS", "60"))
        except ValueError:
            timeout = 60.0
        return cls(api_key, base_url, model, timeout, fallback=fallback)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/rerank"

    def _fallback(self, query: str, results: List[Dict[str, Any]],
                  top_n: int) -> List[Dict[str, Any]]:
        if self.fallback is None:
            return results[:top_n]
        return self.fallback.rerank(query, results, top_n)

    def rerank(self, query: str, results: List[Dict[str, Any]],
               top_n: int = 8) -> List[Dict[str, Any]]:
        if not results:
            return []
        documents = [str(item.get("content") or item.get("text") or "")[:4000]
                     for item in results]
        payload = {"model": self.model, "query": query[:8192],
                   "documents": documents, "top_n": min(max(1, top_n), len(documents))}
        try:
            status, data = self.transport(
                self.endpoint,
                {"Authorization": f"Bearer {self.api_key}",
                 "Content-Type": "application/json"},
                payload, self.timeout)
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {str(data)[:200]}")
            raw = data.get("results")
            if not isinstance(raw, list):
                raise RuntimeError("response missing results[]")
            scored: List[Tuple[float, int, Dict[str, Any]]] = []
            for item in raw:
                index = int(item.get("index"))
                if index < 0 or index >= len(results):
                    continue
                score = float(item.get("relevance_score", item.get("score", 0.0)))
                entry = dict(results[index])
                entry["rerank_score"] = round(score, 6)
                entry["reranker_provider"] = self.provider
                entry["reranker_model"] = self.model
                scored.append((score, index, entry))
            if not scored:
                raise RuntimeError("response contained no valid ranked documents")
            scored.sort(key=lambda value: (-value[0], value[1]))
            self.last_error = None
            return [entry for _, _, entry in scored[:top_n]]
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("[RAG] remote reranker unavailable; using fallback: %s",
                           self.last_error)
            return self._fallback(query, results, top_n)


__all__ = ["RemoteReranker"]
