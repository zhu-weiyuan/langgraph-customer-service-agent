# -*- coding: utf-8 -*-
"""mock LLM 层 + 压测器统计的纯 stdlib 单测（无网络、无三方依赖）。

运行：python -m unittest tests.test_mock_llm_pure -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "loadtest"))

from agent import mock_llm                                      # noqa: E402
from agent.llm_client import LLMClient                          # noqa: E402


class EnvGuard:
    """临时设置环境变量的上下文管理器。"""

    def __init__(self, **kv):
        self.kv = kv
        self.old = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class TestSwitchesDefaultOff(unittest.TestCase):
    def test_disabled_by_default(self):
        with EnvGuard(MOCK_LLM=None, MOCK_EMBEDDING=None):
            self.assertFalse(mock_llm.mock_llm_enabled())
            self.assertFalse(mock_llm.mock_embedding_enabled())

    def test_truthy_values(self):
        for value in ("1", "true", "YES", "on"):
            with EnvGuard(MOCK_LLM=value):
                self.assertTrue(mock_llm.mock_llm_enabled())
        for value in ("0", "false", "", "no"):
            with EnvGuard(MOCK_LLM=value):
                self.assertFalse(mock_llm.mock_llm_enabled())

    def test_delay_env(self):
        with EnvGuard(MOCK_LLM_DELAY_MS="350", MOCK_LLM_JSON_DELAY_MS=None):
            self.assertAlmostEqual(mock_llm.mock_delay_seconds(), 0.35, 3)
            self.assertAlmostEqual(mock_llm.mock_json_delay_seconds(), 0.35, 3)
        with EnvGuard(MOCK_LLM_DELAY_MS="350", MOCK_LLM_JSON_DELAY_MS="50"):
            self.assertAlmostEqual(mock_llm.mock_json_delay_seconds(), 0.05, 3)
        with EnvGuard(MOCK_LLM_DELAY_MS="garbage"):
            self.assertAlmostEqual(mock_llm.mock_delay_seconds(), 0.2, 3)


class TestMockChat(unittest.TestCase):
    def test_sync_chat_is_deterministic_and_delayed(self):
        with EnvGuard(MOCK_LLM="1", MOCK_LLM_DELAY_MS="60"):
            client = LLMClient(api_key="")          # 无 key 也能跑
            t0 = time.perf_counter()
            a = client.chat([{"role": "user", "content": "退货政策"}])
            elapsed = time.perf_counter() - t0
            b = client.chat([{"role": "user", "content": "退货政策"}])
        self.assertEqual(a, b)                       # 确定性
        self.assertIn("退货政策", a)
        self.assertGreaterEqual(elapsed, 0.05)       # time.sleep 生效

    def test_chat_json_keys_are_whitelisted(self):
        """返回的 key 必须在 EXPECTED_JSON_KEYS 内，否则调用方会走兜底分支。"""
        with EnvGuard(MOCK_LLM="1", MOCK_LLM_DELAY_MS="1"):
            client = LLMClient(api_key="")
            cases = [
                ("请判断用户意图并给出 intent", {"intent", "ending"}),
                ("分析用户情绪 emotion", {"emotion", "intensity"}),
                ("判断资料是否足够 sufficient", {"sufficient", "new_queries"}),
                ("生成工单总结", {"issue_category", "resolution", "priority"}),
                ("用户是否满意", {"satisfaction", "satisfied"}),
            ]
            for system, required in cases:
                out = client.chat_json([{"role": "user", "content": "x"}],
                                       system=system)
                self.assertTrue(required.issubset(set(out)),
                                f"{system} -> {out}")
                self.assertTrue(set(out).issubset(
                    LLMClient.EXPECTED_JSON_KEYS | {"confidence"}),
                    f"unexpected keys in {out}")

    def test_ending_detected(self):
        with EnvGuard(MOCK_LLM="1", MOCK_LLM_DELAY_MS="1"):
            client = LLMClient(api_key="")
            self.assertTrue(client.chat_json(
                [{"role": "user", "content": "谢谢，再见"}],
                system="意图")["ending"])
            self.assertFalse(client.chat_json(
                [{"role": "user", "content": "怎么退货"}],
                system="意图")["ending"])

    def test_stream_yields_multiple_tokens_totalling_delay(self):
        with EnvGuard(MOCK_LLM="1", MOCK_LLM_DELAY_MS="80",
                      MOCK_LLM_TOKENS="16"):
            client = LLMClient(api_key="")
            t0 = time.perf_counter()
            pieces = list(client.chat_stream([{"role": "user", "content": "hi"}]))
            elapsed = time.perf_counter() - t0
        self.assertEqual(len(pieces), 16)
        self.assertGreaterEqual(elapsed, 0.06)       # ≈ 总延迟
        self.assertEqual("".join(pieces),
                         mock_llm.mock_reply_text([{"role": "user",
                                                    "content": "hi"}]))

    def test_async_paths_use_asyncio_sleep(self):
        """20 个并发 mock 调用应≈1 倍延迟——证明异步路径没有阻塞事件循环。"""
        async def scenario():
            t0 = time.perf_counter()
            await asyncio.gather(*[mock_llm.mock_chat_async(
                [{"role": "user", "content": str(i)}]) for i in range(20)])
            return time.perf_counter() - t0

        with EnvGuard(MOCK_LLM="1", MOCK_LLM_DELAY_MS="100"):
            elapsed = asyncio.run(scenario())
        self.assertGreaterEqual(elapsed, 0.09)
        self.assertLess(elapsed, 0.6)                # 远小于串行的 2.0s

    def test_reply_override(self):
        with EnvGuard(MOCK_LLM="1", MOCK_LLM_DELAY_MS="1",
                      MOCK_LLM_REPLY="FIXED"):
            self.assertEqual(mock_llm.mock_reply_text([{"role": "user",
                                                        "content": "x"}]),
                             "FIXED")


class TestFakeEmbedding(unittest.TestCase):
    def test_deterministic_normalized_and_dim(self):
        v1 = mock_llm.fake_embedding("hello", dim=64)
        v2 = mock_llm.fake_embedding("hello", dim=64)
        v3 = mock_llm.fake_embedding("world", dim=64)
        self.assertEqual(v1, v2)
        self.assertNotEqual(v1, v3)
        self.assertEqual(len(v1), 64)
        norm = sum(x * x for x in v1) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_embedding_client_mock_path(self):
        from agent.embedding_client import EmbeddingClient
        with EnvGuard(MOCK_EMBEDDING="1", MOCK_EMBEDDING_DIM="32",
                      MOCK_EMBEDDING_DELAY_MS="0", OPENAI_API_KEY=None,
                      EMBEDDING_API_KEY=None, MY_AGENT_API_KEY=None):
            client = EmbeddingClient.from_env(strict=False)
            self.assertIsNotNone(client)

            def boom(*a, **kw):                      # 任何 HTTP 都算失败
                raise AssertionError("mock embedding must not hit network")

            client.transport = boom
            vecs = client.embed(["a", "b", "c"])
        self.assertEqual(len(vecs), 3)
        self.assertEqual(len(vecs[0]), 32)


class TestLoadtestStats(unittest.TestCase):
    """压测器统计逻辑（分位数 / 429 归类）——数字可信度的底座。"""

    def test_percentile_nearest_rank(self):
        from run_loadtest import percentile
        data = [float(i) for i in range(1, 101)]
        self.assertEqual(percentile(data, 50), 50.0)
        self.assertEqual(percentile(data, 90), 90.0)
        self.assertEqual(percentile(data, 95), 95.0)
        self.assertEqual(percentile(data, 99), 99.0)
        self.assertEqual(percentile(data, 100), 100.0)
        self.assertEqual(percentile([], 95), 0.0)
        self.assertEqual(percentile([7.0], 95), 7.0)

    def test_collector_counts_429_separately(self):
        from run_loadtest import Collector, Sample
        col = Collector()
        for _ in range(8):
            col.add(Sample("chat", 0.0, 100.0, 200, True))
        for _ in range(2):
            col.add(Sample("chat", 0.0, 5.0, 429, False, rate_limited=True))
        col.add(Sample("chat", 0.0, 50.0, 500, False, error="boom"))
        report = col.summarize(wall_seconds=1.0)
        o = report["overall"]
        self.assertEqual(o["requests"], 11)
        self.assertEqual(o["ok"], 8)
        self.assertEqual(o["rate_limited_429"], 2)
        self.assertEqual(o["failed"], 1)             # 429 不算失败
        self.assertAlmostEqual(o["fail_ratio"], 1 / 11, places=4)
        self.assertEqual(o["qps"], 11.0)
        self.assertEqual(report["status_distribution"]["429"], 2)
        self.assertEqual(report["status_distribution"]["500"], 1)

    def test_weights_parsing(self):
        from run_loadtest import parse_weights, DEFAULT_WEIGHTS
        self.assertEqual(parse_weights([]), DEFAULT_WEIGHTS)
        self.assertEqual(parse_weights(["chat=5,healthz=1"]),
                         {"chat": 5, "chat_sse": 0, "sessions": 0,
                          "healthz": 1})
        with self.assertRaises(SystemExit):
            parse_weights(["nope=1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
