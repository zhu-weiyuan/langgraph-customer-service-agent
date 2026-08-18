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

# ── Mock 开关（应用层压测用，默认关闭）─────────────────────
# MOCK_LLM=1 时 chat/chat_json/chat_stream 不发任何 HTTP，返回固定回复，
# 延迟由 MOCK_LLM_DELAY_MS 控制。详见 agent/mock_llm.py。
try:                                     # 包内导入
    from .mock_llm import (mock_chat, mock_chat_json, mock_llm_enabled,
                           mock_stream)
except ImportError:                      # 直接以脚本方式运行本文件时
    from mock_llm import (mock_chat, mock_chat_json, mock_llm_enabled,  # type: ignore
                          mock_stream)

# ── 重试配置 ─────────────────────────────────────────────
MAX_RETRIES = 3
BASE_DELAY = 1.0       # 首次重试等待1秒
MAX_DELAY = 30.0       # 最长等待30秒
JITTER = 1.0           # 随机抖动±1秒

# ── 超时配置 ─────────────────────────────────────────────
def _resolve_timeout() -> float:
    """Resolve LLM HTTP timeout from env or default (120s)."""
    try:
        return max(1.0, float(os.getenv("LLM_TIMEOUT", "120")))
    except (TypeError, ValueError):
        return 120.0

DEFAULT_LLM_TIMEOUT = _resolve_timeout()

# 可重试的HTTP状态码
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class LLMStreamInterruptedError(RuntimeError):
    """A provider stream broke after content was already forwarded to the user.

    Retrying such a request would replay a fresh completion from the beginning
    and corrupt the visible answer with duplicated text, so callers must surface
    an interrupted response instead of silently retrying it.
    """


class LLMClient:
    """OpenAI-compatible LLM client (works with mimo-v2.5)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,  # mimov2.5 needs higher tokens due to reasoning
        use_gateway: Optional[bool] = None,
        timeout: Optional[float] = None,
    ):
        # An explicit endpoint keeps the legacy direct HTTP client for compatibility;
        # the application singleton (no explicit arguments) uses the unified gateway.
        explicit_connection = any(value is not None for value in (base_url, api_key, model))
        self._use_gateway = (not explicit_connection) if use_gateway is None else bool(use_gateway)
        # Load from .env file if exists (only for local development)
        # In Docker/container environments, prefer environment variables over .env file
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists() and not os.getenv("_IN_DOCKER"):
            self._load_env(env_path)

        # 约定：base_url **包含 /v1**（如 https://api.xiaomimimo.com/v1）。
        # 所有端点统一在其后拼接资源路径：
        #   chat()        → {base_url}/chat/completions
        #   list_models() → {base_url}/models
        # 不要把 /chat/completions 写进 base_url（llm_gateway.py 同此约定）。
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL")
                         or "https://api.xiaomimimo.com/v1").rstrip("/")
        self.api_url = self.base_url  # Alias for compatibility
        # 安全修复：不再内置硬编码 key 兜底，一律从参数或环境变量读取。
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        if not self.api_key and not mock_llm_enabled():
            logger.warning("OPENAI_API_KEY is not set; LLM calls will fail until configured")
        self.model = model or os.getenv("OPENAI_MODEL") or "mimo-v2.5"
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout if timeout is not None else DEFAULT_LLM_TIMEOUT

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
        # MOCK_LLM=1：应用层压测模式，不发 HTTP，固定延迟 + 固定回复
        if mock_llm_enabled():
            return mock_chat(messages)

        if self._use_gateway:
            from .llm_gateway import GatewayRequest, get_gateway_context, get_llm_gateway
            context = get_gateway_context()
            request = GatewayRequest(
                messages=messages,
                scene=str(context.get("scene") or "chat"),
                tenant_id=str(context.get("tenant_id") or "default"),
                user_id=context.get("user_id"),
                trace_id=str(context.get("trace_id") or ""),
                idempotency_key=context.get("idempotency_key"),
                prompt_version=str(context.get("prompt_version") or "v1"),
                metadata={**context, "trace_session": context.get("trace_session")},
                temperature=self.temperature if temperature is None else temperature,
                max_output_tokens=self.max_tokens if max_tokens is None else max_tokens,
            )
            return get_llm_gateway().chat_sync(request).content.strip()

        import requests

        if not self.api_key:
            raise RuntimeError(
                "LLM API key is empty. Set the OPENAI_API_KEY environment variable "
                "(or pass api_key=) before calling the LLM client.")

        # base_url 已含 /v1（见 __init__ 注释），此处只拼资源路径
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            # Qwen-compatible switches: return final answer without reasoning traces.
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }

        last_exception = None
        for attempt in range(max_retries + 1):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=self.timeout)

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
        # MOCK_LLM=1：直接返回场景对应的合法 JSON（走 Level 1 等价路径）
        if mock_llm_enabled():
            return mock_chat_json(messages, system)

        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        # 修复：raw 先绑定，避免 Level 1 的 chat() 抛异常时后续引用 raw 触发 NameError
        raw = ""

        # ── Level 1: 直接解析 ──────────────────────────────
        try:
            raw = self.chat(msgs, temperature=temperature, max_tokens=max_tokens)
            parsed = self._extract_json(raw)
            if parsed:
                return parsed
        except Exception as e:
            logger.error(f"Level 1 failed: {e}")

        # ── Level 2: 让 LLM 修复格式（Level 1 无输出时跳过）─
        if raw:
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
            if raw:
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

    # 调用方实际会读取的 JSON key 白名单（用于优先挑选目标 JSON 对象）：
    # - nodes.py:        intent / ending / satisfaction / satisfied
    # - sentiment.py:    emotion / intensity
    # - agentic_rag.py:  sufficient / reason / new_queries
    # - summary.py:      issue_category / description / resolution / priority
    # - 通用:            confidence
    EXPECTED_JSON_KEYS = frozenset({
        "intent", "ending", "satisfaction", "satisfied",
        "emotion", "intensity",
        "sufficient", "reason", "new_queries",
        "issue_category", "description", "resolution", "priority",
        "confidence",
    })

    @classmethod
    def _extract_json(cls, text: str) -> dict:
        """Extract JSON object from the LAST response only.

        策略（修复旧版白名单缺 key 导致 sentiment/summary/RAG 的合法 JSON 被丢弃）：
        1. 优先返回包含任一白名单 key 的 JSON 对象（倒序，取最后一个）
        2. 否则退化为通用提取：返回最后一个合法 JSON 对象，由调用方自行校验字段
        """
        import re
        if not text:
            return {}
        # Only look at the last 800 chars of response to avoid matching system prompt
        tail = text[-800:] if len(text) > 800 else text
        matches = re.findall(r'\{[^{}]*\}', tail, re.DOTALL)
        generic: dict = {}
        for m in reversed(matches):          # 倒序：最后出现的优先
            try:
                obj = json.loads(m)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if any(k in obj for k in cls.EXPECTED_JSON_KEYS):
                return obj
            if not generic:
                generic = obj                # 记住最后一个合法 dict 作为兜底
        return generic

    def chat_stream(
        self,
        messages: List[dict],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """True token streaming via SSE, with safe pre-output retries only.

        A retry is safe before any delta has reached the caller.  Once output is
        visible, reissuing the request can duplicate a response prefix, therefore
        a broken stream raises :class:`LLMStreamInterruptedError` instead.
        """
        if mock_llm_enabled():
            yield from mock_stream(messages)
            return

        if self._use_gateway:
            from .llm_gateway import GatewayRequest, get_gateway_context, get_llm_gateway
            context = get_gateway_context()
            request = GatewayRequest(
                messages=messages,
                scene=str(context.get("scene") or "chat"),
                tenant_id=str(context.get("tenant_id") or "default"),
                user_id=context.get("user_id"),
                trace_id=str(context.get("trace_id") or ""),
                idempotency_key=context.get("idempotency_key"),
                prompt_version=str(context.get("prompt_version") or "v1"),
                metadata={**context, "trace_session": context.get("trace_session")},
                temperature=self.temperature if temperature is None else temperature,
                max_output_tokens=self.max_tokens if max_tokens is None else max_tokens,
            )
            yield from get_llm_gateway().stream_sync(request)
            return

        import requests

        if not self.api_key:
            raise RuntimeError(
                "LLM API key is empty. Set the OPENAI_API_KEY environment variable "
                "(or pass api_key=) before calling the LLM client.")

        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": True,
        }

        yielded_content = False
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                with requests.post(url, headers=headers, json=payload,
                                   timeout=self.timeout, stream=True) as resp:
                    resp.raise_for_status()
                    for raw_line in resp.iter_lines(chunk_size=1, decode_unicode=False):
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8").strip()
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            return
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        choices = data.get("choices", []) if isinstance(data, dict) else []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {}) or {}
                        content = delta.get("content", "")
                        if content:
                            yielded_content = True
                            yield content
                return
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = exc
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status not in RETRYABLE_STATUS_CODES:
                    raise
                last_error = exc
            except Exception:
                raise

            if yielded_content:
                raise LLMStreamInterruptedError(
                    "LLM stream interrupted after partial output; retry is unsafe because it may duplicate text."
                ) from last_error
            if attempt >= MAX_RETRIES:
                break

            delay = self._calculate_delay(attempt)
            if isinstance(last_error, requests.exceptions.HTTPError):
                response = last_error.response
                retry_after = response.headers.get("Retry-After") if response is not None else None
                try:
                    if retry_after is not None:
                        delay = max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    pass
            logger.warning(
                "chat_stream %s before first token; retry %d/%d in %.1fs",
                type(last_error).__name__, attempt + 1, MAX_RETRIES, delay,
            )
            time.sleep(delay)

        raise RuntimeError(
            f"LLM streaming request failed after {MAX_RETRIES} retries before any output. "
            f"Last error: {last_error}"
        ) from last_error

    def list_models(self, timeout: float = 10.0) -> list:
        """List available models（连通性探测也可用）。

        与 chat() 的 URL 拼接约定一致：base_url 已含 /v1，此处只拼 /models。
        （旧版 app 层各自手写 replace("/chat/completions", "") 再拼 /models，
        两处口径不一致；统一收敛到本方法。）
        """
        import requests

        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", []) if isinstance(data, dict) else []

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
