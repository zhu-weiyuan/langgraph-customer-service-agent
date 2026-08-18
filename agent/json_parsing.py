"""Small, dependency-free helpers for extracting structured JSON from LLM text.

LLM responses often contain Markdown fences or explanation before/after the JSON.
These helpers use ``JSONDecoder.raw_decode`` instead of greedy regular expressions,
so one unrelated JSON value cannot swallow another one.
"""
from __future__ import annotations

import json
from typing import Any, Iterable


def _decoded_values(text: Any, opening: str) -> Iterable[Any]:
    """Yield JSON values decoded from each plausible opening character in *text*."""
    if not isinstance(text, str):
        return
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != opening:
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        yield value


def parse_json_array(text: Any) -> list[str] | None:
    """Return the first useful JSON string array embedded in LLM output.

    Empty illustrative arrays are retained only when no later non-empty array is
    available. Non-string / blank members are intentionally ignored because query
    rewrite consumers require usable text variants.
    """
    empty: list[str] | None = None
    for value in _decoded_values(text, "["):
        if not isinstance(value, list):
            continue
        items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if items:
            return items
        if empty is None:
            empty = []
    return empty


def parse_json_object(text: Any, *, required_keys: Iterable[str] = ()) -> dict[str, Any] | None:
    """Return the first JSON object containing all ``required_keys``.

    If no required keys are given, the first decoded object is returned.
    """
    required = set(required_keys)
    for value in _decoded_values(text, "{"):
        if isinstance(value, dict) and required.issubset(value):
            return value
    return None
