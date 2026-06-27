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
from pathlib import Path
from typing import List, Optional


class LLMClient:
    """OpenAI-compatible LLM client (works with mimo-v2.5)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,  # mimov2.5 needs higher tokens due to reasoning
    ):
        # Load from .env file if exists
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            self._load_env(env_path)

        self.base_url = base_url or os.getenv("OPENAI_BASE_URL") or "https://token-plan-cn.xiaomimimo.com/v1"
        self.api_url = self.base_url  # Alias for compatibility
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
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

    def chat(
        self,
        messages: List[dict],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Send chat completion request."""
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

        response = requests.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()

        return data["choices"][0]["message"]["content"]

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
