# -*- coding: utf-8 -*-
"""
Patched server for load testing (LLM + rate limiter both mocked).
Auto-starts on port 17860.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 1) Mock rate limiter (before import!)
import agent.rate_limiter as _rl
class _MockLimiter:
    async def acquire(self, *, user_id="", ip="", session_id=""):
        pass
    def concurrency(self, timeout=30):
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _noop(): yield
        return _noop()
    def get_stats(self):
        return {"by_layer": {}, "total_requests": 0, "total_blocked": 0}
_rl.get_rate_limiter = lambda: _MockLimiter()

# 2) Mock LLM (before node functions are called)
import agent.nodes as _nodes
def _mock_call_llm(messages, system=None, max_tokens=512, stream=False):
    return "您好，我是智能客服助手，请问有什么可以帮您的？"
_nodes._call_llm = _mock_call_llm
_nodes._call_llm_json = lambda messages, system, max_tokens=256: {
    "intent": "consult", "confidence": 0.95, "reasoning": "general question"
}

# 3) Patch user_memory extraction so it doesn't fail silently
import agent.user_memory as _um
_um.extract_from_message = lambda user_id, user_message, **kw: {}

# 4) Now import the real app
os.environ.setdefault("DATABASE_URL",
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph")
os.environ.setdefault("API_KEYS", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:8080/v1")
os.environ.setdefault("OPENAI_API_KEY", "sk-local")
os.environ["REDIS_URL"] = ""  # 禁用 Redis 限流

from app_fastapi import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=17860, log_level="warning")
