# -*- coding: utf-8 -*-
"""
分层限流器 — 用户级 / 租户级 / 模型级 / 供应商级

四层架构：
1. 用户级：防止单个用户滥用（滑动窗口）
2. 租户级：控制企业/项目套餐成本（令牌桶）
3. 模型级：避免热门模型被打满（令牌桶 + 并发信号量）
4. 供应商级：保护外部依赖（全局令牌桶 + 熔断器）

使用示例：
    limiter = MultiLevelRateLimiter()
    
    # 在请求进入时检查
    if not limiter.acquire(user_id="u123", model="mimo-v2.5"):
        raise RateLimitError("请求过多，请稍后重试")
    
    # 收到 429 时触发熔断
    limiter.record_vendor_429()
    
    # 检查是否已熔断
    if limiter.is_vendor_circuit_open():
        raise ServiceUnavailableError("供应商服务不可用")
"""

import time
import threading
import logging
from collections import defaultdict
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    """限流异常"""
    pass


class SlidingWindow:
    """滑动窗口限流器。

    在固定时间窗口内限制请求数量。

    Args:
        max_requests: 最大请求数
        window_seconds: 窗口大小（秒）
    """
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests: Dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        """检查是否允许请求。

        Args:
            key: 限流键（如用户ID、IP等）

        Returns:
            True 如果允许，False 否则
        """
        now = time.time()
        cutoff = now - self.window

        with self._lock:
            # 清理过期请求
            self._requests[key] = [t for t in self._requests[key] if t > cutoff]
            
            # 检查是否超限
            if len(self._requests[key]) >= self.max_requests:
                logger.warning(f"Rate limit exceeded for {key}: {len(self._requests[key])}/{self.max_requests}")
                return False
            
            # 记录请求
            self._requests[key].append(now)
            return True

    def remaining(self, key: str) -> int:
        """查询剩余可用请求数。"""
        now = time.time()
        cutoff = now - self.window
        
        with self._lock:
            current = [t for t in self._requests[key] if t > cutoff]
            return max(0, self.max_requests - len(current))

    def reset(self, key: str) -> None:
        """重置指定键的限流状态。"""
        with self._lock:
            self._requests.pop(key, None)


class TokenBucket:
    """令牌桶限流器。

    允许一定程度的突发流量，但长期速率受限制。

    Args:
        rate: 每秒添加的令牌数
        capacity: 桶的最大容量（突发上限）
    """
    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last_refill = time.time()
        self._lock = threading.Lock()

    def is_allowed(self, tokens: int = 1) -> bool:
        """检查是否有足够令牌。

        Args:
            tokens: 需要的令牌数

        Returns:
            True 如果有足够令牌，False 否则
        """
        with self._lock:
            self._refill()
            
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            
            return False

    def _refill(self) -> None:
        """补充令牌。"""
        now = time.time()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self.rate
        
        if new_tokens > 0:
            self._tokens = min(self.capacity, self._tokens + new_tokens)
            self._last_refill = now

    @property
    def available_tokens(self) -> int:
        """当前可用令牌数。"""
        with self._lock:
            self._refill()
            return int(self._tokens)


class CircuitBreaker:
    """熔断器。

    当供应商连续返回错误时，自动"熔断"一段时间，避免雪崩。

    Args:
        failure_threshold: 失败次数阈值（达到后熔断）
        recovery_timeout: 恢复等待时间（秒）
    """
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        
        self._failure_count = 0
        self._last_failure_time = 0
        self._state = "closed"  # closed / open / half-open
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        """检查是否允许请求。

        Returns:
            True 如果允许，False 否则（熔断中）
        """
        with self._lock:
            if self._state == "closed":
                return True
            
            if self._state == "open":
                # 检查是否到了恢复时间
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = "half-open"
                    logger.info("Circuit breaker transitioning to half-open")
                    return True
                return False
            
            # half-open: 允许一个请求测试
            return True

    def record_success(self) -> None:
        """记录成功请求。"""
        with self._lock:
            if self._state == "half-open":
                logger.info("Circuit breaker closed after successful request")
            self._failure_count = 0
            self._state = "closed"

    def record_failure(self) -> None:
        """记录失败请求。"""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            
            if self._failure_count >= self.failure_threshold:
                logger.warning(
                    f"Circuit breaker OPEN after {self._failure_count} failures "
                    f"(threshold={self.failure_threshold})"
                )
                self._state = "open"

    @property
    def state(self) -> str:
        """当前熔断器状态。"""
        return self._state


class MultiLevelRateLimiter:
    """分层限流器。

    四层架构：
    1. 用户级：滑动窗口（60 req/60s）
    2. 租户级：令牌桶（100 tokens/s，容量1000）
    3. 模型级：令牌桶 + 并发信号量
    4. 供应商级：全局令牌桶 + 熔断器

    Args:
        user_max_requests: 用户级最大请求数
        user_window_seconds: 用户级窗口大小（秒）
        vendor_rate: 供应商级每秒令牌数
        vendor_capacity: 供应商级令牌桶容量
        vendor_failure_threshold: 供应商熔断失败阈值
        vendor_recovery_timeout: 供应商恢复等待时间（秒）
    """
    def __init__(
        self,
        user_max_requests: int = 60,
        user_window_seconds: float = 60.0,
        vendor_rate: float = 100.0,
        vendor_capacity: int = 1000,
        vendor_failure_threshold: int = 5,
        vendor_recovery_timeout: float = 60.0,
    ):
        # 用户级限流（滑动窗口）
        self.user_limiter = SlidingWindow(user_max_requests, user_window_seconds)
        
        # 供应商级限流（令牌桶 + 熔断器）
        self.vendor_limiter = TokenBucket(vendor_rate, vendor_capacity)
        self.vendor_circuit_breaker = CircuitBreaker(
            failure_threshold=vendor_failure_threshold,
            recovery_timeout=vendor_recovery_timeout,
        )
        
        # 模型级限流（按模型名隔离）
        self.model_limiters: Dict[str, TokenBucket] = {}
        self._model_lock = threading.Lock()
        
        # 并发信号量（限制同时进行的请求数）
        self._concurrent_semaphore = threading.Semaphore(10)  # 最多10个并发
        self._active_requests = 0
        self._concurrent_lock = threading.Lock()

    def acquire(self, user_id: str, model: str = "default") -> bool:
        """检查所有层级限流。

        Args:
            user_id: 用户ID
            model: 模型名称

        Returns:
            True 如果所有层级都允许，False 否则

        Raises:
            RateLimitError: 如果任何一层被限流
        """
        # ① 检查供应商熔断器
        if not self.vendor_circuit_breaker.is_allowed():
            raise RateLimitError("供应商服务不可用（熔断中），请稍后重试")
        
        # ② 用户级限流
        if not self.user_limiter.is_allowed(user_id):
            remaining = self.user_limiter.remaining(user_id)
            raise RateLimitError(
                f"请求过多，请等待 {self.user_limiter.window} 秒后重试 "
                f"(剩余 {remaining}/{self.user_limiter.max_requests})"
            )
        
        # ③ 供应商级限流
        if not self.vendor_limiter.is_allowed():
            raise RateLimitError("系统繁忙，请稍后重试")
        
        # ④ 模型级限流（按需创建）
        model_limiter = self._get_model_limiter(model)
        if not model_limiter.is_allowed():
            raise RateLimitError(f"模型 {model} 繁忙，请稍后重试")
        
        # ⑤ 并发限制
        with self._concurrent_lock:
            if self._active_requests >= self._concurrent_semaphore._value:
                raise RateLimitError("系统繁忙，正在处理的请求过多")
            self._active_requests += 1
        
        return True

    def release(self) -> None:
        """释放并发限制。"""
        with self._concurrent_lock:
            self._active_requests = max(0, self._active_requests - 1)

    def record_vendor_success(self) -> None:
        """记录供应商成功响应（用于熔断器恢复）。"""
        self.vendor_circuit_breaker.record_success()

    def record_vendor_429(self) -> None:
        """记录供应商 429 限流响应。"""
        self.vendor_circuit_breaker.record_failure()

    def is_vendor_circuit_open(self) -> bool:
        """检查供应商熔断器是否打开。"""
        return self.vendor_circuit_breaker.state == "open"

    def _get_model_limiter(self, model: str) -> TokenBucket:
        """获取或创建模型级限流器。"""
        with self._model_lock:
            if model not in self.model_limiters:
                # 每个模型：50 tokens/s，容量200
                self.model_limiters[model] = TokenBucket(rate=50.0, capacity=200)
            return self.model_limiters[model]

    def get_stats(self) -> Dict[str, Any]:
        """获取限流统计信息。"""
        return {
            "vendor_circuit_state": self.vendor_circuit_breaker.state,
            "vendor_available_tokens": self.vendor_limiter.available_tokens,
            "active_requests": self._active_requests,
            "model_limiters": {
                model: limiter.available_tokens 
                for model, limiter in self.model_limiters.items()
            },
        }


# ── 全局实例 ────────────────────────────────────────────────
_rate_limiter = MultiLevelRateLimiter()

def get_rate_limiter() -> MultiLevelRateLimiter:
    """获取全局限流器实例。"""
    return _rate_limiter
