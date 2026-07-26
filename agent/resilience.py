"""Resilience module — Phase B: Graceful Degradation + Fallback Chain

生产级降级策略：当 LLM 不可用时，返回结构化的 fallback 响应而不是崩溃。
同时集成 CircuitBreaker 到 `_call_llm()` 主路径中。

参考 JavaGuide "AI 应用系统设计" 的 resilience pattern。
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict


def generate_degraded_response(request_id: str) -> Dict[str, Any]:
    """当所有模型不可用时，返回结构化的降级响应。

    遵循 JavaGuide "Graceful Degradation" 最佳实践：
    - 保持 HTTP 200 状态码（让前端知道接口正常）
    - 包含 request_id 便于 trace
    - 提供人工客服提示
    """
    return {
        "status": "degraded",
        "request_id": request_id,
        "error_code": "MODEL_UNAVAILABLE",
        "message": "系统正在维护中，请稍后再试。如需帮助，请联系人工客服。",
        "fallback_to_human": True,
    }


class CircuitOpenError(Exception):
    """断路器打开时抛出 — 调用方应降级处理。"""
    pass


def retry_with_backoff(
    func,
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    **kwargs,
):
    """带指数退避的重试机制。

    Args:
        func: 要执行的函数（通常是 _call_llm）
        max_retries: 最大重试次数
        base_delay: 基础延迟秒数
        max_delay: 最大延迟秒数
    """
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except CircuitOpenError:
            # Circuit open — stop retrying immediately (don't hammer an open circuit)
            raise
        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.uniform(0, 0.5)  # jitter
                time.sleep(delay)
    
    raise last_exception
