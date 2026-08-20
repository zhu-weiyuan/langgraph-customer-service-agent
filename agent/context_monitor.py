"""Context-window utilization monitoring independent of a tokenizer backend."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from .token_estimator import estimate_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextUsage:
    input_tokens: int
    context_window: int
    utilization: float
    level: str


class TokenEstimator:
    """Compatibility monitor backed by the project's unified estimator.

    Keeping this facade avoids breaking callers while ensuring context assembly,
    gateway checks, and monitoring report the same token estimate.
    """
    WARN_THRESHOLD = 0.60
    DUMB_ZONE_THRESHOLD = 0.80

    @staticmethod
    def estimate_text(text: Any) -> int:
        return estimate_tokens(text)

    def estimate_messages(self, messages: Iterable[Any]) -> int:
        return estimate_messages_tokens(messages)

    def monitor(self, *, system_prompt: str = "", messages: Iterable[Any] = (),
                rag_chunks: Any = "", memory: Any = "", context_window: int = 120_000) -> ContextUsage:
        if context_window <= 0:
            raise ValueError("context_window must be > 0")
        tokens = (self.estimate_text(system_prompt) + self.estimate_messages(messages) +
                  self.estimate_text(rag_chunks) + self.estimate_text(memory))
        utilization = tokens / context_window
        level = "normal"
        if utilization > self.DUMB_ZONE_THRESHOLD:
            level = "dumb_zone"
            logger.error("Context utilization %.1f%% exceeds 80%% Dumb Zone", utilization * 100)
        elif utilization > self.WARN_THRESHOLD:
            level = "warning"
            logger.warning("Context utilization %.1f%% exceeds 60%%", utilization * 100)
        return ContextUsage(tokens, context_window, utilization, level)
