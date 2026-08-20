"""Unified token estimator — the ONLY token estimation entry point for the project.

Every module that needs a token count MUST import from here:

    from .token_estimator import estimate_tokens, estimate_messages_tokens

Strategy
--------
1. Preferred backend: ``tiktoken`` (``cl100k_base``). Imported lazily on first
   use so this module is importable in environments without tiktoken; any
   import/encoding failure permanently falls back to the heuristic below.
2. Heuristic fallback (deterministic, stdlib-only)::

       tokens = ceil(cjk_chars * 0.7 + ascii_words * 1.3 + other_chars * 0.3)

Calibration notes (why these coefficients)
------------------------------------------
Measured against ``cl100k_base`` on mixed customer-service corpora:

* CJK (Chinese/Japanese/Korean ideographs, kana, fullwidth forms): common
  hanzi encode to ~0.6–0.8 tokens per character on cl100k (frequent chars are
  single tokens; rarer chars split into 2 byte-level tokens, averaging out
  around 0.7). The legacy coefficient of 1.5 tokens/char massively
  over-estimated and starved the context budget.
* ASCII words: average English word is ~4.7 chars and encodes to ~1.3 tokens
  (1 token for common words, 2+ for longer/rarer ones).
* Remaining characters (punctuation, digits, whitespace): most collapse into
  neighbouring tokens or encode 2–4 chars per token, ~0.3 tokens/char.

The heuristic intentionally errs slightly high for safety: over-estimating
wastes a little budget; under-estimating overflows the model context.
"""
from __future__ import annotations

import math
import re
import os
import time
from typing import Any, Iterable

__all__ = [
    "estimate_tokens",
    "estimate_messages_tokens",
    "TokenEstimator",
]

# CJK unified ideographs + extension A, CJK punctuation, kana, hangul,
# fullwidth/halfwidth forms, compatibility ideographs.
_CJK_RE = re.compile(
    r"[ᄀ-ᇿ⺀-⻿　-〿぀-ヿ㄰-㆏"
    r"㐀-䶿一-鿿가-힯豈-﫿＀-￯]"
)
_ASCII_WORD_RE = re.compile(r"[A-Za-z]+")

# Per-message structural overhead (role markers, separators) in tokens.
_MESSAGE_OVERHEAD_TOKENS = 4

# Lazy tiktoken state: None = not tried yet, False = the last attempt failed,
# otherwise the encoder object.  A transient package/cache error must not make
# the whole process permanently use the heuristic fallback.
_ENCODER: Any = None
_ENCODER_FAILED_AT: float | None = None
_DEFAULT_RETRY_SECONDS = 60.0


def _retry_seconds() -> float:
    """Return a bounded retry cooldown without making config parsing fatal."""
    try:
        return max(1.0, float(os.getenv("TOKEN_ESTIMATOR_RETRY_SECONDS", _DEFAULT_RETRY_SECONDS)))
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_SECONDS


def _get_encoder():
    """Lazily obtain ``tiktoken`` and periodically retry a failed acquisition."""
    global _ENCODER, _ENCODER_FAILED_AT
    now = time.monotonic()
    should_try = _ENCODER is None or (
        _ENCODER is False and (
            _ENCODER_FAILED_AT is None or now - _ENCODER_FAILED_AT >= _retry_seconds()
        )
    )
    if should_try:
        try:
            import tiktoken  # deferred third-party import
            _ENCODER = tiktoken.get_encoding("cl100k_base")
            _ENCODER_FAILED_AT = None
        except Exception:
            _ENCODER = False
            _ENCODER_FAILED_AT = now
    return _ENCODER or None


def _heuristic_estimate(text: str) -> int:
    """Stdlib heuristic: cjk*0.7 + ascii_words*1.3 + other*0.3, ceil'ed."""
    if not text:
        return 0
    cjk_chars = len(_CJK_RE.findall(text))
    ascii_words = _ASCII_WORD_RE.findall(text)
    ascii_word_count = len(ascii_words)
    ascii_letter_chars = sum(len(w) for w in ascii_words)
    # Keep the fallback estimator stable across Unicode and platform builds.
    # ASCII digits are treated as word-like tokens rather than punctuation.
    digit_chars = sum(ch.isdigit() for ch in text)
    other_chars = max(0, len(text) - cjk_chars - ascii_letter_chars - digit_chars)
    tokens = math.ceil(
        cjk_chars * 0.7 + ascii_word_count * 1.3 + digit_chars * 0.3 + other_chars * 0.3
    )
    return max(tokens, 1) if text.strip() else 0


def estimate_tokens(text: Any) -> int:
    """Estimate token count of a text. Prefers tiktoken, degrades gracefully."""
    if text is None:
        return 0
    text = text if isinstance(text, str) else str(text)
    if not text:
        return 0
    encoder = _get_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:
            pass
    return _heuristic_estimate(text)


def _content_of(message: Any) -> str:
    """Extract textual content from a dict / LangChain message / raw string."""
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content", "")
    else:
        content = getattr(message, "content", message)
    if isinstance(content, (list, tuple)):  # multimodal content blocks
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts)
    return content if isinstance(content, str) else str(content)


def estimate_messages_tokens(messages: Iterable[Any]) -> int:
    """Estimate total tokens of a message list (content + per-message overhead)."""
    total = 0
    for message in messages or []:
        total += estimate_tokens(_content_of(message)) + _MESSAGE_OVERHEAD_TOKENS
    return total


class TokenEstimator:
    """Compatibility shim for legacy references (context_monitor-style API).

    Old code did::

        est = TokenEstimator()
        est.estimate_text(text); est.estimate_messages(msgs)

    Both now delegate to the unified estimator above.
    """

    WARN_THRESHOLD = 0.60
    DUMB_ZONE_THRESHOLD = 0.80

    @staticmethod
    def estimate_text(text: Any) -> int:
        return estimate_tokens(text)

    def estimate_messages(self, messages: Iterable[Any]) -> int:
        return estimate_messages_tokens(messages)

    def __call__(self, text: Any) -> int:
        return estimate_tokens(text)
