# -*- coding: utf-8 -*-
"""
EmbeddingClient — OpenAI 兼容 /embeddings 客户端（查询嵌入 + 知识库导入共用）

修复背景：vector_rag 直连 OpenRouter 且只认 OPENROUTER_API_KEY，用户配置的是
OPENAI_API_KEY/.env → 请求 401。本客户端统一走 OpenAI 兼容协议：

    POST {OPENAI_BASE_URL}/embeddings
    Authorization: Bearer {OPENAI_API_KEY}

特性：
  * 批量：每次调用 ≤ 32 条文本（batch_size 可配）
  * 超时 + 2 次重试
  * 传输层：httpx 优先 → requests 降级 → 均缺失时报出清晰错误
  * transport 可注入（stdlib 单测无需网络/三方库）
  * 纯函数 batched() / build_headers() 可独测
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_BATCH_SIZE = 32
DEFAULT_TIMEOUT = 6.0
DEFAULT_MAX_RETRIES = 0

# ── Mock 开关（应用层压测用，默认关闭）─────────────────────
# MOCK_EMBEDDING=1 时 embed() 返回确定性伪向量，不发 HTTP（否则压测时
# RAG 那一步仍会打 SiliconFlow，既花钱又把延迟绑死在外部服务上）。
try:
    from .mock_llm import (fake_embeddings, mock_embedding_enabled)
except ImportError:                      # pragma: no cover - 脚本方式运行
    from mock_llm import (fake_embeddings, mock_embedding_enabled)  # type: ignore

# transport 签名：fn(url, headers, payload, timeout) -> (status_code, response_json)
Transport = Callable[[str, Dict[str, str], Dict[str, Any], float],
                     Tuple[int, Dict[str, Any]]]


# ── 纯函数 ───────────────────────────────────────────────────

def batched(items: Sequence, size: int) -> List[List]:
    """按 size 分批（纯函数）。size<=0 视为 1。"""
    size = max(1, int(size))
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def build_headers(api_key: str) -> Dict[str, str]:
    """构造认证头（纯函数）。401 修复点：Authorization 必须携带。"""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _default_transport(url: str, headers: Dict[str, str],
                       payload: Dict[str, Any], timeout: float):
    """httpx 优先 / requests 降级 / 都缺失时报清晰错误。"""
    try:
        import httpx
        resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        return resp.status_code, resp.json()
    except ImportError:
        pass
    try:
        import requests
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        return resp.status_code, resp.json()
    except ImportError:
        raise RuntimeError(
            "No HTTP library available for EmbeddingClient: "
            "install 'httpx' or 'requests' (pip install httpx)")


# ── 客户端 ───────────────────────────────────────────────────

class EmbeddingClient:
    """OpenAI 兼容 embeddings 客户端。

    Args:
        api_key: 必填（缺失即 401 的根因）
        base_url: OpenAI 兼容网关地址（尾部 /v1，自动去尾 /）
        model: embedding 模型名
        batch_size: 单次请求最大文本数（默认 32）
        timeout: 单请求超时秒数
        max_retries: 失败重试次数（默认 2，即最多 3 次尝试）
        transport: 可注入传输层（测试用）
    """

    def __init__(self, api_key: str,
                 base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL,
                 batch_size: int = DEFAULT_BATCH_SIZE,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 transport: Optional[Transport] = None,
                 dimensions: Optional[int] = None):
        if not api_key and not mock_embedding_enabled():
            raise ValueError(
                "EmbeddingClient: missing api_key (set OPENAI_API_KEY in .env)")
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.batch_size = min(max(1, int(batch_size)), DEFAULT_BATCH_SIZE)
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.transport = transport or _default_transport
        # MRL 降维:pgvector 的 HNSW/IVFFlat 索引上限 2000 维。
        # Qwen3-Embedding 等 MRL 模型支持请求时指定输出维度(OpenAI 兼容
        # 的 dimensions 参数);设置后请求带 dimensions,并校验返回维度。
        self.dimensions = int(dimensions) if dimensions else None

    # -- construction --

    @classmethod
    def from_env(cls, strict: bool = True,
                 transport: Optional[Transport] = None) -> Optional["EmbeddingClient"]:
        """从环境变量构造。

        缺 OPENAI_API_KEY 时：strict=True 抛 ValueError（列出缺失项）；
        strict=False 返回 None（调用方自行降级）。
        """
        # Embedding 服务常与 chat LLM 分离(如本地 llama.cpp 跑 chat、
        # SiliconFlow 跑 embedding)。优先读 EMBEDDING_* 专属变量,
        # 兼容旧变量名 MY_AGENT_*(simple-agent 时期的约定),最后回退 OPENAI_*。
        api_key = (os.environ.get("EMBEDDING_API_KEY", "").strip()
                   or os.environ.get("MY_AGENT_API_KEY", "").strip()
                   or os.environ.get("OPENAI_API_KEY", "").strip())
        if not api_key and mock_embedding_enabled():
            api_key = "mock-embedding-key"      # 压测模式：无需真实凭据
        if not api_key:
            if strict:
                raise ValueError(
                    "EmbeddingClient config missing: EMBEDDING_API_KEY (或 OPENAI_API_KEY) "
                    "(optional: EMBEDDING_BASE_URL, EMBEDDING_MODEL). "
                    "Put them in .env — app/scripts load it via python-dotenv.")
            return None
        base_url = (os.environ.get("EMBEDDING_BASE_URL", "").strip()
                    or os.environ.get("MY_AGENT_BASE_URL", "").strip()
                    or os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL))
        model = (os.environ.get("EMBEDDING_MODEL", "").strip()
                 or os.environ.get("MY_AGENT_MODEL", "").strip()
                 or DEFAULT_MODEL)
        dims_raw = (os.environ.get("EMBEDDING_DIMENSIONS", "").strip()
                    or os.environ.get("PGVECTOR_DIM", "").strip())
        dimensions = int(dims_raw) if dims_raw.isdigit() else None

        def _env_float(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            try:
                value = float(raw) if raw else default
            except (TypeError, ValueError):
                value = default
            return max(0.1, value)

        def _env_int(name: str, default: int) -> int:
            raw = os.environ.get(name, "").strip()
            try:
                value = int(raw) if raw else default
            except (TypeError, ValueError):
                value = default
            return max(0, value)

        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            transport=transport,
            dimensions=dimensions,
            timeout=_env_float("EMBEDDING_TIMEOUT_SECONDS", DEFAULT_TIMEOUT),
            max_retries=_env_int("EMBEDDING_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        )

    # -- API --

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/embeddings"

    def headers(self) -> Dict[str, str]:
        return build_headers(self.api_key)

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """批量嵌入，保持输入顺序。任一批次重试耗尽 → RuntimeError。"""
        # MOCK_EMBEDDING=1：确定性伪向量，不发 HTTP
        if mock_embedding_enabled():
            raw_dim = os.getenv("MOCK_EMBEDDING_DIM", "").strip()
            dim = int(raw_dim) if raw_dim.isdigit() else (self.dimensions or 1024)
            return fake_embeddings(list(texts), dim=dim)

        out: List[List[float]] = []
        for batch in batched(list(texts), self.batch_size):
            out.extend(self._embed_batch(batch))
        return out

    def embed_one(self, text: str) -> List[float]:
        vecs = self.embed([text])
        return vecs[0] if vecs else []

    # -- internal --

    def _embed_batch(self, batch: List[str]) -> List[List[float]]:
        if not batch:
            return []
        payload = {"model": self.model,
                   "input": [t[:8192] for t in batch]}
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        last_err: Any = None
        for attempt in range(self.max_retries + 1):
            try:
                status, data = self.transport(
                    self.endpoint, self.headers(), payload, self.timeout)
                if status == 200:
                    items = sorted(data.get("data", []),
                                   key=lambda d: d.get("index", 0))
                    vectors = [d["embedding"] for d in items]
                    if len(vectors) != len(batch):
                        raise RuntimeError(
                            f"embeddings count mismatch: sent {len(batch)}, "
                            f"got {len(vectors)}")
                    if self.dimensions and vectors and \
                            len(vectors[0]) != self.dimensions:
                        raise RuntimeError(
                            f"embedding dim mismatch: requested {self.dimensions}, "
                            f"got {len(vectors[0])} — 该 embedding 服务可能不支持 "
                            f"dimensions 参数(MRL 降维),请换支持的模型或调整 "
                            f"PGVECTOR_DIM 与建表维度一致")
                    return vectors
                last_err = f"HTTP {status}: {str(data)[:200]}"
                if status == 401:
                    last_err += (" — check OPENAI_API_KEY / OPENAI_BASE_URL "
                                 "(Authorization: Bearer 已发送)")
            except RuntimeError:
                raise  # 配置类错误不重试
            except Exception as e:  # 网络类错误 → 重试
                last_err = e
            if attempt < self.max_retries:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
        raise RuntimeError(f"EmbeddingClient failed after "
                           f"{self.max_retries + 1} attempts: {last_err}")
