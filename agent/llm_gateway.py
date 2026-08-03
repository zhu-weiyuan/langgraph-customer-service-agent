#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM Gateway — 统一模型接入层（P1-B async 重写版）

核心能力：
1. **async 优先** — httpx.AsyncClient 连接池；保留 ``chat_sync`` 同步包装供渐进迁移
2. **多模型路由 + Fallback 链** — 按场景/租户选模型；每次尝试独立记录 attempt_id
3. **重试策略** — 最多 3 次、指数退避 + 全抖动 (full jitter)、每次尝试超时可配、
   总 deadline（默认 60s）；按错误类型分流：
   - 429   → 读 Retry-After 后重试；等不起（超 deadline）则切 fallback
   - 5xx / 网络错误 → 重试 + 熔断记录
   - 400 上下文超限 → 抛 ContextOverflowError（不重试，由上层回压缩）
   - 安全拒答（content_filter）→ 不 fallback，直接返回
4. **Token 预算** — TokenBudgetManager：key 含日期（YYYY-MM-DD）自然日重置，
   estimate → reserve → 真实 usage → reconcile 四步；后端可注入（内存 / Redis）
5. **成本统计** — 版本化价格表 MODEL_PRICES，成本按真实 usage 计算，不再恒 0
6. **精确响应缓存** — ExactResponseCache（SHA256 精确匹配；原名 SemanticCache
   属误导性命名；阈值式语义相似缓存因误命中风险高已整体移除）；key 含
   tenant_id + prompt_version + model；内存 LRU（OrderedDict）或注入 Redis 后端
7. **幂等** — chat() 可传 idempotency_key，命中直接返回缓存响应

三方依赖策略：httpx / redis 均延迟导入并守卫，纯 stdlib 环境可 import 本模块。

Usage:
    from agent.llm_gateway import LLMGateway, GatewayRequest

    gateway = LLMGateway()
    result = await gateway.chat(GatewayRequest(messages=[...],
                                               scene="customer_reply",
                                               tenant_id="free"))
    # 或同步（渐进迁移期）：
    result = gateway.chat_sync(GatewayRequest(messages=[...]))
"""

import asyncio
import concurrent.futures
import contextlib
import contextvars
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import threading
import time
import uuid
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Mock 开关（应用层压测用，默认关闭）─────────────────────
# MOCK_LLM=1 时 chat() 不路由、不发 HTTP、不动预算，直接 asyncio.sleep 后返回
# 固定内容。详见 agent/mock_llm.py。
try:
    from .mock_llm import (mock_json_text, mock_llm_enabled, mock_reply_text,
                           mock_sleep_async, mock_delay_seconds,
                           mock_json_delay_seconds)
except ImportError:                      # pragma: no cover - 脚本方式运行
    from mock_llm import (mock_json_text, mock_llm_enabled, mock_reply_text,  # type: ignore
                          mock_sleep_async, mock_delay_seconds,
                          mock_json_delay_seconds)

# 这些场景的调用方期望 JSON 字符串（意图/情绪），mock 时必须返回可解析 JSON
_MOCK_JSON_SCENES = {"intent_classification", "sentiment_analysis"}


# ─────────────────────────────────────────────────────
# 异常
# ─────────────────────────────────────────────────────

class GatewayError(RuntimeError):
    """网关通用异常基类。"""


class ContextOverflowError(GatewayError):
    """400 上下文超限：不重试，由上层触发上下文压缩后重新调用。"""

    def __init__(self, message: str, model: str = "", est_tokens: int = 0):
        super().__init__(message)
        self.model = model
        self.est_tokens = est_tokens


class BudgetExceededError(GatewayError):
    """Token 预算耗尽。"""

    def __init__(self, message: str, tenant_id: str = "", retry_after: float = 0.0):
        super().__init__(message)
        self.tenant_id = tenant_id
        self.retry_after = retry_after


class AllModelsFailedError(GatewayError):
    """Fallback 链全部失败。"""


# ─────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────

@dataclass
class ModelProfile:
    """模型注册表条目"""
    name: str              # 内部名称，如 "local-qwen"
    provider: str          # 供应商/来源，如 "llamacpp"
    base_url: str          # 约定：包含 /v1（与 llm_client 对齐）
    api_key: str
    model_id: str          # API 实际模型名
    context_window: int    # 最大上下文 token 数
    max_output: int        # 最大输出 token
    tier: str              # 层级：nano / fast / balanced / flagship
    input_cost_per_m: float = 0.0   # 兜底单价（价格表未命中时使用）
    output_cost_per_m: float = 0.0
    capabilities: List[str] = field(default_factory=list)
    enabled: bool = True
    protocol: str = "openai_chat_completions"
    api_key_env: str = ""
    languages: List[str] = field(default_factory=lambda: ["*"])
    expected_latency_ms: float = 0.0
    success_rate: float = 1.0
    priority: int = 100


def validate_model_profile(profile: ModelProfile) -> List[str]:
    """Validate a single model profile, return list of warnings."""
    warnings = []
    if not profile.name.strip():
        warnings.append("Model 'name' cannot be empty")
    if not profile.provider.strip():
        warnings.append(f"Model '{profile.name}': 'provider' cannot be empty")
    if not profile.base_url.strip():
        warnings.append(f"Model '{profile.name}': 'base_url' cannot be empty")
    if not profile.model_id.strip():
        warnings.append(f"Model '{profile.name}': 'model_id' cannot be empty")
    if profile.context_window <= 0:
        warnings.append(f"Model '{profile.name}': 'context_window' must be positive")
    if profile.max_output <= 0:
        warnings.append(f"Model '{profile.name}': 'max_output' must be positive")
    valid_tiers = {"nano", "fast", "balanced", "flagship"}
    if profile.tier not in valid_tiers:
        warnings.append(f"Model '{profile.name}': invalid tier '{profile.tier}', "
                        f"must be one of {valid_tiers}")
    if profile.input_cost_per_m < 0 or profile.output_cost_per_m < 0:
        warnings.append(f"Model '{profile.name}': costs cannot be negative")
    return warnings


def validate_gateway_config(model_profiles: List[ModelProfile]) -> List[str]:
    """Validate entire gateway configuration, return list of warnings."""
    warnings = []
    if not model_profiles:
        warnings.append("No enabled model profiles configured")
        return warnings
    if sum(1 for p in model_profiles if p.enabled) == 0:
        warnings.append("No enabled model found in configuration")
    for profile in model_profiles:
        warnings.extend(validate_model_profile(profile))
    return warnings


@dataclass
class GatewayRequest:
    """网关请求"""
    messages: List[dict]
    trace_id: str = ""
    user_id: Optional[str] = None
    scene: str = "default"
    tenant_id: str = ""
    max_output_tokens: Optional[int] = None
    temperature: float = 0.7
    idempotency_key: Optional[str] = None
    prompt_version: str = "v1"          # cache/attribution version
    metadata: Dict[str, Any] = field(default_factory=dict)
    language: str = ""
    context_tokens: int = 0
    preferred_model: str = ""


@dataclass
class GatewayResponse:
    """网关响应"""
    content: str
    model_used: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost: float
    latency_ms: float
    fallback_used: bool
    route_reason: str
    trace_id: str = ""
    cache_hit: bool = False
    safety_refusal: bool = False        # 安全拒答：不 fallback，直接返回
    idempotent_replay: bool = False     # 幂等命中重放
    attempts: int = 1                   # 实际发起的模型调用次数（含重试/fallback）
    ttft_ms: Optional[float] = None     # time-to-first-token（流式时可用，可选）


# ─────────────────────────────────────────────────────
# 价格表（版本化）
# ─────────────────────────────────────────────────────

# 单位：元 / 每百万 token（人民币）。均为公开牌价的量级参考，**以各官网实时定价为准**。
# 美元牌价按 ~7.2 汇率折算。新增价格版本时在此追加新 key，不要改历史版本（便于成本回溯）。
PRICE_VERSION = "2026-07"

MODEL_PRICES: Dict[str, Dict[str, Tuple[float, float]]] = {
    "2026-07": {
        # model_id: (input_per_million, output_per_million)
        "deepseek-chat":     (2.0, 8.0),     # DeepSeek V3 系列，以官网为准
        "deepseek-reasoner": (4.0, 16.0),    # DeepSeek R1 系列，以官网为准
        "qwen-turbo":        (0.3, 0.6),     # 阿里云百炼，以官网为准
        "qwen-plus":         (0.8, 2.0),
        "qwen-max":          (2.4, 9.6),
        "glm-4-plus":        (5.0, 5.0),     # 智谱，以官网为准
        "glm-4-flash":       (0.1, 0.1),
        "moonshot-v1-8k":    (12.0, 12.0),   # 月之暗面，以官网为准
        "doubao-pro-32k":    (0.8, 2.0),     # 火山方舟，以官网为准
        "gpt-4o":            (18.0, 72.0),   # $2.5/$10 折算，以 OpenAI 官网为准
        "gpt-4o-mini":       (1.1, 4.4),     # $0.15/$0.60 折算
        "mimo-v2.5":         (2.0, 8.0),     # 估算值（deepseek-chat 同量级），以官网为准
        "Qwen3.6-27B":       (0.0, 0.0),     # 本地部署，零边际成本
    },
}


def get_model_price(model_id: str,
                    price_version: str = PRICE_VERSION) -> Optional[Tuple[float, float]]:
    """查价格表；未命中返回 None（调用方回退 ModelProfile 兜底单价）。"""
    return MODEL_PRICES.get(price_version, {}).get(model_id)


def compute_cost(model: ModelProfile, input_tokens: int, output_tokens: int,
                 price_version: str = PRICE_VERSION) -> float:
    price = get_model_price(model.model_id, price_version)
    if price is None:
        price = (model.input_cost_per_m, model.output_cost_per_m)
    return input_tokens * price[0] / 1_000_000 + output_tokens * price[1] / 1_000_000


# ─────────────────────────────────────────────────────
# 默认模型注册表
# ─────────────────────────────────────────────────────

DEFAULT_MODELS = [
    ModelProfile(
        name="cloud-pro",
        provider="xiaomimimo",
        base_url="https://api.xiaomimimo.com/v1",
        api_key=os.getenv("LLM_API_KEY", ""),
        model_id="mimo-v2.5",
        context_window=1_000_000,
        max_output=8192,
        tier="flagship",
        capabilities=["json", "tool_call", "reasoning"],
    ),
    ModelProfile(
        name="local-qwen",
        provider="llamacpp",
        base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:8080/v1"),
        api_key=os.getenv("LOCAL_LLM_API_KEY", "local"),
        model_id="Qwen3.6-27B",
        context_window=160_000,
        max_output=4096,
        tier="balanced",
        capabilities=["json"],
    ),
]


# ─────────────────────────────────────────────────────
# 路由规则配置
# ─────────────────────────────────────────────────────



def _load_env_file() -> None:
    """Load local .env without overwriting exported variables."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except Exception:
        logger.debug("Unable to read local .env", exc_info=True)


def load_model_registry(path: Optional[str] = None) -> List[ModelProfile]:
    """Load model profiles from JSON; API keys are resolved from environment."""
    _load_env_file()
    registry_path = Path(path or os.getenv("MODEL_REGISTRY_PATH", "config/model_registry.json"))
    if not registry_path.is_absolute():
        registry_path = Path(__file__).resolve().parent.parent / registry_path
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        rows = payload.get("models", payload) if isinstance(payload, dict) else payload
        result: List[ModelProfile] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            api_key_env = str(row.get("api_key_env") or "")
            api_key = os.getenv(api_key_env, "") if api_key_env else str(row.get("api_key") or "")
            result.append(ModelProfile(
                name=str(row.get("name") or row.get("model_id")),
                provider=str(row.get("provider") or "openai-compatible"),
                base_url=str(row.get("base_url") or ""),
                api_key=api_key,
                model_id=str(row.get("model_id") or row.get("name")),
                context_window=int(row.get("context_window") or 128000),
                max_output=int(row.get("max_output") or 4096),
                tier=str(row.get("tier") or "balanced"),
                input_cost_per_m=float(row.get("input_cost_per_m") or row.get("price_input_per_million") or 0.0),
                output_cost_per_m=float(row.get("output_cost_per_m") or row.get("price_output_per_million") or 0.0),
                capabilities=list(row.get("capabilities") or []),
                enabled=bool(row.get("enabled", True)),
                protocol=str(row.get("protocol") or "openai_chat_completions"),
                api_key_env=api_key_env,
                languages=list(row.get("languages") or ["*"]),
                expected_latency_ms=float(row.get("expected_latency_ms") or 0.0),
                success_rate=float(row.get("success_rate") if row.get("success_rate") is not None else 1.0),
                priority=int(row.get("priority") or 100),
            ))
        if result and not validate_gateway_config(result):
            return result
    except Exception as exc:
        logger.info("Model registry unavailable (%s); using built-in defaults", exc)
    return []


def _default_model_profiles() -> List[ModelProfile]:
    _load_env_file()
    local = os.getenv("LLM_PROFILE", "local").lower() == "local"
    local_profile = ModelProfile(
        name="local-qwen", provider="llamacpp",
        base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:8080/v1"),
        api_key=os.getenv("OPENAI_API_KEY", "local"),
        model_id=os.getenv("OPENAI_MODEL", "Qwen3.6-27B"),
        context_window=160_000, max_output=4096, tier="balanced",
        capabilities=["json", "streaming"], protocol="openai_chat_completions",
        api_key_env="OPENAI_API_KEY", expected_latency_ms=800, priority=10)
    cloud_profile = ModelProfile(
        name="cloud-pro", provider="xiaomimimo",
        base_url=os.getenv("LLM_BASE_URL", "https://api.xiaomimimo.com/v1"),
        api_key=os.getenv("LLM_API_KEY", ""),
        model_id=os.getenv("LLM_MODEL", "mimo-v2.5"),
        context_window=1_000_000, max_output=8192, tier="flagship",
        capabilities=["json", "tool_call", "reasoning", "streaming"],
        protocol="openai_chat_completions", api_key_env="LLM_API_KEY",
        expected_latency_ms=1200, priority=20)
    return [local_profile, cloud_profile] if local else [cloud_profile, local_profile]


ROUTE_RULES = {
    "intent_classification":  {"tiers": ["balanced", "flagship"], "max_output": 256,  "risk": "low"},
    "sentiment_analysis":     {"tiers": ["balanced", "flagship"], "max_output": 256,  "risk": "low"},
    "customer_reply":         {"tiers": ["flagship", "balanced"], "max_output": 2048, "risk": "medium"},
    "summary_generation":     {"tiers": ["balanced", "flagship"], "max_output": 1024, "risk": "low"},
    "context_compaction":     {"tiers": ["balanced", "flagship"], "max_output": 512,  "risk": "low"},
    "agentic_rag_rewrite":    {"tiers": ["balanced", "flagship"], "max_output": 512,  "risk": "low"},
    "agentic_rag_evaluate":   {"tiers": ["balanced", "flagship"], "max_output": 512,  "risk": "low"},
    "default":                {"tiers": ["flagship", "balanced"], "max_output": 4096, "risk": "medium"},
}

# 仅低风险确定性场景允许进响应缓存
CACHEABLE_SCENES = {"intent_classification", "sentiment_analysis"}

TENANT_TIER_ALLOWLIST = {
    "free":     ["balanced"],
    "premium":  ["flagship", "balanced"],
    "internal": ["flagship", "balanced"],
}


# ─────────────────────────────────────────────────────
# Token 估算器
# ─────────────────────────────────────────────────────

def estimate_tokens(messages: List[dict]) -> int:
    """粗估输入 token（中文 ~1.5 chars/token, 英文 ~4 chars/token，混合按 2）。"""
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content)
        total_chars += len(str(content))
    return max(1, int(total_chars / 2) + len(messages) * 4)


# ─────────────────────────────────────────────────────
# 精确响应缓存（原 SemanticCache — 实为 SHA256 精确匹配，改名以正视听。
# 阈值式"语义相似度缓存"已整体移除：不同问题误命中同一答案的风险在客服
# 场景不可接受，且阈值难以离线验证。）
# ─────────────────────────────────────────────────────

class CacheBackend:
    """可注入的缓存后端接口（async）。"""

    async def get(self, key: str) -> Optional[str]:  # pragma: no cover - interface
        raise NotImplementedError

    async def set(self, key: str, value: str, ttl: int) -> None:  # pragma: no cover
        raise NotImplementedError


class InMemoryCacheBackend(CacheBackend):
    """内存 LRU：OrderedDict + get 时 move_to_end（修复旧版按写入时间淘汰的伪 LRU）。"""

    def __init__(self, max_size: int = 500):
        self._data: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    async def get(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._data[key]
                return None
            self._data.move_to_end(key)   # 真·LRU：命中即移到队尾
            return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (value, time.time() + ttl)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)   # 淘汰最久未使用

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class RedisCacheBackend(CacheBackend):
    """Redis 后端（redis.asyncio，延迟导入 + 守卫）。"""

    def __init__(self, redis_client: Any = None, url: str = "redis://127.0.0.1:6379/0",
                 prefix: str = "llmgw:cache"):
        self._prefix = prefix
        if redis_client is not None:
            self._redis = redis_client
        else:
            try:
                import redis.asyncio as aioredis  # 延迟导入，无 redis 包时不炸整个模块
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("redis package is required for RedisCacheBackend") from e
            self._redis = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Optional[str]:
        try:
            return await self._redis.get(f"{self._prefix}:{key}")
        except Exception as e:
            logger.warning(f"Redis cache get failed: {e}")
            return None

    async def set(self, key: str, value: str, ttl: int) -> None:
        try:
            await self._redis.set(f"{self._prefix}:{key}", value, ex=ttl)
        except Exception as e:
            logger.warning(f"Redis cache set failed: {e}")


class ExactResponseCache:
    """SHA256 精确匹配响应缓存。

    key 必须包含 tenant_id + prompt_version + model_id + scene + messages：
    - tenant_id：不同租户的系统 Prompt/策略可能不同，跨租户命中是数据泄露
    - prompt_version：Prompt 升级后旧缓存必须失效
    - model_id：不同模型答案风格不同，混用会导致体验抖动
    """

    def __init__(self, backend: Optional[CacheBackend] = None,
                 max_size: int = 500, ttl_seconds: int = 3600):
        self._backend = backend or InMemoryCacheBackend(max_size=max_size)
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    @staticmethod
    def make_key(tenant_id: str, prompt_version: str, model_id: str,
                 scene: str, messages: List[dict]) -> str:
        raw = json.dumps({
            "tenant_id": tenant_id,
            "prompt_version": prompt_version,
            "model": model_id,
            "scene": scene,
            "messages": messages,
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def get(self, tenant_id: str, prompt_version: str, model_id: str,
                  scene: str, messages: List[dict]) -> Optional[str]:
        if scene not in CACHEABLE_SCENES:
            return None
        key = self.make_key(tenant_id, prompt_version, model_id, scene, messages)
        value = await self._backend.get(key)
        with self._lock:
            if value is None:
                self._misses += 1
            else:
                self._hits += 1
        return value

    async def put(self, tenant_id: str, prompt_version: str, model_id: str,
                  scene: str, messages: List[dict], content: str) -> None:
        if scene not in CACHEABLE_SCENES:
            return
        key = self.make_key(tenant_id, prompt_version, model_id, scene, messages)
        await self._backend.set(key, content, self._ttl)

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total * 100, 2) if total else 0.0,
            }


# ─────────────────────────────────────────────────────
# Token 预算管理器（estimate → reserve → usage → reconcile）
# ─────────────────────────────────────────────────────

class BudgetBackend:
    """预算存储后端接口：按 key 原子累加，返回累加后的值。"""

    async def incr_by(self, key: str, amount: int, ttl: int) -> int:  # pragma: no cover
        raise NotImplementedError

    async def get(self, key: str) -> int:  # pragma: no cover - interface
        raise NotImplementedError


class InMemoryBudgetBackend(BudgetBackend):
    def __init__(self):
        self._data: Dict[str, int] = {}
        self._lock = threading.Lock()

    async def incr_by(self, key: str, amount: int, ttl: int) -> int:
        with self._lock:
            self._data[key] = self._data.get(key, 0) + amount
            return self._data[key]

    async def get(self, key: str) -> int:
        with self._lock:
            return self._data.get(key, 0)


class RedisBudgetBackend(BudgetBackend):
    """Redis 预算后端（redis.asyncio，延迟导入 + 守卫）。"""

    def __init__(self, redis_client: Any = None, url: str = "redis://127.0.0.1:6379/0",
                 prefix: str = "llmgw:budget"):
        self._prefix = prefix
        if redis_client is not None:
            self._redis = redis_client
        else:
            try:
                import redis.asyncio as aioredis
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("redis package is required for RedisBudgetBackend") from e
            self._redis = aioredis.from_url(url, decode_responses=True)

    async def incr_by(self, key: str, amount: int, ttl: int) -> int:
        k = f"{self._prefix}:{key}"
        new_val = await self._redis.incrby(k, amount)
        # 首次创建时设置 TTL（自然日 key 到期自动清理）
        if new_val == amount:
            await self._redis.expire(k, ttl)
        return int(new_val)

    async def get(self, key: str) -> int:
        val = await self._redis.get(f"{self._prefix}:{key}")
        return int(val) if val else 0


class TokenBudgetManager:
    """Token 预算：key 含自然日（YYYY-MM-DD），跨日自动重置。

    四步流程：
        est = estimate_tokens(messages)             # 1. estimate
        rid = await budget.reserve(tenant, est)     # 2. reserve（预占，超额抛异常）
        ... call model, get real usage ...
        await budget.reconcile(rid, actual_tokens)  # 3+4. 用真实 usage 修正预占差额
    """

    DEFAULT_LIMITS = {
        "free":     50_000,
        "premium":  500_000,
        "internal": 10_000_000,
    }

    def __init__(self, backend: Optional[BudgetBackend] = None,
                 limits: Optional[Dict[str, int]] = None,
                 clock: Callable[[], float] = time.time):
        self._backend = backend or InMemoryBudgetBackend()
        self._limits = dict(limits or self.DEFAULT_LIMITS)
        self._clock = clock                       # 可注入，便于测试跨日重置
        self._reservations: Dict[str, Tuple[str, int]] = {}   # rid → (key, reserved)
        self._lock = threading.Lock()

    def _day_key(self, tenant_id: str) -> str:
        day = time.strftime("%Y-%m-%d", time.gmtime(self._clock()))
        return f"{tenant_id or 'anonymous'}:{day}"

    def limit_for(self, tenant_id: str) -> int:
        return self._limits.get(tenant_id, self._limits.get("free", 50_000))

    async def reserve(self, tenant_id: str, estimated_tokens: int) -> str:
        """预占额度；超限回滚并抛 BudgetExceededError。返回 reservation_id。"""
        key = self._day_key(tenant_id)
        limit = self.limit_for(tenant_id)
        new_total = await self._backend.incr_by(key, estimated_tokens, ttl=2 * 86400)
        if new_total > limit:
            await self._backend.incr_by(key, -estimated_tokens, ttl=2 * 86400)  # 回滚
            raise BudgetExceededError(
                f"Token budget exceeded for tenant '{tenant_id}': "
                f"used≈{new_total - estimated_tokens}/{limit}, requested {estimated_tokens}",
                tenant_id=tenant_id,
            )
        rid = uuid.uuid4().hex
        with self._lock:
            self._reservations[rid] = (key, estimated_tokens)
        return rid

    async def reconcile(self, reservation_id: str, actual_tokens: int) -> None:
        """用真实 usage 修正预占（多退少补）。"""
        with self._lock:
            entry = self._reservations.pop(reservation_id, None)
        if entry is None:
            logger.warning(f"reconcile: unknown reservation {reservation_id}")
            return
        key, reserved = entry
        delta = actual_tokens - reserved
        if delta != 0:
            await self._backend.incr_by(key, delta, ttl=2 * 86400)

    async def release(self, reservation_id: str) -> None:
        """调用失败时释放预占（等价于 reconcile(rid, 0)）。"""
        await self.reconcile(reservation_id, 0)

    async def get_usage(self, tenant_id: str) -> int:
        return await self._backend.get(self._day_key(tenant_id))


# ─────────────────────────────────────────────────────
# 重试配置
# ─────────────────────────────────────────────────────

@dataclass
class RetryPolicy:
    max_attempts: int = 3           # 单个模型最多尝试次数
    base_delay: float = 0.5         # 指数退避基数（秒）
    max_delay: float = 10.0         # 单次退避上限
    attempt_timeout: float = 30.0   # 每次尝试超时（秒，可配）
    total_deadline: float = 60.0    # 整个 chat() 的总 deadline（秒）

    def backoff(self, attempt: int) -> float:
        """指数退避 + 全抖动（full jitter）：delay ∈ [0, min(cap, base*2^attempt)]。"""
        cap = min(self.max_delay, self.base_delay * (2 ** attempt))
        return random.uniform(0.0, cap)


# 上下文超限的常见错误特征（OpenAI 兼容各家实现）
_CONTEXT_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "too many tokens",
    "input is too long",
    "max_tokens_exceeded",
)

# 安全拒答特征
_SAFETY_FINISH_REASONS = {"content_filter", "safety"}


# ─────────────────────────────────────────────────────
# LLM Gateway 主类
# ─────────────────────────────────────────────────────

_gateway_context: contextvars.ContextVar = contextvars.ContextVar("llm_gateway_context", default={})


def set_gateway_context(**values: Any):
    """Set request-scoped gateway metadata and return a reset token."""
    current = dict(_gateway_context.get() or {})
    current.update({k: v for k, v in values.items() if v is not None})
    return _gateway_context.set(current)


def reset_gateway_context(token: Any) -> None:
    with contextlib.suppress(Exception):
        _gateway_context.reset(token)


def get_gateway_context() -> Dict[str, Any]:
    return dict(_gateway_context.get() or {})


class LLMGateway:
    """LLM Gateway — 统一模型接入层（async）。

    请求生命周期：
    1. 幂等检查（idempotency_key）→ 命中直接返回
    2. 路由决策（scene + tenant → tier → model 候选链）
    3. 精确响应缓存 → 命中直接返回
    4. Token 预算 reserve → 超预算抛 BudgetExceededError
    5. 逐候选调用：熔断检查 → 重试（退避+全抖动，总 deadline 约束）→ 错误分流
    6. 真实 usage reconcile 预算 + 成本计算 + 调用日志（每次尝试独立 attempt_id）
    """

    def __init__(self,
                 models: Optional[List[ModelProfile]] = None,
                 cache: Optional[ExactResponseCache] = None,
                 budget: Optional[TokenBudgetManager] = None,
                 retry_policy: Optional[RetryPolicy] = None,
                 http_client: Any = None,
                 circuit_breaker: Any = None):
        selected_models = models or load_model_registry() or _default_model_profiles() or DEFAULT_MODELS
        self._models = {m.name: m for m in selected_models if m.enabled}
        self._cache = cache or ExactResponseCache()
        self._budget = budget or self._build_budget()
        self._retry = retry_policy or RetryPolicy(
            attempt_timeout=float(os.getenv("LLM_ATTEMPT_TIMEOUT", "30")),
            total_deadline=float(os.getenv("LLM_TOTAL_DEADLINE", "60")),
        )
        self._http_client = http_client        # 可注入（测试用 Fake）；否则延迟创建
        self._http_client_lock = threading.Lock()
        # AsyncClient cannot be shared across event loops; sync wrappers create temporary loops.
        self._http_clients_by_loop = weakref.WeakKeyDictionary()
        self._breaker = circuit_breaker if circuit_breaker is not None else self._make_breaker()
        # 调用日志：deque(maxlen=10000)，修复旧版 list 手动截断
        self._call_log: Deque[Dict[str, Any]] = deque(maxlen=10_000)
        self._lock = threading.Lock()
        # 幂等缓存：idempotency_key → GatewayResponse
        self._idempotency: "OrderedDict[str, GatewayResponse]" = OrderedDict()
        self._idempotency_max = int(os.getenv("LLM_IDEMPOTENCY_CACHE_SIZE", "4096"))
        self._rate_limit_counts: Dict[Tuple[str, str], Tuple[int, float]] = {}
        self._rate_limit_limit = int(os.getenv("LLM_RATE_LIMIT", "60"))
        self._rate_limit_window = float(os.getenv("LLM_RATE_LIMIT_WINDOW", "60"))

    # ── 组件构造 ──────────────────────────────────────

    @staticmethod
    def _build_budget() -> TokenBudgetManager:
        backend_name = os.getenv("LLM_BUDGET_BACKEND", "memory").lower()
        if backend_name == "redis":
            try:
                return TokenBudgetManager(backend=RedisBudgetBackend(
                    url=os.getenv("LLM_BUDGET_REDIS_URL", os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))))
            except Exception as exc:
                logger.warning("Redis budget backend unavailable, using memory: %s", exc)
        return TokenBudgetManager()

    @staticmethod
    def _make_breaker() -> Any:
        try:
            from .circuit_breaker import CircuitBreaker
        except ImportError:
            try:
                from circuit_breaker import CircuitBreaker  # 直跑脚本兜底
            except ImportError:
                return None
        return CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

    def _get_http_client(self) -> Any:
        if self._http_client is not None:
            return self._http_client
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            raise RuntimeError("LLM HTTP client must be acquired inside an asyncio event loop")
        with self._http_client_lock:
            client = self._http_clients_by_loop.get(loop)
            if client is None:
                import httpx  # delayed import keeps stdlib-only import working
                client = httpx.AsyncClient(
                    # Local model endpoints must bypass desktop/system proxies.
                    trust_env=False,
                    limits=httpx.Limits(max_connections=50,
                                        max_keepalive_connections=20),
                )
                self._http_clients_by_loop[loop] = client
            return client

    async def _aclose_current_loop_client(self) -> None:
        """Close only the HTTP client owned by the current event loop."""
        if self._http_client is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with self._http_client_lock:
            client = self._http_clients_by_loop.pop(loop, None)
        if client is not None and hasattr(client, "aclose"):
            with contextlib.suppress(Exception):
                await client.aclose()

    async def aclose(self) -> None:
        if self._http_client is not None and hasattr(self._http_client, "aclose"):
            await self._http_client.aclose()
            return
        await self._aclose_current_loop_client()

    # ── 路由 ──────────────────────────────────────────

    def _get_model_by_tier(self, tier: str) -> Optional[ModelProfile]:
        for m in self._models.values():
            if m.tier == tier and m.enabled:
                return m
        return None

    def _route(self, scene: str, tenant_id: str, language: str = "", context_tokens: int = 0) -> List[Tuple[ModelProfile, str]]:
        rule = ROUTE_RULES.get(scene, ROUTE_RULES["default"])
        tier_whitelist = TENANT_TIER_ALLOWLIST.get(tenant_id, ["flagship", "balanced", "fast", "nano"])
        requested = set(rule.get("tiers", []))
        candidates: List[Tuple[ModelProfile, str, float]] = []
        for model in self._models.values():
            if not model.enabled or model.tier not in tier_whitelist or (requested and model.tier not in requested):
                continue
            if not model.api_key and not mock_llm_enabled():
                continue
            if context_tokens and model.context_window < context_tokens:
                continue
            if language and "*" not in model.languages and language not in model.languages:
                continue
            tier_rank = list(rule.get("tiers", [])).index(model.tier) if model.tier in rule.get("tiers", []) else 99
            score = tier_rank * 10000 + (1.0 - min(max(model.success_rate, 0.0), 1.0)) * 1000
            score += model.expected_latency_ms / 100.0 + model.input_cost_per_m + model.output_cost_per_m
            score += model.priority / 1000.0
            candidates.append((model, f"scene={scene},tier={model.tier},tenant={tenant_id},score={score:.2f}", score))
        if not candidates:
            for model in self._models.values():
                if model.enabled and (model.api_key or mock_llm_enabled()):
                    candidates.append((model, f"fallback_no_rule,scene={scene}", 0.0))
        candidates.sort(key=lambda item: item[2])
        return [(model, reason) for model, reason, _ in candidates]


    # ── 单次 HTTP 调用 ────────────────────────────────

    async def _post_once(self, model: ModelProfile, payload: dict, timeout: float) -> Any:
        client = self._get_http_client()
        protocol = (model.protocol or "openai_chat_completions").lower()
        headers = {"Content-Type": "application/json"}
        if protocol == "anthropic_messages":
            url = f"{model.base_url.rstrip('/')}/messages"
            headers.update({"x-api-key": model.api_key, "anthropic-version": "2023-06-01"})
        elif protocol == "google_generative":
            url = f"{model.base_url.rstrip('/')}/models/{model.model_id}:generateContent?key={model.api_key}"
        else:
            url = f"{model.base_url.rstrip('/')}/chat/completions"
            headers["Authorization"] = f"Bearer {model.api_key}"
        return await client.post(url, headers=headers, json=payload, timeout=timeout)

    @staticmethod
    def _provider_payload(model: ModelProfile, messages: List[dict], temperature: float, max_tokens: int) -> dict:
        protocol = (model.protocol or "openai_chat_completions").lower()
        if protocol == "anthropic_messages":
            system = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
            turns = [{"role": m.get("role", "user"), "content": m.get("content", "")}
                     for m in messages if m.get("role") != "system"]
            payload = {"model": model.model_id, "messages": turns, "max_tokens": max_tokens, "temperature": temperature}
            if system:
                payload["system"] = system
            return payload
        if protocol == "google_generative":
            contents = []
            for m in messages:
                role = "model" if m.get("role") == "assistant" else "user"
                contents.append({"role": role, "parts": [{"text": str(m.get("content", ""))}]})
            return {"contents": contents, "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
        payload = {"model": model.model_id, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        # llama.cpp ???? chat template ???? <think>???????
        # ??????????????????? reasoning_content ? SSE
        # ???????? content ???????????????????????
        # ????????????????????? token ?????????
        # ???????????? LOCAL_LLM_ENABLE_THINKING=1 ???
        provider = (model.provider or "").strip().lower()
        local_provider = provider in {"llamacpp", "llama.cpp", "local", "local_llm"}
        thinking_enabled = os.getenv("LOCAL_LLM_ENABLE_THINKING", "0").strip().lower() in {"1", "true", "yes", "on"}
        if local_provider and not thinking_enabled:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    @staticmethod
    def _parse_provider_response(model: ModelProfile, data: dict) -> Tuple[str, Dict[str, Any], str]:
        protocol = (model.protocol or "openai_chat_completions").lower()
        if protocol == "anthropic_messages":
            parts = data.get("content") or []
            text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
            usage = data.get("usage") or {}
            usage = {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}
            return text.strip(), usage, str(data.get("stop_reason") or "")
        if protocol == "google_generative":
            candidates = data.get("candidates") or []
            content = ((candidates[0].get("content") or {}) if candidates else {})
            parts = content.get("parts") or []
            text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
            usage = data.get("usageMetadata") or {}
            usage = {"prompt_tokens": usage.get("promptTokenCount", 0), "completion_tokens": usage.get("candidatesTokenCount", 0)}
            return text.strip(), usage, str((candidates[0].get("finishReason") if candidates else "") or "")
        choice = (data.get("choices") or [{}])[0]
        return ((choice.get("message") or {}).get("content") or "").strip(), data.get("usage") or {}, str(choice.get("finish_reason") or "")


    @staticmethod
    def _is_context_overflow(status_code: int, body_text: str) -> bool:
        if status_code != 400:
            return False
        lowered = (body_text or "").lower()
        return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)

    async def _call_model(self, model: ModelProfile, messages: List[dict],
                          temperature: float, max_tokens: int,
                          deadline: float, trace: Callable[..., None],
                          route_reason: str) -> Tuple[str, Dict[str, Any], bool]:
        """调用单个模型（重试 + 错误分流）。

        Returns:
            (content, usage, safety_refusal) —— 修复旧版注解 `-> str` 与实际
            返回 tuple 不符的问题。

        Raises:
            ContextOverflowError: 400 上下文超限（不重试）
            GatewayError: 其他失败（触发 fallback）
        """
        if not model.api_key:
            raise GatewayError(
                f"API key for model '{model.name}' (provider '{model.provider}') is empty. "
                "Set the LLM_API_KEY environment variable before calling the gateway."
            )

        payload = self._provider_payload(model, messages, temperature, max_tokens)

        try:
            import httpx
            network_errors: tuple = (httpx.TransportError, httpx.TimeoutException,
                                     ConnectionError, TimeoutError, asyncio.TimeoutError)
        except ImportError:                      # 测试注入 Fake client 时无 httpx
            network_errors = (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError)

        last_error: Optional[BaseException] = None
        for attempt in range(self._retry.max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError(
                    f"Total deadline exceeded before attempt {attempt} on {model.name}")

            attempt_id = uuid.uuid4().hex[:12]
            attempt_start = time.monotonic()
            try:
                resp = await self._post_once(
                    model, payload, timeout=min(self._retry.attempt_timeout, remaining))
            except network_errors as e:
                # 网络错误 / 超时：重试 + 熔断记录
                last_error = e
                self._record_breaker_failure(model)
                trace(model=model, attempt_id=attempt_id, status="network_error",
                      error=str(e), latency_ms=(time.monotonic() - attempt_start) * 1000,
                      route_reason=route_reason)
                delay = self._retry.backoff(attempt)
                if time.monotonic() + delay >= deadline:
                    break
                await asyncio.sleep(delay)
                continue

            status = resp.status_code
            latency_ms = (time.monotonic() - attempt_start) * 1000

            if status == 200:
                data = resp.json()
                content, usage, finish_reason = self._parse_provider_response(model, data)
                finish_reason = (finish_reason or "").lower()
                safety = finish_reason in _SAFETY_FINISH_REASONS
                self._record_breaker_success(model)
                trace(model=model, attempt_id=attempt_id, status="ok",
                      latency_ms=latency_ms, usage=usage, route_reason=route_reason,
                      safety_refusal=safety)
                return content, usage, safety

            body_text = ""
            try:
                body_text = resp.text
            except Exception:
                pass

            if status == 429:
                # 429：读 Retry-After；等得起就等再重试，等不起（超 deadline）break 切 fallback
                retry_after = self._parse_retry_after(resp)
                delay = retry_after if retry_after is not None else self._retry.backoff(attempt)
                self._record_breaker_failure(model)
                trace(model=model, attempt_id=attempt_id, status="rate_limited",
                      latency_ms=latency_ms, retry_after=delay, route_reason=route_reason)
                last_error = GatewayError(f"HTTP 429 from {model.name}")
                if time.monotonic() + delay >= deadline:
                    break                        # 切 fallback
                await asyncio.sleep(delay)
                continue

            if self._is_context_overflow(status, body_text):
                # 400 上下文超限：不重试、不 fallback（换模型大概率同样超限），
                # 抛给上层触发 context compaction 后重新进入网关。
                trace(model=model, attempt_id=attempt_id, status="context_overflow",
                      latency_ms=latency_ms, route_reason=route_reason)
                raise ContextOverflowError(
                    f"Context window exceeded on {model.name}: {body_text[:200]}",
                    model=model.name, est_tokens=estimate_tokens(messages))

            if status in (500, 502, 503, 504):
                # 5xx：重试 + 熔断记录
                last_error = GatewayError(f"HTTP {status} from {model.name}")
                self._record_breaker_failure(model)
                trace(model=model, attempt_id=attempt_id, status=f"http_{status}",
                      latency_ms=latency_ms, route_reason=route_reason)
                delay = self._retry.backoff(attempt)
                if time.monotonic() + delay >= deadline:
                    break
                await asyncio.sleep(delay)
                continue

            # 其他 4xx（400 参数错 / 401 / 403）：不可重试
            trace(model=model, attempt_id=attempt_id, status=f"http_{status}",
                  latency_ms=latency_ms, route_reason=route_reason)
            raise GatewayError(f"HTTP {status} from {model.name}: {body_text[:200]}")

        raise GatewayError(
            f"Model {model.name} failed after retries: {last_error}") from last_error

    @staticmethod
    def _parse_retry_after(resp: Any) -> Optional[float]:
        try:
            value = resp.headers.get("Retry-After")
        except Exception:
            return None
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    # ── 熔断记录 ─────────────────────────────────────

    def _record_breaker_metric(self, model: ModelProfile) -> None:
        """Publish the current per-model breaker state without affecting traffic."""
        if self._breaker is None:
            return
        try:
            state = self._breaker.state(model.provider, model.model_id)
            from .metrics import metrics
            metrics.set_circuit_breaker_state(
                f"{model.provider}:{model.model_id}",
                getattr(state, "value", state),
            )
        except Exception:
            # Observability must never turn a provider call into a failure.
            pass

    def _breaker_allow(self, model: ModelProfile) -> bool:
        if self._breaker is None:
            return True
        try:
            self._breaker.allow_request(model.provider, model.model_id)
            self._record_breaker_metric(model)
            return True
        except Exception:
            self._record_breaker_metric(model)
            return False

    def _record_breaker_failure(self, model: ModelProfile) -> None:
        if self._breaker is not None:
            try:
                self._breaker.record_failure(model.provider, model.model_id)
            except Exception:
                pass
            finally:
                self._record_breaker_metric(model)

    def _record_breaker_success(self, model: ModelProfile) -> None:
        if self._breaker is not None:
            try:
                self._breaker.record_success(model.provider, model.model_id)
            except Exception:
                pass
            finally:
                self._record_breaker_metric(model)

    # ── 调用日志 ─────────────────────────────────────

    def _log_attempt(self, req: GatewayRequest, **kw: Any) -> None:
        model = kw.pop("model", None)
        entry = {
            "timestamp": time.time(),
            "trace_id": req.trace_id,
            "scene": req.scene,
            "tenant_id": req.tenant_id,
            "model": getattr(model, "name", ""),
            "provider": getattr(model, "provider", ""),
        }
        entry.update(kw)          # attempt_id / status / latency_ms / route_reason / ttft...
        with self._lock:
            self._call_log.append(entry)
        try:
            from .metrics import metrics
            status = str(entry.get("status") or "attempt")
            model_name = entry.get("model") or "unknown"
            metrics.record_llm_attempt(model_name, req.scene, status)
            if status in {"rate_limited", "context_overflow"}:
                metrics.record_llm_error(model_name, req.scene, status)
            elif status.startswith("network") or status.startswith("http_5"):
                metrics.record_llm_error(model_name, req.scene, "error")
        except Exception:
            pass

    # ── 幂等 ─────────────────────────────────────────

    def _record_observability(self, req: GatewayRequest, response: GatewayResponse,
                              *, stage: str = "generate", finish_reason: str = "stop") -> None:
        """Record Prometheus metrics and rich TraceSession data without affecting requests."""
        try:
            from .metrics import metrics
            model = response.model_used or "unknown"
            metrics.record_llm_request(model, "cache_hit" if response.cache_hit else "success")
            metrics.record_llm_tokens(model, req.scene, "input", response.input_tokens)
            metrics.record_llm_tokens(model, req.scene, "output", response.output_tokens)
            metrics.record_llm_cost(model, response.cost)
            # Keep attribution queryable by tenant/user/scene/prompt version.
            # The user value is anonymized before it becomes a Prometheus label.
            user_key = hashlib.sha256(
                str(req.user_id or "anonymous").encode("utf-8")
            ).hexdigest()[:16]
            metrics.record_llm_cost_attribution(
                model, req.tenant_id, user_key, req.scene,
                req.prompt_version, response.cost
            )
            metrics.record_llm_latency(model, req.scene, response.latency_ms / 1000.0)
            if response.ttft_ms is not None:
                metrics.record_llm_ttft(model, req.scene, response.ttft_ms / 1000.0)
            if response.fallback_used:
                metrics.record_llm_fallback(req.scene, model)
            metrics.record_llm_route(req.scene, model)
        except Exception:
            logger.debug("gateway metrics recording failed", exc_info=True)
        trace = req.metadata.get("trace_session") if isinstance(req.metadata, dict) else None
        if trace is not None:
            with contextlib.suppress(Exception):
                trace.record_model(provider=response.provider, model=response.model_used,
                                  params={"temperature": req.temperature, "stream": stage.endswith("stream")},
                                  in_tok=response.input_tokens, out_tok=response.output_tokens,
                                  finish=finish_reason, ttft_ms=response.ttft_ms, stage=stage)
            with contextlib.suppress(Exception):
                trace.record_latency(model_ttft_ms=response.ttft_ms, total_ms=response.latency_ms)
            with contextlib.suppress(Exception):
                trace.record_cost(input_cost=0.0, output_cost=response.cost,
                                  cache_hit=response.cache_hit, tenant=req.tenant_id,
                                  scene=req.scene, prompt_version=req.prompt_version)

    @staticmethod
    def _record_cache_event(cache: str, result: str) -> None:
        """Record cache/idempotency events without making observability a dependency."""
        with contextlib.suppress(Exception):
            from .metrics import metrics
            metrics.record_cache_event(cache, result)

    def _idempotency_get(self, key: Optional[str]) -> Optional[GatewayResponse]:
        if not key:
            return None
        with self._lock:
            resp = self._idempotency.get(key)
            if resp is not None:
                self._idempotency.move_to_end(key)
        return resp

    def _idempotency_put(self, key: Optional[str], resp: GatewayResponse) -> None:
        if not key:
            return
        with self._lock:
            self._idempotency[key] = resp
            self._idempotency.move_to_end(key)
            while len(self._idempotency) > self._idempotency_max:
                self._idempotency.popitem(last=False)

    def _check_rate_limit(self, tenant_id: str, user_id: Optional[str]) -> bool:
        """Process-local fixed-window guard for direct gateway callers."""
        key = (tenant_id or "anonymous", user_id or "anonymous")
        now = time.monotonic()
        with self._lock:
            count, started = self._rate_limit_counts.get(key, (0, now))
            if now - started >= self._rate_limit_window:
                count, started = 0, now
            if count >= self._rate_limit_limit:
                return False
            self._rate_limit_counts[key] = (count + 1, started)
            return True

    def get_rate_limit_info(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            usage = {}
            for key, (count, started) in self._rate_limit_counts.items():
                usage["%s:%s" % key] = 0 if now - started >= self._rate_limit_window else count
            return {"limit": self._rate_limit_limit,
                    "window_seconds": self._rate_limit_window,
                    "tracked_keys": len(self._rate_limit_counts),
                    "usage": usage}


    async def _mock_chat(self, req: GatewayRequest,
                         start: float) -> GatewayResponse:
        """MOCK_LLM=1 时的假响应：asyncio.sleep（不阻塞事件循环）+ 确定性内容。"""
        json_scene = req.scene in _MOCK_JSON_SCENES
        await mock_sleep_async(mock_json_delay_seconds() if json_scene
                               else mock_delay_seconds())
        content = (mock_json_text(req.messages) if json_scene
                   else mock_reply_text(req.messages))
        response = GatewayResponse(
            content=content, model_used="mock", provider="mock",
            input_tokens=estimate_tokens(req.messages),
            output_tokens=max(len(content) // 2, 1),
            cost=0.0, latency_ms=(time.monotonic() - start) * 1000,
            fallback_used=False, route_reason="mock_llm",
            trace_id=req.trace_id, attempts=1)
        self._record_observability(req, response)
        return response

    async def chat(self, req: GatewayRequest) -> GatewayResponse:
        """主入口（async）：处理一次 LLM 请求。"""
        start = time.monotonic()
        deadline = start + self._retry.total_deadline
        req.trace_id = req.trace_id or uuid.uuid4().hex[:16]

        # ── Step -1: MOCK_LLM=1（应用层压测）───────────
        # 固定延迟 + 固定内容，绕过路由/缓存/预算/熔断，让压测数字只反映应用层。
        if mock_llm_enabled():
            return await self._mock_chat(req, start)

        # ── Step 0: 幂等命中 ─────────────────────────
        replay = self._idempotency_get(req.idempotency_key)
        if replay is not None:
            resp = copy.copy(replay)
            resp.idempotent_replay = True
            self._record_cache_event("idempotency", "replay")
            self._record_observability(req, resp, stage="idempotency_replay")
            return resp

        # ── Step 1: 路由（先路由，缓存 key 需要 model_id）──
        if not self._check_rate_limit(req.tenant_id, req.user_id):
            self._log_attempt(req, status="rate_limited", route_reason="gateway_rate_limit")
            try:
                from .metrics import metrics
                metrics.record_rate_limit_event("gateway")
            except Exception:
                pass
            raise GatewayError("LLM gateway rate limit exceeded")

        candidates = self._route(req.scene, req.tenant_id, req.language, req.context_tokens)
        if req.preferred_model and req.preferred_model in self._models:
            preferred = self._models[req.preferred_model]
            candidates = [(preferred, "preferred_model=" + req.preferred_model)] + [c for c in candidates if c[0].name != req.preferred_model]
        if not candidates:
            raise GatewayError("No available models")
        primary_model = candidates[0][0]
        max_out = req.max_output_tokens or ROUTE_RULES.get(
            req.scene, ROUTE_RULES["default"])["max_output"]

        # ── Step 2: 精确响应缓存 ─────────────────────
        cached = await self._cache.get(
            req.tenant_id, req.prompt_version, primary_model.model_id,
            req.scene, req.messages)
        if cached is not None:
            self._record_cache_event("exact_response", "hit")
            resp = GatewayResponse(
                content=cached, model_used="cache", provider="cache",
                input_tokens=0, output_tokens=0, cost=0.0,
                latency_ms=(time.monotonic() - start) * 1000,
                fallback_used=False, route_reason="cache_hit",
                trace_id=req.trace_id, cache_hit=True, attempts=0)
            self._idempotency_put(req.idempotency_key, resp)
            self._record_observability(req, resp, stage="cache")
            return resp
        if req.scene in CACHEABLE_SCENES:
            self._record_cache_event("exact_response", "miss")

        # ── Step 3: 预算 reserve（estimate → reserve）─
        est_input = estimate_tokens(req.messages)
        reservation_id = await self._budget.reserve(req.tenant_id, est_input + max_out)

        attempts_made = 0
        last_error: Optional[BaseException] = None
        try:
            # ── Step 4: Fallback 链 ──────────────────
            for i, (model, reason) in enumerate(candidates):
                fallback_used = i > 0
                if fallback_used:
                    logger.warning(f"Fallback to {model.name} (reason: {reason})")
                if not self._breaker_allow(model):
                    self._log_attempt(req, model=model, attempt_id=uuid.uuid4().hex[:12],
                                      status="circuit_open", route_reason=reason,
                                      fallback_used=fallback_used)
                    last_error = GatewayError(f"Circuit open for {model.name}")
                    continue

                def trace(**kw: Any) -> None:
                    nonlocal attempts_made
                    attempts_made += 1
                    self._log_attempt(req, fallback_used=fallback_used, **kw)

                try:
                    content, usage, safety = await self._call_model(
                        model, req.messages, req.temperature, max_out,
                        deadline, trace, reason)
                except ContextOverflowError:
                    # 不 fallback：换模型大概率同样超限，直接抛给上层压缩
                    raise
                except Exception as e:
                    last_error = e
                    logger.error(f"Model {model.name} failed: {e}")
                    continue

                # ── Step 5: 真实 usage → reconcile ───
                actual_input = int(usage.get("prompt_tokens", est_input))
                actual_output = int(usage.get("completion_tokens",
                                              max(len(content) // 2, 1)))
                await self._budget.reconcile(reservation_id,
                                             actual_input + actual_output)

                cost = compute_cost(model, actual_input, actual_output)
                latency_ms = (time.monotonic() - start) * 1000

                # 安全拒答：直接返回，不写缓存、不 fallback
                if not safety:
                    await self._cache.put(req.tenant_id, req.prompt_version,
                                          model.model_id, req.scene,
                                          req.messages, content)
                    if req.scene in CACHEABLE_SCENES:
                        self._record_cache_event("exact_response", "set")

                resp = GatewayResponse(
                    content=content, model_used=model.name, provider=model.provider,
                    input_tokens=actual_input, output_tokens=actual_output,
                    cost=cost, latency_ms=latency_ms, fallback_used=fallback_used,
                    route_reason=reason, trace_id=req.trace_id,
                    safety_refusal=safety, attempts=max(attempts_made, 1))
                self._idempotency_put(req.idempotency_key, resp)
                self._record_observability(req, resp)
                return resp

            raise AllModelsFailedError(
                f"All models in fallback chain failed. Last error: {last_error}"
            ) from last_error
        except BaseException:
            # 失败路径释放预占额度（成功路径已 reconcile，release 为 no-op 前已 pop）
            await self._budget.release(reservation_id)
            raise

    # ── 同步包装（渐进迁移期）─────────────────────────

    @staticmethod
    def _stream_payload(model: ModelProfile, messages: List[dict],
                        temperature: float, max_tokens: int) -> dict:
        payload = LLMGateway._provider_payload(model, messages, temperature, max_tokens)
        protocol = (model.protocol or "openai_chat_completions").lower()
        if protocol == "google_generative":
            payload["alt"] = "sse"
        else:
            payload["stream"] = True
            # OpenAI-compatible servers that support this return usage in the final chunk.
            # llama.cpp's OpenAI-compatible endpoint may reject ``stream_options``
            # (often surfacing as a non-descriptive 502), so keep this opt-in for
            # cloud OpenAI-compatible providers only.
            if protocol == "openai_chat_completions" and (model.provider or "").lower() not in {
                "llamacpp", "llama.cpp", "local", "local_llm"
            }:
                payload.setdefault("stream_options", {"include_usage": True})
        return payload

    @staticmethod
    def _stream_url_headers(model: ModelProfile) -> Tuple[str, Dict[str, str]]:
        protocol = (model.protocol or "openai_chat_completions").lower()
        headers = {"Content-Type": "application/json"}
        if protocol == "anthropic_messages":
            return (f"{model.base_url.rstrip('/')}/messages",
                    {**headers, "x-api-key": model.api_key,
                     "anthropic-version": "2023-06-01"})
        if protocol == "google_generative":
            return (f"{model.base_url.rstrip('/')}/models/{model.model_id}:streamGenerateContent?alt=sse&key={model.api_key}", headers)
        return (f"{model.base_url.rstrip('/')}/chat/completions",
                {**headers, "Authorization": f"Bearer {model.api_key}"})

    @staticmethod
    def _stream_json_line(model: ModelProfile, data: dict, state: Dict[str, Any]) -> str:
        protocol = (model.protocol or "openai_chat_completions").lower()
        if protocol == "anthropic_messages":
            event_type = str(data.get("type") or "")
            if event_type == "message_start":
                usage = (data.get("message") or {}).get("usage") or {}
                state["input_tokens"] = int(usage.get("input_tokens") or 0)
            elif event_type == "message_delta":
                usage = data.get("usage") or {}
                state["output_tokens"] = int(usage.get("output_tokens") or 0)
                state["finish_reason"] = str(data.get("delta", {}).get("stop_reason") or "")
            elif event_type == "content_block_delta":
                return str((data.get("delta") or {}).get("text") or "")
            return ""
        if protocol == "google_generative":
            candidates = data.get("candidates") or []
            if candidates:
                state["finish_reason"] = str(candidates[0].get("finishReason") or "")
                parts = ((candidates[0].get("content") or {}).get("parts") or [])
                text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
                usage = data.get("usageMetadata") or {}
                state["input_tokens"] = int(usage.get("promptTokenCount") or state.get("input_tokens", 0))
                state["output_tokens"] = int(usage.get("candidatesTokenCount") or state.get("output_tokens", 0))
                return text
            return ""
        choices = data.get("choices") or []
        if choices:
            choice = choices[0] or {}
            state["finish_reason"] = str(choice.get("finish_reason") or state.get("finish_reason", ""))
            delta = choice.get("delta") or {}
            usage = data.get("usage") or {}
            state["input_tokens"] = int(usage.get("prompt_tokens") or state.get("input_tokens", 0))
            state["output_tokens"] = int(usage.get("completion_tokens") or state.get("output_tokens", 0))
            return str(delta.get("content") or "")
        return ""

    async def _stream_once(self, model: ModelProfile, payload: dict,
                           timeout: float, state: Dict[str, Any]) -> AsyncIterator[str]:
        client = self._get_http_client()
        url, headers = self._stream_url_headers(model)
        # httpx.AsyncClient.stream is required for true incremental delivery.
        async with client.stream("POST", url, headers=headers, json=payload, timeout=timeout) as response:
            status = int(getattr(response, "status_code", 200))
            if status != 200:
                try:
                    body = await response.aread()
                    body_text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
                except Exception:
                    body_text = str(getattr(response, "text", "") or "")
                err = GatewayError(f"HTTP {status} from {model.name}: {body_text[:200]}")
                setattr(err, "status_code", status)
                setattr(err, "body_text", body_text)
                raise err
            async for raw_line in response.aiter_lines():
                line = (raw_line or "").strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    state["event"] = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data_text = line[5:].strip()
                else:
                    # Some local servers emit newline-delimited JSON instead of SSE.
                    data_text = line
                if data_text == "[DONE]":
                    break
                try:
                    data = json.loads(data_text)
                except (TypeError, ValueError):
                    continue
                token = self._stream_json_line(model, data, state)
                if token:
                    state["emitted"] = True
                    yield token

    async def stream(self, req: GatewayRequest) -> AsyncIterator[Dict[str, Any]]:
        """True provider streaming with routing, fallback, budget and observability."""
        start = time.monotonic()
        deadline = start + self._retry.total_deadline
        req.trace_id = req.trace_id or uuid.uuid4().hex[:16]
        replay = self._idempotency_get(req.idempotency_key)
        if replay is not None:
            replay = copy.copy(replay)
            replay.idempotent_replay = True
            self._record_cache_event("idempotency", "replay")
            self._record_observability(req, replay, stage="idempotency_replay_stream")
            for piece in replay.content.splitlines(True) or ([replay.content] if replay.content else []):
                if piece:
                    yield {"token": piece}
            yield {"done": True, "response": replay}
            return
        if not self._check_rate_limit(req.tenant_id, req.user_id):
            self._log_attempt(req, status="rate_limited", route_reason="gateway_rate_limit")
            with contextlib.suppress(Exception):
                from .metrics import metrics
                metrics.record_rate_limit_event("gateway")
            raise GatewayError("LLM gateway rate limit exceeded")

        candidates = self._route(req.scene, req.tenant_id, req.language, req.context_tokens)
        if req.preferred_model and req.preferred_model in self._models:
            preferred = self._models[req.preferred_model]
            candidates = [(preferred, "preferred_model=" + req.preferred_model)] + [c for c in candidates if c[0].name != preferred.name]
        if not candidates:
            raise GatewayError("No available models")
        primary = candidates[0][0]
        cached = await self._cache.get(req.tenant_id, req.prompt_version, primary.model_id, req.scene, req.messages)
        if cached is not None:
            self._record_cache_event("exact_response", "hit")
            response = GatewayResponse(cached, "cache", "cache", 0, 0, 0.0,
                                       (time.monotonic() - start) * 1000, False, "cache_hit",
                                       req.trace_id, cache_hit=True, attempts=0)
            self._idempotency_put(req.idempotency_key, response)
            self._record_observability(req, response, stage="cache_stream")
            for piece in cached.splitlines(True) or ([cached] if cached else []):
                if piece:
                    yield {"token": piece}
            yield {"done": True, "response": response}
            return
        if req.scene in CACHEABLE_SCENES:
            self._record_cache_event("exact_response", "miss")

        max_out = req.max_output_tokens or ROUTE_RULES.get(req.scene, ROUTE_RULES["default"])["max_output"]
        reservation_id = await self._budget.reserve(req.tenant_id, estimate_tokens(req.messages) + max_out)
        est_input = estimate_tokens(req.messages)
        last_error: Optional[BaseException] = None
        try:
            for index, (model, route_reason) in enumerate(candidates):
                fallback_used = index > 0
                if not self._breaker_allow(model):
                    self._log_attempt(req, model=model, status="circuit_open", route_reason=route_reason, fallback_used=fallback_used)
                    last_error = GatewayError(f"Circuit open for {model.name}")
                    continue
                state: Dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "finish_reason": "", "emitted": False}
                payload = self._stream_payload(model, req.messages, req.temperature, min(max_out, model.max_output))
                attempt_id = uuid.uuid4().hex[:12]
                attempt_start = time.monotonic()
                try:
                    async for token in self._stream_once(model, payload,
                                                          min(self._retry.attempt_timeout, max(0.1, deadline - time.monotonic())), state):
                        state["content"] = str(state.get("content") or "") + str(token)
                        state["chars"] = len(state["content"])
                        if not state.get("first_token_at"):
                            state["first_token_at"] = time.monotonic()
                            ttft = (state["first_token_at"] - start) * 1000
                            self._log_attempt(req, model=model, attempt_id=attempt_id, status="first_token",
                                              ttft_ms=ttft, route_reason=route_reason, fallback_used=fallback_used)
                        yield {"token": token}
                except Exception as exc:
                    last_error = exc
                    status_code = int(getattr(exc, "status_code", 0) or 0)
                    status = "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else (f"http_{status_code}" if status_code else "stream_error")
                    self._record_breaker_failure(model)
                    self._log_attempt(req, model=model, attempt_id=attempt_id, status=status,
                                      error=str(exc), latency_ms=(time.monotonic() - attempt_start) * 1000,
                                      route_reason=route_reason, fallback_used=fallback_used)
                    # Once data was delivered, changing model would duplicate the answer.
                    if state.get("emitted"):
                        raise
                    continue

                actual_input = int(state.get("input_tokens") or est_input)
                content = str(state.get("content") or "")
                actual_output = int(state.get("output_tokens") or max(1, int(len(content) / 2)))
                await self._budget.reconcile(reservation_id, actual_input + actual_output)
                cost = compute_cost(model, actual_input, actual_output)
                response = GatewayResponse(content=content, model_used=model.name, provider=model.provider,
                                           input_tokens=actual_input, output_tokens=actual_output, cost=cost,
                                           latency_ms=(time.monotonic() - start) * 1000,
                                           fallback_used=fallback_used, route_reason=route_reason,
                                           trace_id=req.trace_id, attempts=1,
                                           ttft_ms=((state.get("first_token_at") - start) * 1000 if state.get("first_token_at") else None))
                self._idempotency_put(req.idempotency_key, response)
                if content:
                    await self._cache.put(req.tenant_id, req.prompt_version, model.model_id, req.scene, req.messages, content)
                    if req.scene in CACHEABLE_SCENES:
                        self._record_cache_event("exact_response", "set")
                self._record_observability(req, response, stage="generate_stream", finish_reason=state.get("finish_reason", "stop"))
                self._log_attempt(req, model=model, attempt_id=attempt_id, status="ok",
                                  latency_ms=response.latency_ms, ttft_ms=response.ttft_ms,
                                  usage={"prompt_tokens": actual_input, "completion_tokens": actual_output},
                                  route_reason=route_reason, fallback_used=fallback_used)
                yield {"done": True, "response": response}
                return
            raise AllModelsFailedError(f"All models in streaming fallback chain failed. Last error: {last_error}") from last_error
        except BaseException:
            await self._budget.release(reservation_id)
            raise

    def stream_sync(self, req: GatewayRequest):
        """Bridge async provider SSE to the synchronous graph node without buffering."""
        import queue
        q: queue.Queue = queue.Queue()
        sentinel = object()
        errors: List[BaseException] = []

        def worker() -> None:
            async def consume() -> None:
                collected = []
                try:
                    async for event in self.stream(req):
                        if event.get("token"):
                            token = str(event["token"])
                            collected.append(token)
                            q.put(token)
                        elif event.get("done"):
                            response = event.get("response")
                            if response is not None:
                                req.metadata["gateway_response"] = response
                                req.metadata["stream_content"] = "".join(collected)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    await self._aclose_current_loop_client()
                    q.put(sentinel)
            asyncio.run(consume())

        threading.Thread(target=worker, daemon=True, name="llm-gateway-stream").start()
        while True:
            item = q.get()
            if item is sentinel:
                break
            yield item
        if errors:
            raise errors[0]

    async def _chat_sync_worker(self, req: GatewayRequest) -> GatewayResponse:
        try:
            return await self.chat(req)
        finally:
            await self._aclose_current_loop_client()

    def chat_sync(self, req: GatewayRequest) -> GatewayResponse:
        """Synchronous wrapper that closes temporary-loop HTTP clients safely."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._chat_sync_worker(req))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, self._chat_sync_worker(req)).result()

    # ── 便捷方法 ──────────────────────────────────────

    async def chat_simple(self, messages: List[dict], scene: str = "default",
                          tenant_id: str = "", **kwargs: Any) -> GatewayResponse:
        return await self.chat(GatewayRequest(
            messages=messages, scene=scene, tenant_id=tenant_id, **kwargs))

    async def chat_text(self, messages: List[dict], scene: str = "default",
                        tenant_id: str = "", **kwargs: Any) -> str:
        resp = await self.chat_simple(messages, scene=scene,
                                      tenant_id=tenant_id, **kwargs)
        return resp.content

    # ── 观测接口 ──────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """获取网关统计信息（加锁快照，避免遍历时并发 append 竞态）。"""
        with self._lock:
            log_snapshot = list(self._call_log)

        total_attempts = len(log_snapshot)
        if total_attempts == 0:
            return {"total_attempts": 0, "cache": self._cache.stats}

        ok_entries = [t for t in log_snapshot if t.get("status") == "ok"]
        avg_latency = (sum(t.get("latency_ms", 0.0) for t in ok_entries) / len(ok_entries)
                       if ok_entries else 0.0)
        fallback_count = sum(1 for t in ok_entries if t.get("fallback_used"))

        by_scene: Dict[str, int] = {}
        by_model: Dict[str, int] = {}
        by_status: Dict[str, int] = {}
        for t in log_snapshot:
            by_scene[t.get("scene", "?")] = by_scene.get(t.get("scene", "?"), 0) + 1
            by_model[t.get("model", "?")] = by_model.get(t.get("model", "?"), 0) + 1
            by_status[t.get("status", "?")] = by_status.get(t.get("status", "?"), 0) + 1

        return {
            "total_attempts": total_attempts,
            "success_count": len(ok_entries),
            "avg_latency_ms": round(avg_latency, 2),
            "fallback_count": fallback_count,
            "by_scene": by_scene,
            "by_model": by_model,
            "by_status": by_status,
            "cache": self._cache.stats,
            "price_version": PRICE_VERSION,
        }

    def get_recent_calls(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._call_log)[-limit:]


# ─────────────────────────────────────────────────────
# 全局单例
# ─────────────────────────────────────────────────────

_gateway_instance: Optional[LLMGateway] = None
_gateway_lock = threading.Lock()


def get_llm_gateway() -> LLMGateway:
    """获取全局 LLM Gateway 实例。"""
    global _gateway_instance
    if _gateway_instance is None:
        with _gateway_lock:
            if _gateway_instance is None:
                _gateway_instance = LLMGateway()
    return _gateway_instance
