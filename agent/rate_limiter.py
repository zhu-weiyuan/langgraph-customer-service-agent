<<<<<<< HEAD
# -*- coding: utf-8 -*-
"""
分层限流器（P1-B 重写版）— 全局 / IP / 用户 / 会话 四层令牌桶

设计：
1. **RedisTokenBucketLimiter** — redis.asyncio + 内嵌 Lua 脚本，一次 EVAL 原子
   检查并扣减全部四层令牌桶（任何一层不足则整体不扣减，返回触发层与 retry_after）。
2. **fail-closed 降级** — Redis 不可用时降级到 LocalConservativeLimiter
   （线程安全滑动窗口，限额取正常值的 50%，窗口数据定期清理防内存泄漏），
   并通过 degrade_callback 上报降级指标。绝不因 Redis 挂掉而放开全部流量。
3. **并发闸门** — asyncio.Semaphore 真实 acquire/release：
       async with limiter.concurrency():
           ... call model ...
   （删除旧版"读 Semaphore._value 对比自增计数器"的假信号量。）
4. **统一异常** — RateLimitExceeded(layer, retry_after)。

三方依赖策略：redis 延迟导入 + 守卫；纯 stdlib 环境可 import 本模块并使用
LocalConservativeLimiter / 注入 Fake Redis 测试。
"""

import asyncio
import contextlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# 异常
# ─────────────────────────────────────────────────────

class RateLimitExceeded(Exception):
    """统一限流异常：带触发层级与建议重试等待时间（秒）。"""

    def __init__(self, layer: str, retry_after: float, message: Optional[str] = None):
        self.layer = layer
        self.retry_after = max(0.0, float(retry_after))
        super().__init__(
            message or f"Rate limit exceeded at layer '{layer}', "
                       f"retry after {self.retry_after:.1f}s")


# 兼容旧代码的别名（旧文件抛 RateLimitError）
RateLimitError = RateLimitExceeded


# ─────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class BucketConfig:
    """令牌桶配置：rate 每秒补充令牌数，capacity 桶容量（突发上限）。"""
    rate: float
    capacity: int


# 四层默认限额（全局 / 单 IP / 单用户 / 单会话）
DEFAULT_BUCKETS: Dict[str, BucketConfig] = {
    # The product target is about 333 requests/s at 10,000 users sending
    # once per 30 seconds. Redis shares this budget across workers/replicas.
    "global":  BucketConfig(rate=500.0, capacity=1000),
    # IP is an abuse guard, not the product capacity limit. A higher value
    # avoids rejecting legitimate users behind a NAT gateway.
    "ip":      BucketConfig(rate=500.0, capacity=1000),
    "user":    BucketConfig(rate=2.0,   capacity=10),
    "session": BucketConfig(rate=1.0,   capacity=5),
}


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    # A zero refill rate would permanently deny a bucket after its burst is
    # consumed, which is almost always a configuration mistake.  Keep the
    # service on the safe built-in value instead of accepting it.
    return value if value > 0 else default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def rate_limit_config_from_env() -> Tuple[Dict[str, BucketConfig], int]:
    """Build limiter settings from environment without changing safe defaults.

    The values are deliberately per-process settings.  When several workers or
    replicas are deployed, the Redis buckets still provide the shared global
    budget while ``MAX_CONCURRENT_REQUESTS`` limits each worker's in-flight
    model work.
    """
    buckets = {
        layer: BucketConfig(
            rate=_env_float(f"RATE_LIMIT_{layer.upper()}_RATE", cfg.rate),
            capacity=_env_int(
                f"RATE_LIMIT_{layer.upper()}_CAPACITY", cfg.capacity, minimum=1),
        )
        for layer, cfg in DEFAULT_BUCKETS.items()
    }
    max_concurrency = _env_int("MAX_CONCURRENT_REQUESTS", 10)
    return buckets, max_concurrency

LAYER_ORDER = ("global", "ip", "user", "session")


# ─────────────────────────────────────────────────────
# Lua 脚本：一次原子检查并扣减多个令牌桶
# ─────────────────────────────────────────────────────
#
# KEYS[i]         第 i 个桶的 Redis key（hash: tokens, ts）
# ARGV[1]         now（秒，浮点）
# ARGV[2]         cost（本次扣减令牌数）
# ARGV[2+i*2-1]   第 i 个桶的 rate
# ARGV[2+i*2]     第 i 个桶的 capacity
#
# 返回：
#   {1}                     全部通过并已扣减
#   {0, i, retry_after_ms}  第 i 个桶（1-based）不足，未扣减任何桶
MULTI_BUCKET_LUA = """
local now = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
local n = #KEYS
local tokens = {}

-- Pass 1: 补充令牌并检查全部桶
for i = 1, n do
    local rate = tonumber(ARGV[1 + i * 2])
    local capacity = tonumber(ARGV[2 + i * 2])
    local bucket = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
    local cur = tonumber(bucket[1])
    local ts = tonumber(bucket[2])
    if cur == nil then
        cur = capacity
        ts = now
    end
    local elapsed = math.max(0, now - ts)
    cur = math.min(capacity, cur + elapsed * rate)
    if cur < cost then
        local retry_after = 0
        if rate > 0 then
            retry_after = (cost - cur) / rate
        end
        return {0, i, math.ceil(retry_after * 1000)}
    end
    tokens[i] = cur
end

-- Pass 2: 全部通过，统一扣减
for i = 1, n do
    local rate = tonumber(ARGV[1 + i * 2])
    local capacity = tonumber(ARGV[2 + i * 2])
    local ttl = 60
    if rate > 0 then
        ttl = math.ceil(capacity / rate) + 60
    end
    redis.call('HSET', KEYS[i], 'tokens', tokens[i] - cost, 'ts', now)
    redis.call('EXPIRE', KEYS[i], ttl)
end
return {1}
"""


# ─────────────────────────────────────────────────────
# 本地保守限流器（fail-closed 降级路径）
# ─────────────────────────────────────────────────────

class LocalConservativeLimiter:
    """线程安全滑动窗口限流器 — Redis 不可用时的保守降级实现。

    - 限额取正常值的 conservative_factor（默认 50%）：单实例视角看不到集群
      总量，收紧限额避免 Redis 故障期间超卖。
    - 窗口数据定期整体清理（cleanup_interval），防止 key 集合只增不减导致
      内存泄漏（旧版仅清理被再次访问的 key）。
    """

    def __init__(self,
                 buckets: Optional[Dict[str, BucketConfig]] = None,
                 window_seconds: float = 10.0,
                 conservative_factor: float = 0.5,
                 cleanup_interval: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        base = buckets or DEFAULT_BUCKETS
        self._window = window_seconds
        self._clock = clock
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = clock()
        # 窗口内允许的请求数 = rate * window * factor（至少 1）
        self._limits: Dict[str, int] = {
            layer: max(1, int(cfg.rate * window_seconds * conservative_factor))
            for layer, cfg in base.items()
        }
        self._events: Dict[str, list] = {}   # key → [timestamps]
        self._lock = threading.Lock()

    def limit_for(self, layer: str) -> int:
        return self._limits.get(layer, 1)

    def _maybe_cleanup(self, now: float) -> None:
        """定期全量清理过期窗口数据，防内存泄漏。调用方需持锁。"""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        cutoff = now - self._window
        stale = []
        for key, events in self._events.items():
            fresh = [t for t in events if t > cutoff]
            if fresh:
                self._events[key] = fresh
            else:
                stale.append(key)
        for key in stale:
            del self._events[key]
        self._last_cleanup = now

    def acquire(self, layer_keys: Dict[str, str], cost: int = 1) -> None:
        """检查并记录所有层；任一层超限抛 RateLimitExceeded（不记录任何层）。"""
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            self._maybe_cleanup(now)
            # Pass 1: 检查
            for layer in LAYER_ORDER:
                if layer not in layer_keys:
                    continue
                key = f"{layer}:{layer_keys[layer]}"
                events = [t for t in self._events.get(key, []) if t > cutoff]
                self._events[key] = events
                limit = self.limit_for(layer)
                if len(events) + cost > limit:
                    retry_after = (events[0] + self._window - now) if events else self._window
                    raise RateLimitExceeded(
                        layer=layer, retry_after=max(0.1, retry_after),
                        message=f"[degraded/local] rate limit at '{layer}' "
                                f"({len(events)}/{limit} in {self._window}s window)")
            # Pass 2: 记录
            for layer in LAYER_ORDER:
                if layer not in layer_keys:
                    continue
                key = f"{layer}:{layer_keys[layer]}"
                self._events.setdefault(key, []).extend([now] * cost)

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._events)


# ─────────────────────────────────────────────────────
# Redis 四层令牌桶限流器（主路径）
# ─────────────────────────────────────────────────────

class RedisTokenBucketLimiter:
    """redis.asyncio + Lua 一次原子检查扣减四层令牌桶；Redis 故障时 fail-closed
    降级到 LocalConservativeLimiter（50% 限额），并回调降级指标。

    Usage:
        limiter = RedisTokenBucketLimiter()          # 或注入 redis_client（测试用 Fake）
        await limiter.acquire(user_id="u1", ip="1.2.3.4", session_id="s1")
        async with limiter.concurrency():
            ... call model ...
    """

    def __init__(self,
                 redis_client: Any = None,
                 redis_url: str = "redis://127.0.0.1:6379/0",
                 buckets: Optional[Dict[str, BucketConfig]] = None,
                 prefix: str = "rl",
                 max_concurrency: int = 10,
                 degrade_callback: Optional[Callable[[str], None]] = None,
                 local_limiter: Optional[LocalConservativeLimiter] = None,
                 clock: Callable[[], float] = time.time):
        self._buckets = dict(buckets or DEFAULT_BUCKETS)
        self._prefix = prefix
        self._clock = clock
        self._redis = redis_client
        self._redis_url = redis_url
        self._degrade_callback = degrade_callback
        self._local = local_limiter or LocalConservativeLimiter(buckets=self._buckets)
        self._degraded = False
        self._degraded_count = 0
        self._stats_lock = threading.Lock()
        # 并发闸门：真实的 asyncio.Semaphore acquire/release
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._active = 0

    # ── Redis 连接（延迟导入 + 守卫）─────────────────

    def _get_redis(self) -> Any:
        if self._redis is None:
            try:
                import redis.asyncio as aioredis   # 延迟导入
            except ImportError as e:
                raise ConnectionError("redis package not installed") from e
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    # ── 主入口 ────────────────────────────────────────

    def _layer_keys(self, user_id: Optional[str], ip: Optional[str],
                    session_id: Optional[str]) -> Dict[str, str]:
        keys = {"global": "all"}
        if ip:
            keys["ip"] = ip
        if user_id:
            keys["user"] = user_id
        if session_id:
            keys["session"] = session_id
        return keys

    async def acquire(self, user_id: Optional[str] = None, ip: Optional[str] = None,
                      session_id: Optional[str] = None, cost: int = 1) -> None:
        """检查并扣减四层令牌桶；超限抛 RateLimitExceeded。

        Redis 故障 → fail-closed：降级到本地保守限流（50% 限额）而非放行。
        """
        layer_keys = self._layer_keys(user_id, ip, session_id)
        try:
            await self._acquire_redis(layer_keys, cost)
            with self._stats_lock:
                self._degraded = False
        except RateLimitExceeded:
            raise
        except Exception as e:
            self._record_degrade(str(e))
            # fail-closed 降级：本地保守限流（同步、线程安全）
            self._local.acquire(layer_keys, cost)

    async def _acquire_redis(self, layer_keys: Dict[str, str], cost: int) -> None:
        redis_client = self._get_redis()
        layers = [layer for layer in LAYER_ORDER if layer in layer_keys]
        keys = [f"{self._prefix}:{layer}:{layer_keys[layer]}" for layer in layers]
        argv: list = [self._clock(), cost]
        for layer in layers:
            cfg = self._buckets[layer]
            argv.extend([cfg.rate, cfg.capacity])
        result = await redis_client.eval(MULTI_BUCKET_LUA, len(keys), *keys, *argv)
        # result: [1] 通过；[0, failing_index(1-based), retry_after_ms] 拒绝
        if int(result[0]) != 1:
            failing_layer = layers[int(result[1]) - 1]
            retry_after = float(result[2]) / 1000.0
            raise RateLimitExceeded(layer=failing_layer, retry_after=retry_after)

    def _record_degrade(self, reason: str) -> None:
        with self._stats_lock:
            first = not self._degraded
            self._degraded = True
            self._degraded_count += 1
        if first:
            logger.error(f"Redis rate limiter unavailable ({reason}); "
                         f"fail-closed degradation to local conservative limiter (50% limits)")
        if self._degrade_callback is not None:
            try:
                self._degrade_callback(reason)
            except Exception:
                logger.exception("degrade_callback raised")

    # ── 并发闸门（真实 Semaphore）─────────────────────

    @contextlib.asynccontextmanager
    async def concurrency(self, timeout: Optional[float] = None):
        """并发闸门：async with limiter.concurrency(): ...

        timeout 秒内拿不到并发额度抛 RateLimitExceeded(layer='concurrency')。
        """
        if timeout is None:
            await self._semaphore.acquire()
        else:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RateLimitExceeded(
                    layer="concurrency", retry_after=1.0,
                    message=f"Too many concurrent requests (max {self._max_concurrency})")
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()

    # ── 观测 ─────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            return {
                "degraded": self._degraded,
                "degraded_count": self._degraded_count,
                "active_concurrency": self._active,
                "max_concurrency": self._max_concurrency,
                "local_tracked_keys": self._local.tracked_keys(),
                "buckets": {layer: {"rate": cfg.rate, "capacity": cfg.capacity}
                            for layer, cfg in self._buckets.items()},
            }


class MultiLevelRateLimiter:
    """Synchronous compatibility adapter for the pre-P1 limiter API."""

    def __init__(self, user_max_requests=60, vendor_rate=100.0,
                 vendor_capacity=200, max_concurrent_requests=10, **_kwargs):
        self._max = max(1, int(max_concurrent_requests))
        self._active = 0
        self._lock = threading.Lock()
        self._user_max = max(1, int(user_max_requests))
        self._users: Dict[str, int] = {}

    def acquire(self, user_id: str) -> None:
        with self._lock:
            if self._active >= self._max:
                raise RateLimitError("concurrency", 1.0, "系统繁忙，正在处理的请求过多")
            if self._users.get(user_id, 0) >= self._user_max:
                raise RateLimitError("user", 1.0, "用户请求过多")
            self._active += 1
            self._users[user_id] = self._users.get(user_id, 0) + 1

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1

    def get_stats(self) -> Dict[str, int]:
        return {"active_requests": self._active,
                "max_concurrent_requests": self._max,
                "available_concurrency": self._max - self._active}


# ─────────────────────────────────────────────────────
# 全局实例（延迟创建，避免 import 即建 Semaphore 绑定错误事件循环）
# ─────────────────────────────────────────────────────

_rate_limiter: Optional[RedisTokenBucketLimiter] = None
_rate_limiter_lock = threading.Lock()


def get_rate_limiter() -> RedisTokenBucketLimiter:
    """获取全局限流器实例。"""
    global _rate_limiter
    if _rate_limiter is None:
        with _rate_limiter_lock:
            if _rate_limiter is None:
                buckets, max_concurrency = rate_limit_config_from_env()
                _rate_limiter = RedisTokenBucketLimiter(
                    redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                    buckets=buckets, max_concurrency=max_concurrency)
    return _rate_limiter
=======
# -*- coding: utf-8 -*-
"""
分层限流器（P1-B 重写版）— 全局 / IP / 用户 / 会话 四层令牌桶

设计：
1. **RedisTokenBucketLimiter** — redis.asyncio + 内嵌 Lua 脚本，一次 EVAL 原子
   检查并扣减全部四层令牌桶（任何一层不足则整体不扣减，返回触发层与 retry_after）。
2. **fail-closed 降级** — Redis 不可用时降级到 LocalConservativeLimiter
   （线程安全滑动窗口，限额取正常值的 50%，窗口数据定期清理防内存泄漏），
   并通过 degrade_callback 上报降级指标。绝不因 Redis 挂掉而放开全部流量。
3. **并发闸门** — asyncio.Semaphore 真实 acquire/release：
       async with limiter.concurrency():
           ... call model ...
   （删除旧版"读 Semaphore._value 对比自增计数器"的假信号量。）
4. **统一异常** — RateLimitExceeded(layer, retry_after)。

三方依赖策略：redis 延迟导入 + 守卫；纯 stdlib 环境可 import 本模块并使用
LocalConservativeLimiter / 注入 Fake Redis 测试。
"""

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# 异常
# ─────────────────────────────────────────────────────

class RateLimitExceeded(Exception):
    """统一限流异常：带触发层级与建议重试等待时间（秒）。"""

    def __init__(self, layer: str, retry_after: float, message: Optional[str] = None):
        self.layer = layer
        self.retry_after = max(0.0, float(retry_after))
        super().__init__(
            message or f"Rate limit exceeded at layer '{layer}', "
                       f"retry after {self.retry_after:.1f}s")


# 兼容旧代码的别名（旧文件抛 RateLimitError）
RateLimitError = RateLimitExceeded


# ─────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class BucketConfig:
    """令牌桶配置：rate 每秒补充令牌数，capacity 桶容量（突发上限）。"""
    rate: float
    capacity: int


# 四层默认限额（全局 / 单 IP / 单用户 / 单会话）
DEFAULT_BUCKETS: Dict[str, BucketConfig] = {
    "global":  BucketConfig(rate=100.0, capacity=200),
    "ip":      BucketConfig(rate=5.0,   capacity=20),
    "user":    BucketConfig(rate=2.0,   capacity=10),
    "session": BucketConfig(rate=1.0,   capacity=5),
}

LAYER_ORDER = ("global", "ip", "user", "session")


# ─────────────────────────────────────────────────────
# Lua 脚本：一次原子检查并扣减多个令牌桶
# ─────────────────────────────────────────────────────
#
# KEYS[i]         第 i 个桶的 Redis key（hash: tokens, ts）
# ARGV[1]         now（秒，浮点）
# ARGV[2]         cost（本次扣减令牌数）
# ARGV[2+i*2-1]   第 i 个桶的 rate
# ARGV[2+i*2]     第 i 个桶的 capacity
#
# 返回：
#   {1}                     全部通过并已扣减
#   {0, i, retry_after_ms}  第 i 个桶（1-based）不足，未扣减任何桶
MULTI_BUCKET_LUA = """
local now = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
local n = #KEYS
local tokens = {}

-- Pass 1: 补充令牌并检查全部桶
for i = 1, n do
    local rate = tonumber(ARGV[1 + i * 2])
    local capacity = tonumber(ARGV[2 + i * 2])
    local bucket = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
    local cur = tonumber(bucket[1])
    local ts = tonumber(bucket[2])
    if cur == nil then
        cur = capacity
        ts = now
    end
    local elapsed = math.max(0, now - ts)
    cur = math.min(capacity, cur + elapsed * rate)
    if cur < cost then
        local retry_after = 0
        if rate > 0 then
            retry_after = (cost - cur) / rate
        end
        return {0, i, math.ceil(retry_after * 1000)}
    end
    tokens[i] = cur
end

-- Pass 2: 全部通过，统一扣减
for i = 1, n do
    local rate = tonumber(ARGV[1 + i * 2])
    local capacity = tonumber(ARGV[2 + i * 2])
    local ttl = 60
    if rate > 0 then
        ttl = math.ceil(capacity / rate) + 60
    end
    redis.call('HSET', KEYS[i], 'tokens', tokens[i] - cost, 'ts', now)
    redis.call('EXPIRE', KEYS[i], ttl)
end
return {1}
"""


# ─────────────────────────────────────────────────────
# 本地保守限流器（fail-closed 降级路径）
# ─────────────────────────────────────────────────────

class LocalConservativeLimiter:
    """线程安全滑动窗口限流器 — Redis 不可用时的保守降级实现。

    - 限额取正常值的 conservative_factor（默认 50%）：单实例视角看不到集群
      总量，收紧限额避免 Redis 故障期间超卖。
    - 窗口数据定期整体清理（cleanup_interval），防止 key 集合只增不减导致
      内存泄漏（旧版仅清理被再次访问的 key）。
    """

    def __init__(self,
                 buckets: Optional[Dict[str, BucketConfig]] = None,
                 window_seconds: float = 10.0,
                 conservative_factor: float = 0.5,
                 cleanup_interval: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        base = buckets or DEFAULT_BUCKETS
        self._window = window_seconds
        self._clock = clock
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = clock()
        # 窗口内允许的请求数 = rate * window * factor（至少 1）
        self._limits: Dict[str, int] = {
            layer: max(1, int(cfg.rate * window_seconds * conservative_factor))
            for layer, cfg in base.items()
        }
        self._events: Dict[str, list] = {}   # key → [timestamps]
        self._lock = threading.Lock()

    def limit_for(self, layer: str) -> int:
        return self._limits.get(layer, 1)

    def _maybe_cleanup(self, now: float) -> None:
        """定期全量清理过期窗口数据，防内存泄漏。调用方需持锁。"""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        cutoff = now - self._window
        stale = []
        for key, events in self._events.items():
            fresh = [t for t in events if t > cutoff]
            if fresh:
                self._events[key] = fresh
            else:
                stale.append(key)
        for key in stale:
            del self._events[key]
        self._last_cleanup = now

    def acquire(self, layer_keys: Dict[str, str], cost: int = 1) -> None:
        """检查并记录所有层；任一层超限抛 RateLimitExceeded（不记录任何层）。"""
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            self._maybe_cleanup(now)
            # Pass 1: 检查
            for layer in LAYER_ORDER:
                if layer not in layer_keys:
                    continue
                key = f"{layer}:{layer_keys[layer]}"
                events = [t for t in self._events.get(key, []) if t > cutoff]
                self._events[key] = events
                limit = self.limit_for(layer)
                if len(events) + cost > limit:
                    retry_after = (events[0] + self._window - now) if events else self._window
                    raise RateLimitExceeded(
                        layer=layer, retry_after=max(0.1, retry_after),
                        message=f"[degraded/local] rate limit at '{layer}' "
                                f"({len(events)}/{limit} in {self._window}s window)")
            # Pass 2: 记录
            for layer in LAYER_ORDER:
                if layer not in layer_keys:
                    continue
                key = f"{layer}:{layer_keys[layer]}"
                self._events.setdefault(key, []).extend([now] * cost)

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._events)


# ─────────────────────────────────────────────────────
# Redis 四层令牌桶限流器（主路径）
# ─────────────────────────────────────────────────────

class RedisTokenBucketLimiter:
    """redis.asyncio + Lua 一次原子检查扣减四层令牌桶；Redis 故障时 fail-closed
    降级到 LocalConservativeLimiter（50% 限额），并回调降级指标。

    Usage:
        limiter = RedisTokenBucketLimiter()          # 或注入 redis_client（测试用 Fake）
        await limiter.acquire(user_id="u1", ip="1.2.3.4", session_id="s1")
        async with limiter.concurrency():
            ... call model ...
    """

    def __init__(self,
                 redis_client: Any = None,
                 redis_url: str = "redis://127.0.0.1:6379/0",
                 buckets: Optional[Dict[str, BucketConfig]] = None,
                 prefix: str = "rl",
                 max_concurrency: int = 10,
                 degrade_callback: Optional[Callable[[str], None]] = None,
                 local_limiter: Optional[LocalConservativeLimiter] = None,
                 clock: Callable[[], float] = time.time):
        self._buckets = dict(buckets or DEFAULT_BUCKETS)
        self._prefix = prefix
        self._clock = clock
        self._redis = redis_client
        self._redis_url = redis_url
        self._degrade_callback = degrade_callback
        self._local = local_limiter or LocalConservativeLimiter(buckets=self._buckets)
        self._degraded = False
        self._degraded_count = 0
        self._stats_lock = threading.Lock()
        # 并发闸门：真实的 asyncio.Semaphore acquire/release
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._active = 0

    # ── Redis 连接（延迟导入 + 守卫）─────────────────

    def _get_redis(self) -> Any:
        if self._redis is None:
            try:
                import redis.asyncio as aioredis   # 延迟导入
            except ImportError as e:
                raise ConnectionError("redis package not installed") from e
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    # ── 主入口 ────────────────────────────────────────

    def _layer_keys(self, user_id: Optional[str], ip: Optional[str],
                    session_id: Optional[str]) -> Dict[str, str]:
        keys = {"global": "all"}
        if ip:
            keys["ip"] = ip
        if user_id:
            keys["user"] = user_id
        if session_id:
            keys["session"] = session_id
        return keys

    async def acquire(self, user_id: Optional[str] = None, ip: Optional[str] = None,
                      session_id: Optional[str] = None, cost: int = 1) -> None:
        """检查并扣减四层令牌桶；超限抛 RateLimitExceeded。

        Redis 故障 → fail-closed：降级到本地保守限流（50% 限额）而非放行。
        """
        layer_keys = self._layer_keys(user_id, ip, session_id)
        try:
            await self._acquire_redis(layer_keys, cost)
            with self._stats_lock:
                self._degraded = False
        except RateLimitExceeded:
            raise
        except Exception as e:
            self._record_degrade(str(e))
            # fail-closed 降级：本地保守限流（同步、线程安全）
            self._local.acquire(layer_keys, cost)

    async def _acquire_redis(self, layer_keys: Dict[str, str], cost: int) -> None:
        redis_client = self._get_redis()
        layers = [layer for layer in LAYER_ORDER if layer in layer_keys]
        keys = [f"{self._prefix}:{layer}:{layer_keys[layer]}" for layer in layers]
        argv: list = [self._clock(), cost]
        for layer in layers:
            cfg = self._buckets[layer]
            argv.extend([cfg.rate, cfg.capacity])
        result = await redis_client.eval(MULTI_BUCKET_LUA, len(keys), *keys, *argv)
        # result: [1] 通过；[0, failing_index(1-based), retry_after_ms] 拒绝
        if int(result[0]) != 1:
            failing_layer = layers[int(result[1]) - 1]
            retry_after = float(result[2]) / 1000.0
            raise RateLimitExceeded(layer=failing_layer, retry_after=retry_after)

    def _record_degrade(self, reason: str) -> None:
        with self._stats_lock:
            first = not self._degraded
            self._degraded = True
            self._degraded_count += 1
        if first:
            logger.error(f"Redis rate limiter unavailable ({reason}); "
                         f"fail-closed degradation to local conservative limiter (50% limits)")
        if self._degrade_callback is not None:
            try:
                self._degrade_callback(reason)
            except Exception:
                logger.exception("degrade_callback raised")

    # ── 并发闸门（真实 Semaphore）─────────────────────

    @contextlib.asynccontextmanager
    async def concurrency(self, timeout: Optional[float] = None):
        """并发闸门：async with limiter.concurrency(): ...

        timeout 秒内拿不到并发额度抛 RateLimitExceeded(layer='concurrency')。
        """
        if timeout is None:
            await self._semaphore.acquire()
        else:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RateLimitExceeded(
                    layer="concurrency", retry_after=1.0,
                    message=f"Too many concurrent requests (max {self._max_concurrency})")
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()

    # ── 观测 ─────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            return {
                "degraded": self._degraded,
                "degraded_count": self._degraded_count,
                "active_concurrency": self._active,
                "max_concurrency": self._max_concurrency,
                "local_tracked_keys": self._local.tracked_keys(),
                "buckets": {layer: {"rate": cfg.rate, "capacity": cfg.capacity}
                            for layer, cfg in self._buckets.items()},
            }


class MultiLevelRateLimiter:
    """Synchronous compatibility adapter for the pre-P1 limiter API."""

    def __init__(self, user_max_requests=60, vendor_rate=100.0,
                 vendor_capacity=200, max_concurrent_requests=10, **_kwargs):
        self._max = max(1, int(max_concurrent_requests))
        self._active = 0
        self._lock = threading.Lock()
        self._user_max = max(1, int(user_max_requests))
        self._users: Dict[str, int] = {}

    def acquire(self, user_id: str) -> None:
        with self._lock:
            if self._active >= self._max:
                raise RateLimitError("concurrency", 1.0, "系统繁忙，正在处理的请求过多")
            if self._users.get(user_id, 0) >= self._user_max:
                raise RateLimitError("user", 1.0, "用户请求过多")
            self._active += 1
            self._users[user_id] = self._users.get(user_id, 0) + 1

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1

    def get_stats(self) -> Dict[str, int]:
        return {"active_requests": self._active,
                "max_concurrent_requests": self._max,
                "available_concurrency": self._max - self._active}


# ─────────────────────────────────────────────────────
# 全局实例（延迟创建，避免 import 即建 Semaphore 绑定错误事件循环）
# ─────────────────────────────────────────────────────

_rate_limiter: Optional[RedisTokenBucketLimiter] = None
_rate_limiter_lock = threading.Lock()


def get_rate_limiter() -> RedisTokenBucketLimiter:
    """获取全局限流器实例。"""
    global _rate_limiter
    if _rate_limiter is None:
        with _rate_limiter_lock:
            if _rate_limiter is None:
                import os
                _rate_limiter = RedisTokenBucketLimiter(
                    redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    return _rate_limiter
>>>>>>> origin/master
