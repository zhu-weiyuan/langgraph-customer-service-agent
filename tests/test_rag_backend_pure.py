# -*- coding: utf-8 -*-
"""
纯 stdlib 单测 — RAG 后端选择 / EmbeddingClient / ingest dry-run

    python -m unittest tests.test_rag_backend_pure -v

覆盖：
  * select_backend env 矩阵（未设置/tfidf/hybrid/pgvector/大小写/非法值）
  * pgvector 运行期失败 → TF-IDF 降级（mock store 抛异常）
  * EmbeddingClient 批量（≤32/次）、Authorization 头构造、顺序保持、重试
  * ingest --dry-run 在临时 knowledge 目录上的分块统计
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import rag_backend  # noqa: E402
from agent.embedding_client import (  # noqa: E402
    EmbeddingClient, batched, build_headers)
from agent.pgvector_hybrid import PgHybridStore


# ---------------------------------------------------------------------------
# 1. 后端选择 env 矩阵
# ---------------------------------------------------------------------------

class TestSelectBackend(unittest.TestCase):

    def test_explicit_value_matrix(self):
        cases = {
            "": "tfidf", "tfidf": "tfidf", "TFIDF": "tfidf",
            "hybrid": "hybrid", "Hybrid": "hybrid", " HYBRID ": "hybrid",
            "pgvector": "pgvector", "PGVECTOR": "pgvector",
            "bogus": "tfidf", "pg": "tfidf",
        }
        for raw, expected in cases.items():
            self.assertEqual(rag_backend.select_backend(raw), expected,
                             msg=f"value={raw!r}")

    def test_env_matrix(self):
        for env_val, expected in [(None, "tfidf"), ("", "tfidf"),
                                  ("tfidf", "tfidf"), ("hybrid", "hybrid"),
                                  ("pgvector", "pgvector"), ("junk", "tfidf")]:
            env = dict(os.environ)
            env.pop("RAG_BACKEND", None)
            if env_val is not None:
                env["RAG_BACKEND"] = env_val
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(rag_backend.select_backend(), expected,
                                 msg=f"RAG_BACKEND={env_val!r}")


# ---------------------------------------------------------------------------
# 2. pgvector 运行期失败 → TF-IDF 降级
# ---------------------------------------------------------------------------

class TestBackendFallback(unittest.TestCase):

    def test_pgvector_failure_falls_back_to_tfidf(self):
        def broken_pg(query, top_k):
            raise ConnectionError("pg down")
        fallback_hits = [{"title": "TFIDF", "text": "fallback", "score": 1.0,
                          "source": "kb"}]
        calls = []

        def fallback(query, top_k):
            calls.append((query, top_k))
            return fallback_hits

        with self.assertLogs("agent.rag_backend", level="WARNING") as logs:
            out = rag_backend.retrieve_with_backend(
                "q", 3, "pgvector", pg_search_fn=broken_pg, fallback_fn=fallback)
        self.assertEqual(out, fallback_hits)
        self.assertEqual(calls, [("q", 3)])
        self.assertTrue(any("pgvector" in m for m in logs.output))

    def test_pgvector_success_no_fallback(self):
        hits = [{"title": "PG", "content": "x", "score": 0.5, "source": "pg"}]
        out = rag_backend.retrieve_with_backend(
            "q", 3, "pgvector",
            pg_search_fn=lambda q, k: hits,
            fallback_fn=lambda q, k: self.fail("must not fall back"))
        self.assertEqual(out, hits)

    def test_hybrid_failure_falls_back(self):
        def broken(query, top_k):
            raise RuntimeError("no deps")
        out = rag_backend.retrieve_with_backend(
            "q", 2, "hybrid", hybrid_search_fn=broken,
            fallback_fn=lambda q, k: [{"title": "FB"}])
        self.assertEqual(out, [{"title": "FB"}])

    def test_tfidf_backend_uses_default_path(self):
        out = rag_backend.retrieve_with_backend(
            "q", 2, "tfidf",
            pg_search_fn=lambda q, k: self.fail("wrong path"),
            fallback_fn=lambda q, k: [{"title": "LEGACY", "k": k}])
        self.assertEqual(out, [{"title": "LEGACY", "k": 2}])

    def test_double_failure_returns_empty(self):
        def boom(query, top_k):
            raise RuntimeError("x")
        out = rag_backend.retrieve_with_backend(
            "q", 2, "pgvector", pg_search_fn=boom, fallback_fn=boom)
        self.assertEqual(out, [])

    def test_pgvector_strict_mode_does_not_fallback(self):
        def broken_pg(query, top_k):
            raise ConnectionError("pg down")

        with mock.patch.dict(os.environ, {"RAG_STRICT": "1"}):
            with self.assertRaises(ConnectionError):
                rag_backend.retrieve_with_backend(
                    "q", 3, "pgvector", pg_search_fn=broken_pg,
                    fallback_fn=lambda q, k: self.fail("must not fall back"))


class TestPgvectorRuntimeCache(unittest.TestCase):

    def tearDown(self):
        rag_backend.reset_cache()

    def test_rule_reranker_is_constructed_once(self):
        class FakeStore:
            def hybrid_search(self, query, top_k):
                return []

            def load_parent_map(self):
                return {}

        fake_reranker = mock.Mock()
        fake_reranker.rerank.return_value = []
        rag_backend.reset_cache()
        with mock.patch.dict(os.environ, {"RAG_RERANKER": "rule"}), \
             mock.patch("agent.embedding_client.EmbeddingClient.from_env",
                        return_value=None), \
             mock.patch("agent.pgvector_hybrid.PgHybridStore.from_env",
                        return_value=FakeStore()), \
             mock.patch("agent.hybrid_rag.RuleReranker",
                        return_value=fake_reranker) as reranker_cls:
            first = rag_backend._get_pg_search_fn()
            second = rag_backend._get_pg_search_fn()
            first("q1", 3)
            second("q2", 3)

        self.assertEqual(reranker_cls.call_count, 1)
        self.assertEqual(fake_reranker.rerank.call_count, 2)


# ---------------------------------------------------------------------------
# 3. EmbeddingClient
# ---------------------------------------------------------------------------

class _MockTransport:
    """记录请求；可编排失败次数；返回乱序 index 校验排序逻辑。"""

    def __init__(self, fail_times=0, status_after_fail=200):
        self.calls = []
        self.fail_times = fail_times
        self.status = status_after_fail

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": dict(headers),
                           "payload": payload, "timeout": timeout})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("network glitch")
        texts = payload["input"]
        data = [{"index": i, "embedding": [float(i), 0.5]}
                for i in range(len(texts))]
        return self.status, {"data": list(reversed(data))}  # 故意乱序


class TestEmbeddingClient(unittest.TestCase):

    def test_batched_pure(self):
        self.assertEqual(batched(list(range(70)), 32),
                         [list(range(32)), list(range(32, 64)),
                          list(range(64, 70))])
        self.assertEqual(batched([], 32), [])

    def test_auth_header_construction(self):
        headers = build_headers("sk-test-123")
        self.assertEqual(headers["Authorization"], "Bearer sk-test-123")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_batching_and_headers_via_transport(self):
        transport = _MockTransport()
        client = EmbeddingClient(api_key="sk-abc", base_url="https://gw/v1/",
                                 model="test-embed", transport=transport)
        texts = [f"t{i}" for i in range(70)]
        vecs = client.embed(texts)
        self.assertEqual(len(vecs), 70)
        self.assertEqual(len(transport.calls), 3)  # 32 + 32 + 6
        sizes = [len(c["payload"]["input"]) for c in transport.calls]
        self.assertEqual(sizes, [32, 32, 6])
        for call in transport.calls:
            self.assertEqual(call["url"], "https://gw/v1/embeddings")
            self.assertEqual(call["headers"]["Authorization"], "Bearer sk-abc")
            self.assertEqual(call["payload"]["model"], "test-embed")
        # 响应乱序 → 客户端按 index 排序，顺序保持
        self.assertEqual(vecs[0][0], 0.0)
        self.assertEqual(vecs[31][0], 31.0)
        self.assertEqual(vecs[64][0], 0.0)  # 第三批的第 0 条

    def test_retry_then_success(self):
        transport = _MockTransport(fail_times=2)  # 2 次失败 + 第 3 次成功
        client = EmbeddingClient(api_key="k", transport=transport,
                                 max_retries=2)
        with mock.patch("time.sleep"):
            vecs = client.embed(["a"])
        self.assertEqual(len(vecs), 1)
        self.assertEqual(len(transport.calls), 3)

    def test_retries_exhausted_raises(self):
        transport = _MockTransport(fail_times=99)
        client = EmbeddingClient(api_key="k", transport=transport,
                                 max_retries=2)
        with mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                client.embed(["a"])
        self.assertEqual(len(transport.calls), 3)  # 1 + 2 重试

    def test_http_error_status_raises_with_hint(self):
        transport = _MockTransport(status_after_fail=401)
        client = EmbeddingClient(api_key="bad", transport=transport,
                                 max_retries=0)
        with mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                client.embed(["a"])
        self.assertIn("401", str(ctx.exception))

    def test_from_env_strict_and_lenient(self):
        # Exclude ALL embedding-related API key vars so from_env() sees none;
        # previous test imports may have loaded .env setting EMBEDDING_API_KEY
        # or MY_AGENT_API_KEY into os.environ.
        _key_vars = {"OPENAI_API_KEY", "EMBEDDING_API_KEY", "MY_AGENT_API_KEY"}
        env = {k: v for k, v in os.environ.items() if k not in _key_vars}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(EmbeddingClient.from_env(strict=False))
            with self.assertRaises(ValueError) as ctx:
                EmbeddingClient.from_env(strict=True)
            # Error message lists the primary key variable
            self.assertIn("OPENAI_API_KEY", str(ctx.exception))
        # Clear process-level embedding settings loaded from the project .env;
        # this case specifically verifies the OPENAI_BASE_URL fallback.
        with mock.patch.dict(os.environ, {
                "OPENAI_API_KEY": "sk-x",
                "OPENAI_BASE_URL": "https://my-gw/v1",
                "EMBEDDING_MODEL": "bge-m3"}, clear=True):
            client = EmbeddingClient.from_env()
            self.assertEqual(client.endpoint, "https://my-gw/v1/embeddings")
            self.assertEqual(client.model, "bge-m3")
            self.assertEqual(client.headers()["Authorization"], "Bearer sk-x")

    def test_missing_api_key_direct(self):
        with self.assertRaises(ValueError):
            EmbeddingClient(api_key="")

    def test_from_env_reads_timeout_and_retry_budget(self):
        with mock.patch.dict(os.environ, {
                "OPENAI_API_KEY": "sk-x",
                "EMBEDDING_TIMEOUT_SECONDS": "2.5",
                "EMBEDDING_MAX_RETRIES": "3",
        }, clear=False):
            client = EmbeddingClient.from_env()
        self.assertEqual(client.timeout, 2.5)
        self.assertEqual(client.max_retries, 3)


    def test_oversized_embedding_requires_explicit_truncation(self):
        def transport(url, headers, payload, timeout):
            return 200, {"data": [{"index": 0,
                                   "embedding": [1.0, 2.0, 3.0, 4.0]}]}

        strict = EmbeddingClient(api_key="k", dimensions=2,
                                 transport=transport)
        with self.assertRaisesRegex(RuntimeError, "EMBEDDING_ALLOW_TRUNCATION"):
            strict.embed_one("hello")

        reduced = EmbeddingClient(api_key="k", dimensions=2,
                                  allow_truncation=True, transport=transport)
        vector = reduced.embed_one("hello")
        self.assertEqual(len(vector), 2)
        self.assertAlmostEqual(sum(value * value for value in vector), 1.0)

    def test_from_env_reads_explicit_truncation_flag(self):
        with mock.patch.dict(os.environ, {
                "OPENAI_API_KEY": "sk-x",
                "EMBEDDING_DIMENSIONS": "1024",
                "EMBEDDING_ALLOW_TRUNCATION": "1",
        }, clear=False):
            client = EmbeddingClient.from_env()
        self.assertEqual(client.dimensions, 1024)
        self.assertTrue(client.allow_truncation)



# ---------------------------------------------------------------------------
# 4. ingest --dry-run（临时 knowledge 目录）
# ---------------------------------------------------------------------------

class TestPgVectorKeywordFallback(unittest.TestCase):

    def test_embedding_failure_uses_pg_keyword_search(self):
        calls = []

        def broken_embed(_query):
            calls.append("embed")
            raise TimeoutError("embedding timeout")

        store = PgHybridStore("postgresql://unused", embed_fn=broken_embed)
        expected = [{"id": "c1", "content": "WiFi 连接失败", "score": 1.0}]
        store.keyword_search = mock.Mock(return_value=expected)
        store._embedding_failure_cooldown = 60.0

        self.assertEqual(store.hybrid_search("智能门锁 WiFi", top_k=3), expected)
        self.assertEqual(store.hybrid_search("智能门锁 WiFi", top_k=3), expected)
        self.assertEqual(calls, ["embed"])
        self.assertEqual(store.keyword_search.call_count, 2)

    def test_empty_embedding_never_reaches_vector_sql(self):
        store = PgHybridStore("postgresql://unused", embed_fn=lambda _q: [])
        store.keyword_search = mock.Mock(return_value=[{"id": "c2"}])
        self.assertEqual(store.hybrid_search("故障码 E018", top_k=3), [{"id": "c2"}])
        store.keyword_search.assert_called_once()


class TestIngestDryRun(unittest.TestCase):

    def _make_kb(self, tmp):
        (Path(tmp) / "faq.md").write_text(
            "# 常见问题\n" + "退货流程说明。" * 300, encoding="utf-8")   # ~2100 字
        (Path(tmp) / "warranty.md").write_text(
            "# 保修\n" + "保修期两年。" * 100, encoding="utf-8")        # ~600 字

    def test_collect_chunk_stats(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import ingest_knowledge as ing
        with tempfile.TemporaryDirectory() as tmp:
            self._make_kb(tmp)
            stats = ing.collect_chunk_stats(Path(tmp), child_size=300,
                                            parent_size=1200)
        self.assertEqual(stats["files"], 2)
        self.assertGreaterEqual(stats["parents"], 2)   # section-based: 1 parent per heading × 2 files = 2
        self.assertGreater(stats["children"], stats["parents"])
        self.assertEqual(len(stats["per_file"]), 2)
        for entry in stats["per_file"]:
            self.assertGreater(entry["children"], 0)

    def test_dry_run_cli_missing_dir_is_safe(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import ingest_knowledge as ing
        rc = ing.main(["--dry-run", "--kb-dir", "/nonexistent/kb"])
        self.assertEqual(rc, 0)  # 容器内无 knowledge/ 属预期，不报错

    def test_dry_run_cli_with_temp_kb(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import ingest_knowledge as ing
        with tempfile.TemporaryDirectory() as tmp:
            self._make_kb(tmp)
            rc = ing.main(["--dry-run", "--kb-dir", tmp])
        self.assertEqual(rc, 0)

    def test_parse_index_version(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import ingest_knowledge as ing
        self.assertEqual(ing.parse_index_version("v1"), 1)
        self.assertEqual(ing.parse_index_version("2"), 2)
        self.assertEqual(ing.parse_index_version("v10-beta"), 10)
        self.assertEqual(ing.parse_index_version(""), 1)
        self.assertEqual(ing.parse_index_version(None), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
