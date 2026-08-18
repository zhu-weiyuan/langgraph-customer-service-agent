# -*- coding: utf-8 -*-
"""
纯 stdlib 单测 — hybrid_rag 混合检索模块（无三方依赖，unittest 直接可跑）

    python -m unittest tests.test_rag_pure -v

覆盖：
  * RRF 融合正确性（手算 case）
  * 原始 query 与改写变体同时召回
  * Parent-Child 映射与同 parent 去重
  * RuleReranker 排序（关键词重叠 / 来源权重 / 时间新鲜度）
  * tenant_id / tags 过滤透传（两路）
  * TF-IDF 适配路径（stub 检索器注入，签名兼容）
  * chunk_document 分块不变量
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.hybrid_rag import (  # noqa: E402
    CrossEncoderReranker,
    HybridRetriever,
    QueryRewriter,
    RuleReranker,
    chunk_document,
    filter_by_metadata,
    map_children_to_parents,
    rrf_fuse,
)


def _doc(title, content="", source="kb", **kw):
    d = {"title": title, "content": content, "source": source}
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
# 1. RRF 融合
# ---------------------------------------------------------------------------

class TestRRFFuse(unittest.TestCase):

    def test_hand_computed_scores(self):
        """手算 case：k=60。
        list1: A(1), B(2);  list2: B(1), C(2)
        A = 1/61 ≈ 0.016393
        B = 1/62 + 1/61 ≈ 0.032523
        C = 1/62 ≈ 0.016129
        排序应为 B > A > C。
        """
        l1 = [_doc("A"), _doc("B")]
        l2 = [_doc("B"), _doc("C")]
        fused = rrf_fuse([l1, l2], k=60)
        self.assertEqual([f["title"] for f in fused], ["B", "A", "C"])
        self.assertAlmostEqual(fused[0]["rrf_score"], 1 / 62 + 1 / 61, places=9)
        self.assertAlmostEqual(fused[1]["rrf_score"], 1 / 61, places=9)
        self.assertAlmostEqual(fused[2]["rrf_score"], 1 / 62, places=9)

    def test_rank_starts_at_one(self):
        fused = rrf_fuse([[_doc("X")]], k=10)
        self.assertAlmostEqual(fused[0]["rrf_score"], 1 / 11, places=9)

    def test_dedup_by_id_key(self):
        l1 = [{"id": 7, "title": "旧标题", "content": "a", "source": "s", "score": 0.2}]
        l2 = [{"id": 7, "title": "新标题", "content": "a", "source": "s", "score": 0.9}]
        fused = rrf_fuse([l1, l2], k=60)
        self.assertEqual(len(fused), 1)
        self.assertAlmostEqual(fused[0]["rrf_score"], 2 / 61, places=9)
        self.assertAlmostEqual(fused[0]["orig_score"], 0.9)  # 各路最大原始分

    def test_empty_and_single_list(self):
        self.assertEqual(rrf_fuse([], k=60), [])
        self.assertEqual(rrf_fuse([[]], k=60), [])
        fused = rrf_fuse([[_doc("A"), _doc("B")]], k=60)
        self.assertEqual([f["title"] for f in fused], ["A", "B"])


# ---------------------------------------------------------------------------
# 2. QueryRewriter：原始 + 改写并召回
# ---------------------------------------------------------------------------

class TestQueryRewriter(unittest.TestCase):

    def test_original_always_first(self):
        qr = QueryRewriter(synonyms={"WiFi": ["无线网络"]})
        variants = qr.rewrite("咋连WiFi")
        self.assertEqual(variants[0], "咋连WiFi")
        self.assertGreater(len(variants), 1)

    def test_synonym_expansion_variant(self):
        qr = QueryRewriter(synonyms={"WiFi": ["无线网络"]})
        variants = qr.rewrite("连WiFi")
        self.assertTrue(any("无线网络" in v for v in variants[1:]))

    def test_question_word_stripping(self):
        qr = QueryRewriter(synonyms={})
        variants = qr.rewrite("请问怎么退货呢")
        self.assertIn("退货", variants)  # 剥离疑问词后的核心词

    def test_llm_fn_injection_and_fallback(self):
        qr = QueryRewriter(llm_fn=lambda q: ["变体一", "变体二"], synonyms={})
        self.assertEqual(qr.rewrite("原始查询")[:3], ["原始查询", "变体一", "变体二"])
        # llm 抛异常 → 规则降级，原始仍在首位
        def boom(q):
            raise RuntimeError("llm down")
        qr2 = QueryRewriter(llm_fn=boom, synonyms={"退货": ["退换货"]})
        variants = qr2.rewrite("退货")
        self.assertEqual(variants[0], "退货")
        self.assertTrue(any("退换货" in v for v in variants))

    def test_both_original_and_variants_are_recalled(self):
        """HybridRetriever 应对原始 query 与每个变体都调用检索函数。"""
        seen_queries = []

        def stub_search(query, top_k=10, **kw):
            seen_queries.append(query)
            return [_doc("D-" + query, content=query)]

        r = HybridRetriever(
            keyword_search_fn=stub_search,
            rewriter=QueryRewriter(llm_fn=lambda q: ["改写A", "改写B"], synonyms={}),
        )
        r.search("原始问题")
        self.assertIn("原始问题", seen_queries)
        self.assertIn("改写A", seen_queries)
        self.assertIn("改写B", seen_queries)


# ---------------------------------------------------------------------------
# 3. Parent-Child 分块与映射
# ---------------------------------------------------------------------------

class TestParentChild(unittest.TestCase):

    def test_chunk_document_invariants(self):
        text = "第一段。" * 100 + "\n\n" + "第二段内容。" * 100  # ~1000+ 字
        out = chunk_document(text, child_size=100, parent_size=400, doc_id="d1")
        self.assertTrue(out["parents"])
        self.assertTrue(out["children"])
        for child in out["children"]:
            self.assertIn(child["parent_id"], out["parents"])
            self.assertLessEqual(len(child["text"]), 100 + 50)  # 边界宽容
            parent_text = out["parents"][child["parent_id"]]["text"]
            self.assertIn(child["text"], parent_text)  # child ⊆ parent
        # 全覆盖：children 拼接（同 parent 内）应还原 parent 文本
        for pid, parent in out["parents"].items():
            joined = "".join(c["text"] for c in out["children"]
                             if c["parent_id"] == pid)
            self.assertEqual(joined, parent["text"])

    def test_child_hits_map_to_parent_with_dedup(self):
        parent_map = {
            "d:p0": {"parent_id": "d:p0", "text": "PARENT0 完整上下文", "title": "P0"},
            "d:p1": {"parent_id": "d:p1", "text": "PARENT1 完整上下文", "title": "P1"},
        }
        hits = [
            {"title": "c1", "content": "child1", "score": 0.9, "parent_id": "d:p0", "source": "kb"},
            {"title": "c2", "content": "child2", "score": 0.8, "parent_id": "d:p1", "source": "kb"},
            {"title": "c3", "content": "child3", "score": 0.95, "parent_id": "d:p0", "source": "kb"},
        ]
        mapped = map_children_to_parents(hits, parent_map)
        self.assertEqual(len(mapped), 2)  # p0 去重
        self.assertEqual(mapped[0]["parent_id"], "d:p0")
        self.assertEqual(mapped[0]["content"], "PARENT0 完整上下文")
        self.assertAlmostEqual(mapped[0]["score"], 0.95)  # 同 parent 取最大分
        self.assertEqual(mapped[1]["content"], "PARENT1 完整上下文")

    def test_orphan_hit_passes_through(self):
        hits = [{"title": "t", "content": "无 parent 的命中", "score": 0.5,
                 "parent_id": None, "source": "kb"}]
        mapped = map_children_to_parents(hits, {})
        self.assertEqual(len(mapped), 1)
        self.assertEqual(mapped[0]["content"], "无 parent 的命中")


# ---------------------------------------------------------------------------
# 4. RuleReranker
# ---------------------------------------------------------------------------

class TestRuleReranker(unittest.TestCase):

    def test_keyword_overlap_ordering(self):
        rr = RuleReranker()
        results = [
            _doc("无关文档", "今天天气很好适合出门散步"),
            _doc("退换货政策", "退货 退换货 七天无理由退货流程说明"),
        ]
        out = rr.rerank("怎么退货", results, top_n=2)
        self.assertEqual(out[0]["title"], "退换货政策")

    def test_source_weight(self):
        rr = RuleReranker(source_weights={"official": 1.0}, source_weight=0.5)
        results = [
            _doc("同样内容A", "退货流程说明", source="forum"),
            _doc("同样内容B", "退货流程说明", source="official"),
        ]
        out = rr.rerank("退货", results, top_n=2)
        self.assertEqual(out[0]["source"], "official")

    def test_recency_boost(self):
        rr = RuleReranker(recency_weight=0.5)
        results = [
            _doc("旧文档", "退货流程说明",
                 metadata={"created_at": "2020-01-01T00:00:00+00:00"}),
            _doc("新文档", "退货流程说明",
                 metadata={"created_at": "2026-07-01T00:00:00+00:00"}),
        ]
        out = rr.rerank("退货", results, top_n=2)
        self.assertEqual(out[0]["title"], "新文档")

    def test_bad_created_at_is_safe(self):
        rr = RuleReranker()
        results = [_doc("t", "内容", metadata={"created_at": "not-a-date"})]
        out = rr.rerank("内容", results, top_n=1)
        self.assertEqual(len(out), 1)

    def test_cross_encoder_falls_back_when_unavailable(self):
        # Mock sentence_transformers.CrossEncoder to be unavailable so
        # the reranker falls back to RuleReranker instead of hanging on
        # a HuggingFace model download.
        import unittest.mock as mock
        with mock.patch.dict('sys.modules', {'sentence_transformers': None}):
            # Force re-evaluation of _available by creating a fresh instance
            # with the import guarded.
            ce = CrossEncoderReranker.__new__(CrossEncoderReranker)
            ce.model_name = "whatever/none"
            ce.fallback = RuleReranker()
            ce._model = None
            ce._available = False
            ce._cross_encoder_cls = None
        results = [
            _doc("无关", "天气晴朗"),
            _doc("退货指南", "退货 退换货 流程"),
        ]
        out = ce.rerank("退货", results, top_n=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "退货指南")


# ---------------------------------------------------------------------------
# 5. tenant / tags 过滤透传
# ---------------------------------------------------------------------------

class TestTenantFilter(unittest.TestCase):

    def test_tenant_passed_to_both_paths(self):
        calls = {"dense": [], "sparse": []}

        def dense(query, top_k=10, tenant_id=None, tags=None):
            calls["dense"].append((tenant_id, tuple(tags or [])))
            return [_doc("D", "dense结果", tenant_id=tenant_id)]

        def sparse(query, top_k=10, tenant_id=None, tags=None):
            calls["sparse"].append((tenant_id, tuple(tags or [])))
            return [_doc("S", "sparse结果", tenant_id=tenant_id)]

        r = HybridRetriever(vector_search_fn=dense, keyword_search_fn=sparse,
                            rewriter=QueryRewriter(synonyms={}))
        r.search("查询", tenant_id="acme", tags=["faq"])
        self.assertTrue(calls["dense"])
        self.assertTrue(calls["sparse"])
        for tenant, tags in calls["dense"] + calls["sparse"]:
            self.assertEqual(tenant, "acme")
            self.assertEqual(tags, ("faq",))

    def test_filter_by_metadata(self):
        results = [
            _doc("公共", "c1"),                       # 无 tenant → 保留
            _doc("本租户", "c2", tenant_id="acme"),
            _doc("他租户", "c3", tenant_id="other"),
            _doc("标签命中", "c4", tags=["faq"]),
            _doc("标签不中", "c5", tags=["internal"]),
        ]
        out = filter_by_metadata(results, tenant_id="acme", tags=["faq"])
        titles = [r["title"] for r in out]
        self.assertIn("公共", titles)
        self.assertIn("本租户", titles)
        self.assertIn("标签命中", titles)
        self.assertNotIn("他租户", titles)
        self.assertNotIn("标签不中", titles)

    def test_defensive_post_filter_in_search(self):
        """检索函数不理会 tenant → search() 层仍应过滤掉他租户结果。"""
        def leaky(query, top_k=10, **kw):
            return [_doc("泄漏", "x", tenant_id="other"),
                    _doc("正常", "y", tenant_id="acme")]
        r = HybridRetriever(keyword_search_fn=leaky,
                            rewriter=QueryRewriter(synonyms={}))
        out = r.search("q", tenant_id="acme")
        self.assertEqual([o["title"] for o in out], ["正常"])


# ---------------------------------------------------------------------------
# 6. TF-IDF 适配路径（stub 检索器）与端到端结构
# ---------------------------------------------------------------------------

class TestTfidfAdapterPath(unittest.TestCase):

    def test_stub_legacy_signature_adapted(self):
        """模拟 rag.retrieve 风格：只接受 (query, top_k)，返回 text 字段。"""
        def legacy(query, top_k=10):
            return [{"title": "退换货政策", "text": "七天无理由退货",
                     "score": 3.2, "source": "policy"}]
        r = HybridRetriever(keyword_search_fn=legacy,
                            rewriter=QueryRewriter(synonyms={}))
        out = r.search("退货", tenant_id="acme")  # tenant 不透传也不应崩
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["content"], "七天无理由退货")  # text→content 归一
        self.assertEqual(out[0]["text"], out[0]["content"])   # 兼容别名

    def test_unified_output_schema_and_layering(self):
        def sparse(query, top_k=10, **kw):
            return [_doc(f"doc{i}", f"内容 {query} {i}", score=1.0 - i * 0.01)
                    for i in range(30)]
        def dense(query, top_k=10, **kw):
            return [_doc(f"doc{i}", f"内容 {query} {i}", score=0.9 - i * 0.01)
                    for i in range(10, 40)]
        r = HybridRetriever(vector_search_fn=dense, keyword_search_fn=sparse,
                            recall_top_k=50, rerank_top_n=8, context_top_n=5,
                            rewriter=QueryRewriter(synonyms={}))
        out = r.search("测试")
        self.assertLessEqual(len(out), 5)  # context_top_n
        for item in out:
            self.assertTrue(
                {"title", "content", "text", "score", "source", "parent_id"}
                <= set(item.keys())
            )
            self.assertTrue(
                {"rrf_score", "orig_score", "rerank_score", "lexical_overlap"}
                <= set(item.keys())
            )
            self.assertIsInstance(item["score"], float)
            self.assertGreaterEqual(item["lexical_overlap"], 0.0)

    def test_default_adapter_survives_missing_jieba(self):
        """默认 sparse 适配器在 jieba/rag 不可用时安全返回 []（守卫路径）。"""
        from agent.hybrid_rag import default_tfidf_search
        result = default_tfidf_search("任意查询", top_k=5)
        self.assertIsInstance(result, list)  # 不抛异常即可（本环境可能无 jieba/KB）


# ---------------------------------------------------------------------------
# 7. 评估管线（mock 后端冒烟）
# ---------------------------------------------------------------------------

class TestEvalPipeline(unittest.TestCase):

    def test_mock_eval_runs_and_metrics_sane(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import eval_retrieval as ev
        cases = ev.load_cases()
        self.assertEqual(len(cases), 30)
        for c in cases:
            self.assertIn(c["category"], {"精确码", "口语", "多义", "对抗"})
        report = ev.evaluate(ev.make_mock_retriever(cases), cases)
        self.assertEqual(report["n"], 30)
        self.assertEqual(report["hit_rate_at_5"], 1.0)  # mock 全命中
        # 尾数为 0 的 3 条（e10/e20/e30）在 rank3 命中 → MRR = (27*1 + 3/3)/30
        self.assertAlmostEqual(report["mrr"], (27 + 3 * (1 / 3)) / 30, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
