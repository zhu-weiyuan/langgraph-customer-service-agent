"""Context-window utilization monitoring independent of a tokenizer backend."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextUsage:
    input_tokens: int
    context_window: int
    utilization: float
    level: str


class TokenEstimator:
    """Conservative character-based input estimator with utilization thresholds."""
    WARN_THRESHOLD = 0.60
    DUMB_ZONE_THRESHOLD = 0.80

    @staticmethod
    def estimate_text(text: Any) -> int:
        # Matches the existing gateway's mixed Chinese/English approximation.
        return max(0, (len(str(text or "")) + 1) // 2)

    def estimate_messages(self, messages: Iterable[Any]) -> int:
        total = 0
        for message in messages or []:
            if isinstance(message, dict):
                content = message.get("content", "")
            else:
                content = getattr(message, "content", message)
            total += self.estimate_text(content) + 4
        return total

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
