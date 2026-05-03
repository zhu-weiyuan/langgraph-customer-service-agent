"""
LLM Client abstraction layer for customer service agent.

Provides a pluggable interface for LLM calls with built-in retry,
timeout, and robust JSON parsing.  API keys are read from environment
variables by default — **no hardcoding**.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.request
from abc import ABC, abstractmethod
from typing import List, Dict

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    @abstractmethod
    def chat(self, messages: List[Dict[str, str]], system: str, max_tokens: int = 512, temperature: float = 0.7) -> str:
        ...

    @abstractmethod
    def chat_json(self, messages: List[Dict[str, str]], system: str, max_tokens: int = 256) -> dict:
        ...


class LocalLLMClient(LLMClient):
    def __init__(self, api_url: str = None, api_key: str = None, timeout: int = 180, max_retries: int = 3):
        self.api_url = api_url or os.environ.get("LLM_API_URL", "http://127.0.0.1:8080/v1/chat/completions")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.timeout = timeout
        self.max_retries = max_retries

    def _request(self, messages, system, max_tokens, temperature):
        payload = {
            "messages": [{"role": "system", "content": system}] + messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        last_error = None
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(
                    self.api_url, data=data,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
        raise last_error  # type: ignore[misc]

    def chat(self, messages, system, max_tokens=512, temperature=0.7) -> str:
        try:
            result = self._request(messages, system, max_tokens, temperature)
            return result["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.error("[LLM] chat error: %s", exc)
            return "抱歉，我暂时无法处理您的请求，请稍后再试。"

    def chat_json(self, messages, system, max_tokens=256) -> dict:
        try:
            result = self._request(messages, system, max_tokens, temperature=0.3)
            text = result["choices"][0]["message"]["content"].strip()
            return self._parse_json(text)
        except Exception as exc:
            logger.error("[LLM] chat_json error: %s", exc)
            return {}

    @staticmethod
    def _parse_json(text: str) -> dict:
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
        match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        try:
            cleaned = text.strip().rstrip("`").strip()
            if cleaned.startswith("{") and cleaned.endswith("}"):
                return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        return {}


# Module-level singleton (thread-safe lazy init)
_llm_client: LLMClient = None
_llm_lock = threading.Lock()


def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        with _llm_lock:
            if _llm_client is None:
                _llm_client = LocalLLMClient()
    return _llm_client


def set_llm_client(client: LLMClient) -> None:
    global _llm_client
    _llm_client = client
