#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenAI-compatible LLM client for mimo-v2.5

Usage:
    from agent.llm_client import LLMClient
    llm = LLMClient()  # Reads from .env
    response = llm.chat("你好")
"""

import json
import os
import time
import random
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── 重试配置 ─────────────────────────────────────────────
MAX_RETRIES = 3
BASE_DELAY = 1.0       # 首次重试等待1秒
MAX_DELAY = 30.0       # 最长等待30秒
JITTER = 1.0           # 随机抖动±1秒

# 可重试的HTTP状态码
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class LLMClient:
    """OpenAI-compatible LLM client (works with mimo-v2.5)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,  # mimov2.5 needs higher tokens due to reasoning
    ):
        # Load from .env file if exists
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            self._load_env(env_path)

        self.base_url = base_url or os.getenv("OPENAI_BASE_URL") or "https://api.xiaomimimo.com/v1"
        self.api_url = self.base_url  # Alias for compatibility
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or "sk-ckmnbfew0gajnwb508q42tvbvyvcswtf9k2c6wfqwi991ksj"
        self.model = model or os.getenv("OPENAI_MODEL") or "mimo-v2.5"
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _load_env(self, path: Path):
        """Load .env file."""
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    def _should_retry(self, status_code: int, exception: Exception) -> bool:
        """判断是否应该重试。

        可重试的情况：
        - 429: 限流（看 Retry-After）
        - 500/502/503/504: 服务器错误
        - ConnectionError: 网络抖动
        - Timeout: 超时

        不可重试的情况：
        - 400: 参数错误（重试也没用）
        - 401/403: 鉴权错误（Key无效）
        """
        if status_code in (400, 401, 403):
            return False
        if status_code in RETRYABLE_STATUS_CODES:
            return True
        # 网络异常
        if isinstance(exception, (ConnectionError, TimeoutError)):
            return True
        return False

    def _calculate_delay(self, attempt: int) -> float:
        """计算重试延迟（指数退避 + 随机抖动）。

        公式：delay = min(MAX_DELAY, BASE_DELAY * 2^attempt) + random(-JITTER, JITTER)

        示例：
        - 第1次重试: 1s × 2^0 + 抖动 ≈ 1-3s
        - 第2次重试: 1s × 2^1 + 抖动 ≈ 2-4s
        - 第3次重试: 1s × 2^2 + 抖动 ≈ 4-6s
        """
        delay = min(MAX_DELAY, BASE_DELAY * (2 ** attempt))
        jitter = random.uniform(-JITTER, JITTER)
        return max(0.1, delay + jitter)  # 至少等0.1秒

    def chat(
        self,
        messages: List[dict],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_retries: int = MAX_RETRIES,
    ) -> str:
        """Send chat completion request with retry logic.

        重试策略：
        - 指数退避 + 随机抖动（避免雪崩）
        - 429限流时优先使用 Retry-After 头
        - 最多重试 max_retries 次
        """
        import requests

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature or self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }

        last_exception = None
        for attempt in range(max_retries + 1):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=120)

                # 成功
                if response.status_code == 200:
                    data = response.json()
                    msg = data["choices"][0]["message"]
                    content = msg.get("content") or ""
                    reasoning = msg.get("reasoning_content") or ""
                    if not content.strip() and reasoning.strip():
                        logger.warning("Content empty but reasoning exists - max_tokens may be too low")
                    return content.strip()

                # 需要重试的错误
                if self._should_retry(response.status_code, None):
                    # 429限流：优先使用 Retry-After
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            delay = float(retry_after)
                        else:
                            delay = self._calculate_delay(attempt)
                    else:
                        delay = self._calculate_delay(attempt)

                    logger.warning(
                        f"HTTP {response.status_code}, retrying in {delay:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(delay)
                    continue

                # 不可重试的错误，直接抛
                response.raise_for_status()

            except requests.exceptions.HTTPError as e:
                last_exception = e
                if self._should_retry(e.response.status_code if e.response else None, e):
                    delay = self._calculate_delay(attempt)
                    logger.warning(f"HTTP error {e}, retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                raise
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_exception = e
                delay = self._calculate_delay(attempt)
                logger.warning(f"{type(e).__name__}, retrying in {delay:.1f}s")
                time.sleep(delay)
                continue
            except Exception as e:
                # 未知异常，不重试
                raise

        # 所有重试都失败了
        raise RuntimeError(
            f"LLM request failed after {max_retries} retries. "
            f"Last error: {last_exception}"
        ) from last_exception

    def chat_json(
        self,
        messages: List[dict],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        default_response: Optional[dict] = None,
    ) -> dict:
        """Send chat completion and parse JSON response with fallback.

        四级兜底策略：
        1. 直接解析 LLM 返回的 JSON
        2. 让 LLM 修复格式（不重新生成内容）
        3. Few-shot 示例引导
        4. 返回默认值（default_response）

        Args:
            default_response: 所有兜底失败后返回的默认值，默认为空dict
        """
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        # ── Level 1: 直接解析 ──────────────────────────────
        try:
            raw = self.chat(msgs, temperature=temperature, max_tokens=max_tokens)
            parsed = self._extract_json(raw)
            if parsed:
                return parsed
        except Exception as e:
            logger.error(f"Level 1 failed: {e}")

        # ── Level 2: 让 LLM 修复格式 ───────────────────────
        try:
            fix_prompt = (
                "上面的输出不是合法JSON，请修正格式。"
                "原始内容：\n" + raw + "\n"
                "请直接返回修正后的JSON，不要解释。"
            )
            fixed = self.chat([{"role": "user", "content": fix_prompt}])
            parsed = self._extract_json(fixed)
            if parsed:
                return parsed
        except Exception as e:
            logger.error(f"Level 2 failed: {e}")

        # ── Level 3: Few-shot 示例引导 ─────────────────────
        try:
            msgs.append({"role": "assistant", "content": raw})
            msgs.append({"role": "user", "content": (
                '请严格按要求返回JSON，不要解释。'
                '例如：{"intent": "chat", "ending": false}'
            )})
            retry_raw = self.chat(msgs, temperature=0.1, max_tokens=max_tokens)
            parsed = self._extract_json(retry_raw)
            if parsed:
                return parsed
        except Exception as e:
            logger.error(f"Level 3 failed: {e}")

        # ── Level 4: 返回默认值 ─────────────────────────────
        logger.warning(f"All JSON parsing levels failed, returning default: {default_response}")
        return default_response or {}

    @staticmethod
    def _extract_json(text: str) -> dict:
        """Extract JSON object from the LAST response only (skip system prompt contamination)."""
        import re
        # Only look at the last 500 chars of response to avoid matching system prompt
        tail = text[-500:] if len(text) > 500 else text
        matches = re.findall(r'\{[^{}]+\}', tail, re.DOTALL)
        for m in matches:
            try:
                obj = json.loads(m)
                if isinstance(obj, dict) and any(k in obj for k in ('intent', 'ending', 'satisfaction')):
                    return obj
            except json.JSONDecodeError:
                continue
        return {}

    def generate_reply(
        self,
        user_message: str,
        context: str = "",
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate customer service reply with RAG context.

        Args:
            user_message: Customer's message
            context: Retrieved knowledge base context
            system_prompt: Custom system prompt (uses default if None)

        Returns:
            Assistant's response
        """
        if system_prompt is None:
            system_prompt = """你是一个专业的智能客服助手。

你的职责：
1. 根据知识库内容准确回答问题
2. 态度友好、专业、耐心
3. 如果知识库没有相关信息，诚实告知并建议联系客服
4. 回答要简洁明了，不要过度解释

重要规则：
- 只基于提供的参考资料回答
- 不要编造信息
- 保持专业和友好的语气"""

        messages = [
            {"role": "system", "content": system_prompt},
        ]

        if context:
            user_content = f"问题：{user_message}\n\n参考资料：\n{context}"
        else:
            user_content = user_message

        messages.append({"role": "user", "content": user_content})

        return self.chat(messages)


_client_instance = None


def get_llm_client() -> LLMClient:
    """Get singleton LLM client instance."""
    global _client_instance
    if _client_instance is None:
        _client_instance = LLMClient()
    return _client_instance


if __name__ == "__main__":
    llm = LLMClient()
    print(f"LLM: {llm.model}")
    print(f"URL: {llm.base_url}")

    # Test basic chat
    response = llm.chat([{"role": "user", "content": "你好"}])
    print(f"Response: {response}")
