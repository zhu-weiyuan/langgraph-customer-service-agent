#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM Gateway — 统一模型接入层

核心能力：
1. **多模型路由** — 按场景自动选择最合适的模型（小模型省钱，大模型保质量）
2. **Fallback 链** — 主模型失败 → 备用模型 → 降级回复，不中断业务
3. **Token 预算** — 估算输入 Token + 预留输出，超预算拒绝/压缩
4. **成本统计** — 每次调用记录 usage/cost/延迟/路由原因
5. **语义缓存** — 相同问题直接返回缓存结果，省模型调用

参考 JavaGuide LLM Gateway 设计模式。
第一版：规则路由 + Fallback + Token 估算 + 日志审计

Usage:
    from agent.llm_gateway import LLMGateway

    gateway = LLMGateway()
    result = gateway.chat(
        messages=[...],
        scene="customer_reply",   # 按场景路由
        tenant_id="free_user",    # 按租户分级
    )
"""

import json
import os
import time
import hashlib
import logging
import threading
from collections import defaultdict
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────

@dataclass
class ModelProfile:
    """模型注册表条目"""
    name: str              # 内部名称，如 "local-qwen"
    provider: str          # 供应商/来源，如 "llamacpp"
    base_url: str
    api_key: str
    model_id: str          # API 实际模型名
    context_window: int    # 最大上下文 token 数
    max_output: int        # 最大输出 token
    tier: str              # 层级：nano / fast / balanced / flagship
    input_cost_per_m: float   # 每百万 token 输入成本（元）
    output_cost_per_m: float  # 每百万 token 输出成本
    capabilities: List[str] = field(default_factory=list)  # ["json", "tool_call", "reasoning"]
    enabled: bool = True


def validate_model_profile(profile: ModelProfile) -> List[str]:
    """Validate a single model profile, return list of warnings (Phase A: Config Validation)."""
    warnings = []
    
    if not profile.name.strip():
        warnings.append(f"Model 'name' cannot be empty")
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
        warnings.append(f"Model '{profile.name}': invalid tier '{profile.tier}', must be one of {valid_tiers}")
    if profile.input_cost_per_m < 0 or profile.output_cost_per_m < 0:
        warnings.append(f"Model '{profile.name}': costs cannot be negative")
        
    return warnings


def validate_gateway_config(model_profiles: List[ModelProfile]) -> List[str]:
    """Validate entire gateway configuration, return list of warnings (Phase A)."""
    warnings = []
    
    if not model_profiles:
        warnings.append("No enabled model profiles configured")
        return warnings
        
    enabled_count = sum(1 for p in model_profiles if p.enabled)
    if enabled_count == 0:
        warnings.append("No enabled model found in configuration")
        
    for profile in model_profiles:
        profile_warnings = validate_model_profile(profile)
        warnings.extend(profile_warnings)
        
    return warnings


@dataclass
class GatewayRequest:
    """网关请求 (Phase A: Request Governance)"""
    messages: List[dict]
    trace_id: str = ""          # 全链路追踪 ID
    user_id: Optional[str] = None  # 用户 ID（用于 per-user rate limiting）
    scene: str = "default"       # 业务场景
    tenant_id: str = ""          # 租户/用户类型
    max_output_tokens: Optional[int] = None
    temperature: float = 0.7
    idempotency_key: Optional[str] = None


@dataclass
class GatewayResponse:
    """网关响应 (Phase A: trace_id for PII-safe observability)"""
    content: str
    model_used: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost: float
    latency_ms: float
    fallback_used: bool
    route_reason: str
    trace_id: str = ""        # Phase A: trace for correlation, NOT message content
    cache_hit: bool = False


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
        input_cost_per_m=0.0,   # 根据实际定价填写
        output_cost_per_m=0.0,
        capabilities=["json", "tool_call", "reasoning"],
    ),
    ModelProfile(
        name="local-qwen",
        provider="llamacpp",
        base_url="http://localhost:8080/v1",
        api_key="your_key_here",
        model_id="Qwen3.6-27B",
        context_window=160_000,
        max_output=4096,
        tier="balanced",
        input_cost_per_m=0.0,   # 本地部署，零边际成本
        output_cost_per_m=0.0,
        capabilities=["json"],
    ),
]


# ─────────────────────────────────────────────────────
# 路由规则配置
# ─────────────────────────────────────────────────────

# scene → [primary tier, fallback tiers]
ROUTE_RULES = {
    "intent_classification":  {"tiers": ["balanced", "flagship"],    "max_output": 256,   "risk": "low"},
    "sentiment_analysis":     {"tiers": ["balanced", "flagship"],    "max_output": 256,   "risk": "low"},
    "customer_reply":         {"tiers": ["flagship", "balanced"],    "max_output": 2048,  "risk": "medium"},
    "summary_generation":     {"tiers": ["balanced", "flagship"],    "max_output": 1024,  "risk": "low"},
    "context_compaction":     {"tiers": ["balanced", "flagship"],    "max_output": 512,   "risk": "low"},
    "agentic_rag_rewrite":    {"tiers": ["balanced", "flagship"],    "max_output": 512,   "risk": "low"},
    "agentic_rag_evaluate":   {"tiers": ["balanced", "flagship"],    "max_output": 512,   "risk": "low"},
    "default":                {"tiers": ["flagship", "balanced"],    "max_output": 4096,  "risk": "medium"},
}

# 租户 → 可用 tier 白名单（免费用户不用旗舰模型）
TENANT_TIER_ALLOWLIST = {
    "free":     ["balanced"],
    "premium":  ["flagship", "balanced"],
    "internal": ["flagship", "balanced"],
}


# ─────────────────────────────────────────────────────
# Token 估算器（粗略估算，不做精确 tokenize）
# ─────────────────────────────────────────────────────

def estimate_tokens(messages: List[dict]) -> int:
    """估算输入 token 数。

    中文 ~1.5 chars/token, English ~4 chars/token.
    这是粗估，实际以模型返回的 usage 为准。
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content)
        total_chars += len(str(content))

    # 混合中英文，按 ~2 chars/token 估算
    return max(1, int(total_chars / 2) + len(messages) * 4)


# ─────────────────────────────────────────────────────
# 语义缓存（内存 LRU）
# ─────────────────────────────────────────────────────

class SemanticCache:
    """简单的语义缓存：对低风险场景的相同请求返回缓存结果。

    使用 SHA256 做精确匹配（第一版），后续可升级为向量相似度缓存。
    """

    def __init__(self, max_size: int = 500, ttl_seconds: int = 3600):
        self._cache: Dict[str, tuple] = {}   # key → (response_str, timestamp)
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _make_key(self, scene: str, messages: List[dict]) -> str:
        raw = json.dumps({"scene": scene, "messages": messages}, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, scene: str, messages: List[dict]) -> Optional[str]:
        if scene not in ("intent_classification", "sentiment_analysis"):
            # 只有低风险、确定性场景才缓存
            self._misses += 1
            return None

        with self._lock:
            key = self._make_key(scene, messages)
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            content, ts = entry
            if time.time() - ts > self._ttl:
                del self._cache[key]
                self._misses += 1
                return None
            self._hits += 1
            return content

    def put(self, scene: str, messages: List[dict], content: str):
        if scene not in ("intent_classification", "sentiment_analysis"):
            return

        with self._lock:
            key = self._make_key(scene, messages)
            if len(self._cache) >= self._max_size:
                # 简单 LRU：删除最旧的一个
                oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
                del self._cache[oldest_key]
            self._cache[key] = (content, time.time())

    @property
    def stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total * 100, 2) if total > 0 else 0.0,
        }


# ─────────────────────────────────────────────────────
# Token 预算管理器
# ─────────────────────────────────────────────────────

class TokenBudgetManager:
    """Token 预算管理：按租户/场景做粗粒度配额控制。"""

    def __init__(self):
        self._daily_usage: Dict[str, int] = {}   # tenant_id → tokens_today
        self._limits: Dict[str, int] = {
            "free":     50_000,    # 免费用户每日 50K tokens
            "premium":  500_000,   # 付费用户每日 500K
            "internal": 10_000_000,
        }
        self._lock = threading.Lock()

    def check_budget(self, tenant_id: str, estimated_tokens: int) -> bool:
        with self._lock:
            limit = self._limits.get(tenant_id, self._limits["free"])
            used = self._daily_usage.get(tenant_id, 0)
            return (used + estimated_tokens) <= limit

    def record_usage(self, tenant_id: str, tokens: int):
        with self._lock:
            self._daily_usage[tenant_id] = self._daily_usage.get(tenant_id, 0) + tokens

    def get_usage(self, tenant_id: str) -> int:
        return self._daily_usage.get(tenant_id, 0)


# ─────────────────────────────────────────────────────
# LLM Gateway 主类
# ─────────────────────────────────────────────────────

class LLMGateway:
    """LLM Gateway — 统一模型接入层。

    请求生命周期：
    1. 语义缓存检查 → 命中则直接返回
    2. Token 预算检查 → 超预算拒绝
    3. 路由决策（scene + tenant → 选 tier → 选模型）
    4. 调用模型（带重试）
    5. Fallback 链（失败 → 下一个模型）
    6. 记录 usage / cost / trace

    面试亮点：
    - 多模型路由（按场景/租户分级，小模型省钱）
    - Fallback 链（主模型 429/超时 → 切备用）
    - Token 预算（估算 + 配额控制）
    - 语义缓存（低风险场景直接命中）
    - 成本归因（每次调用记录 route_reason / cost / latency）
    """

    def __init__(self, models: Optional[List[ModelProfile]] = None):
        self._models = {m.name: m for m in (models or DEFAULT_MODELS) if m.enabled}
        self._cache = SemanticCache()
        self._budget = TokenBudgetManager()
        self._call_log: List[Dict[str, Any]] = []  # 最近 N 次调用记录
        self._log_max = 10_000
        self._lock = threading.Lock()
        # Phase A: per-user/per-tenant rate limiting
        self._rate_limits: Dict[str, list] = defaultdict(list)  # user_key -> [timestamps]
        self._rate_limit_window = 60  # seconds
        self._rate_limit_max_requests = 60  # requests per window

    def _check_rate_limit(self, tenant_id: str, user_id: Optional[str] = None) -> bool:
        """检查用户/租户是否超出限流配额 (Phase A).
        
        Returns True if request is allowed, False if rate limited.
        """
        now = time.time()
        user_key = f"{tenant_id}:{user_id}" if user_id else tenant_id
        
        with self._lock:
            # Clean old entries outside the window
            self._rate_limits[user_key] = [
                ts for ts in self._rate_limits[user_key]
                if now - ts < self._rate_limit_window
            ]
            
            # Check if within quota
            if len(self._rate_limits[user_key]) >= self._rate_limit_max_requests:
                return False
            
            # Record this request
            self._rate_limits[user_key].append(now)
            return True

    def get_rate_limit_info(self, tenant_id: str = None, user_id: str = None) -> dict:
        """Get rate limit info for a user/tenant (Phase A)."""
        if tenant_id or user_id:
            user_key = f"{tenant_id}:{user_id}" if user_id else tenant_id
            now = time.time()
            current_count = sum(
                1 for ts in self._rate_limits.get(user_key, [])
                if now - ts < self._rate_limit_window
            )
            return {
                "user_key": user_key,
                "current_requests": current_count,
                "max_requests": self._rate_limit_max_requests,
                "window_seconds": self._rate_limit_window,
                "remaining": max(0, self._rate_limit_max_requests - current_count),
            }
        
        # Return summary of all users
        return {
            "total_tracked_users": len(self._rate_limits),
            "window_seconds": self._rate_limit_window,
            "max_requests_per_window": self._rate_limit_max_requests,
        }

    def _get_model_by_tier(self, tier: str) -> Optional[ModelProfile]:
        """按 tier 层级获取可用模型。"""
        for m in self._models.values():
            if m.tier == tier and m.enabled:
                return m
        return None

    def _route(self, scene: str, tenant_id: str) -> List[tuple]:
        """路由决策：返回 [(model_profile, route_reason), ...] 候选列表。"""
        rule = ROUTE_RULES.get(scene, ROUTE_RULES["default"])
        tier_whitelist = TENANT_TIER_ALLOWLIST.get(tenant_id, ["flagship", "balanced"])

        candidates = []
        for tier in rule["tiers"]:
            if tier not in tier_whitelist:
                continue
            model = self._get_model_by_tier(tier)
            if model:
                reason = f"scene={scene},tier={tier},tenant={tenant_id}"
                candidates.append((model, reason))

        # 兜底：如果没有候选，用第一个可用模型
        if not candidates:
            for m in self._models.values():
                if m.enabled:
                    candidates.append((m, f"fallback_no_rule,scene={scene}"))
                    break

        return candidates

    def _call_model(self, model: ModelProfile, messages: List[dict],
                     temperature: float, max_tokens: int) -> str:
        """调用单个模型（带重试）。"""
        import requests

        if not model.api_key:
            raise ValueError(
                f"API key for model '{model.name}' (provider '{model.provider}') is empty. "
                "Set the LLM_API_KEY environment variable before calling the gateway."
            )

        url = f"{model.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {model.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model.model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_error = None
        for attempt in range(3):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=120)
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"].get("content", "").strip()
                    usage = data.get("usage", {})
                    return content, usage

                # 可重试错误
                if resp.status_code in (429, 500, 502, 503, 504):
                    delay = min(30, 1.0 * (2 ** attempt))
                    logger.warning(f"Model {model.name} HTTP {resp.status_code}, retry in {delay:.1f}s")
                    time.sleep(delay)
                    continue

                resp.raise_for_status()

            except requests.exceptions.RequestException as e:
                last_error = e
                time.sleep(min(30, 1.0 * (2 ** attempt)))
                continue

        raise RuntimeError(f"Model {model.name} failed after retries: {last_error}") from last_error

    def chat(self, req: GatewayRequest) -> GatewayResponse:
        """主入口：处理一次 LLM 请求。"""
        start_time = time.time()

        # ── Step 1: 语义缓存 ──────────────────────────────
        cached = self._cache.get(req.scene, req.messages)
        if cached is not None:
            return GatewayResponse(
                content=cached, model_used="cache", provider="cache",
                input_tokens=0, output_tokens=0, cost=0.0,
                latency_ms=(time.time() - start_time) * 1000,
                fallback_used=False, route_reason="cache_hit", cache_hit=True,
            )

        # ── Step 2: Token 预算 ────────────────────────────
        est_input = estimate_tokens(req.messages)
        if not self._budget.check_budget(req.tenant_id, est_input):
            raise RuntimeError(
                f"Token budget exceeded for tenant '{req.tenant_id}'. "
                f"Estimated: {est_input} tokens."
            )

        # ── Step 3: 路由决策 ──────────────────────────────
        candidates = self._route(req.scene, req.tenant_id)
        if not candidates:
            raise RuntimeError("No available models")

        max_out = req.max_output_tokens or ROUTE_RULES.get(req.scene, ROUTE_RULES["default"])["max_output"]

        # ── Step 4+5: Fallback 链 ────────────────────────
        last_error = None
        fallback_used = False
        final_model = None
        final_reason = ""
        final_content = ""
        final_usage = {}

        for i, (model, reason) in enumerate(candidates):
            if i > 0:
                fallback_used = True
                logger.warning(f"Fallback to {model.name} (reason: {reason})")

            try:
                final_content, final_usage = self._call_model(
                    model, req.messages, req.temperature, max_out
                )
                final_model = model
                final_reason = reason
                break

            except Exception as e:
                last_error = e
                logger.error(f"Model {model.name} failed: {e}")
                continue

        if final_model is None:
            raise RuntimeError(
                f"All models in fallback chain failed. Last error: {last_error}"
            ) from last_error

        # ── Step 6: 记录 usage / cost / trace ─────────────
        actual_input = final_usage.get("prompt_tokens", est_input)
        actual_output = final_usage.get("completion_tokens", max(len(final_content) // 2, 1))
        latency_ms = (time.time() - start_time) * 1000

        cost = (
            actual_input * final_model.input_cost_per_m / 1_000_000 +
            actual_output * final_model.output_cost_per_m / 1_000_000
        )

        # 记录预算
        self._budget.record_usage(req.tenant_id, actual_input + actual_output)

        # 写入缓存（低风险场景）
        if req.scene in ("intent_classification", "sentiment_analysis"):
            self._cache.put(req.scene, req.messages, final_content)

        # 记录 trace
        trace = {
            "timestamp": time.time(),
            "scene": req.scene,
            "tenant_id": req.tenant_id,
            "model_used": final_model.name,
            "provider": final_model.provider,
            "route_reason": final_reason,
            "input_tokens": actual_input,
            "output_tokens": actual_output,
            "cost": cost,
            "latency_ms": round(latency_ms, 2),
            "fallback_used": fallback_used,
        }

        with self._lock:
            self._call_log.append(trace)
            if len(self._call_log) > self._log_max:
                self._call_log = self._call_log[-self._log_max:]

        return GatewayResponse(
            content=final_content,
            model_used=final_model.name,
            provider=final_model.provider,
            input_tokens=actual_input,
            output_tokens=actual_output,
            cost=cost,
            latency_ms=latency_ms,
            fallback_used=fallback_used,
            route_reason=final_reason,
        )

    # ── 便捷方法 ──────────────────────────────────────────

    def chat_simple(
        self,
        messages: List[dict],
        scene: str = "default",
        tenant_id: str = "",
        **kwargs,
    ) -> GatewayResponse:
        """简化入口，直接传 messages。"""
        req = GatewayRequest(messages=messages, scene=scene, tenant_id=tenant_id, **kwargs)
        return self.chat(req)

    def chat_text(
        self,
        messages: List[dict],
        scene: str = "default",
        tenant_id: str = "",
        **kwargs,
    ) -> str:
        """只返回文本内容（兼容旧 LLMClient 调用方式）。"""
        return self.chat_simple(messages, scene=scene, tenant_id=tenant_id, **kwargs).content

    # ── 观测接口 ──────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """获取网关统计信息。"""
        total_calls = len(self._call_log)
        if total_calls == 0:
            return {"total_calls": 0}

        total_cost = sum(t["cost"] for t in self._call_log)
        avg_latency = sum(t["latency_ms"] for t in self._call_log) / total_calls
        fallback_count = sum(1 for t in self._call_log if t["fallback_used"])

        # 按场景统计
        by_scene: Dict[str, int] = {}
        by_model: Dict[str, int] = {}
        for t in self._call_log:
            by_scene[t["scene"]] = by_scene.get(t["scene"], 0) + 1
            by_model[t["model_used"]] = by_model.get(t["model_used"], 0) + 1

        return {
            "total_calls": total_calls,
            "total_cost": round(total_cost, 6),
            "avg_latency_ms": round(avg_latency, 2),
            "fallback_count": fallback_count,
            "fallback_rate": round(fallback_count / total_calls * 100, 2),
            "by_scene": by_scene,
            "by_model": by_model,
            "cache": self._cache.stats,
        }

    def get_recent_calls(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取最近 N 次调用记录。"""
        with self._lock:
            return self._call_log[-limit:]


# ─────────────────────────────────────────────────────
# 全局单例
# ─────────────────────────────────────────────────────

_gateway_instance: Optional[LLMGateway] = None


def get_llm_gateway() -> LLMGateway:
    """获取全局 LLM Gateway 实例。"""
    global _gateway_instance
    if _gateway_instance is None:
        _gateway_instance = LLMGateway()
    return _gateway_instance
