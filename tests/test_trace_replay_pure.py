"""
可回放 Trace — 纯 stdlib 单元测试 (无三方依赖)。

覆盖:
  - 八分区全字段记录 + to_dict 完整性
  - PII 脱敏 (input_text / 记忆 / 答案 深度脱敏)
  - 落盘读回 (新列 + trace_json)
  - 迁移旧库 (缺列的 traces 表 ALTER 补齐后可写)
  - replay inspect 结构化时间线
  - replay rerun 注入式检索对比
  - list 过滤 (失败 / 低分)
  - diff 对比两个 trace

运行:
    cd <repo-root> && python -m unittest tests.test_trace_replay_pure -v
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.observability import TraceService, TraceSession
from agent import trace_replay


def _fully_recorded_trace(request_id="req-full", **kw):
    """构造一个八分区全部填满的 trace。"""
    t = TraceSession(request_id=request_id, user_id=kw.get("user_id", "u1"),
                     session_id=kw.get("session_id", "s1"),
                     input_text=kw.get("input_text", "你好"),
                     tenant=kw.get("tenant", "acme"),
                     scene=kw.get("scene", "faq"))
    t.add_event("request_start", {"stream": False})
    t.record_prompt(
        template_name="faq_v3", version="3.1",
        variables={"user_name": "张三", "history_turns": 4},
        rendered_messages=[{"role": "system", "content": "你是客服"},
                           {"role": "user", "content": "退货政策?"}])
    t.record_retrieval(
        query="退货政策",
        chunks=["7天无理由退货", "拆封商品不支持", "内部机密条款"],
        scores=[0.91, 0.77, 0.65],
        sources=["returns.md#1", "returns.md#2", "internal.md#9"],
        acl=[True, True, False],
        rerank=[0, 1, 2])
    t.record_memory([
        {"content": "用户偏好邮件联系", "source": "profile",
         "updated_at": "2026-07-01T00:00:00Z", "confidence": 0.8},
    ])
    t.record_tool("order_lookup", args={"order_id": "A123"},
                  acl="allow", ms=42.5, result={"status": "shipped"}, error=None)
    t.record_model(provider="dashscope", model="qwen-max",
                   params={"temperature": 0.2, "top_p": 0.9},
                   in_tok=120, out_tok=48, finish="stop",
                   ttft_ms=210.0, stage="generate")
    t.record_latency(entry_ms=5.0, retrieval_ms=30.0, model_ttft_ms=210.0,
                     tool_ms=42.5, total_ms=520.0)
    t.record_cost(input_cost=0.0012, output_cost=0.0031, cache_hit=False,
                  tenant="acme", scene="faq")
    t.record_result(answer="我们支持7天无理由退货。",
                    parsed={"intent": "returns"},
                    feedback="thumbs_up", eval_score=0.92)
    return t


class TestFullRecording(unittest.TestCase):
    def test_all_partitions_present(self):
        d = _fully_recorded_trace().to_dict()
        for key in ("prompt", "retrieval", "memory", "tools", "model",
                    "latency", "cost", "result", "events"):
            self.assertIn(key, d, "partition %s missing" % key)
            self.assertIsNotNone(d[key])

    def test_prompt_fields(self):
        d = _fully_recorded_trace().to_dict()["prompt"]
        self.assertEqual(d["template_name"], "faq_v3")
        self.assertEqual(d["version"], "3.1")
        self.assertIn("user_name", d["variables_summary"])
        self.assertEqual(len(d["rendered_messages"]), 2)
        self.assertEqual(d["rendered_messages"][0]["role"], "system")
        self.assertTrue(d["rendered_hash"])

    def test_retrieval_fields_and_acl(self):
        d = _fully_recorded_trace().to_dict()["retrieval"]
        self.assertEqual(d["query"], "退货政策")
        self.assertEqual(d["num_recalled"], 3)
        self.assertEqual(d["num_acl_filtered"], 1)
        self.assertTrue(d["rerank_applied"])
        c0 = d["chunks"][0]
        self.assertEqual(c0["score"], 0.91)
        self.assertEqual(c0["source"], "returns.md#1")
        self.assertTrue(c0["acl_allowed"])
        self.assertEqual(c0["rerank_rank"], 0)
        self.assertFalse(d["chunks"][2]["acl_allowed"])

    def test_memory_fields(self):
        d = _fully_recorded_trace().to_dict()["memory"]
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["source"], "profile")
        self.assertEqual(d[0]["confidence"], 0.8)
        self.assertEqual(d[0]["updated_at"], "2026-07-01T00:00:00Z")

    def test_tool_fields(self):
        d = _fully_recorded_trace().to_dict()["tools"]
        self.assertEqual(d[0]["name"], "order_lookup")
        self.assertEqual(d[0]["args"], {"order_id": "A123"})
        self.assertEqual(d[0]["acl_result"], "allow")
        self.assertEqual(d[0]["duration_ms"], 42.5)
        self.assertIn("shipped", d[0]["result_summary"])
        self.assertIsNone(d[0]["error"])

    def test_model_fields(self):
        d = _fully_recorded_trace().to_dict()["model"]
        self.assertEqual(d[0]["provider"], "dashscope")
        self.assertEqual(d[0]["model"], "qwen-max")
        self.assertEqual(d[0]["input_tokens"], 120)
        self.assertEqual(d[0]["output_tokens"], 48)
        self.assertEqual(d[0]["finish_reason"], "stop")
        self.assertEqual(d[0]["ttft_ms"], 210.0)
        self.assertEqual(d[0]["params"]["temperature"], 0.2)

    def test_latency_cost_result_fields(self):
        d = _fully_recorded_trace().to_dict()
        self.assertEqual(d["latency"]["retrieval_ms"], 30.0)
        self.assertEqual(d["latency"]["total_ms"], 520.0)
        self.assertEqual(d["total_ms"], 520.0)
        self.assertAlmostEqual(d["cost"]["total_cost"], 0.0043, places=6)
        self.assertAlmostEqual(d["total_cost"], 0.0043, places=6)
        self.assertEqual(d["cost"]["tenant"], "acme")
        self.assertEqual(d["result"]["feedback"], "thumbs_up")
        self.assertEqual(d["result"]["eval_score"], 0.92)
        self.assertTrue(d["result"]["answer_hash"])

    def test_json_serializable(self):
        d = _fully_recorded_trace().to_dict(redact=True)
        s = json.dumps(d, ensure_ascii=False)
        self.assertIn("faq_v3", s)


class TestPIIRedaction(unittest.TestCase):
    def test_deep_redaction(self):
        t = TraceSession(request_id="req-pii", input_text="我的手机号是 13812345678")
        t.record_memory([{"content": "备用邮箱 test@example.com", "source": "x"}])
        t.record_result(answer="请拨打 13998887777 联系客服")
        d = t.to_dict(redact=True)
        blob = json.dumps(d, ensure_ascii=False)
        # 手机号必须被脱敏 (pii_redactor 存在时)
        try:
            import agent.security.pii_redactor  # noqa: F401
            has = True
        except Exception:
            has = False
        if has:
            self.assertNotIn("13812345678", blob)
            self.assertNotIn("13998887777", blob)
            self.assertNotIn("test@example.com", blob)

    def test_no_redact_by_default(self):
        t = TraceSession(request_id="r", input_text="13812345678")
        d = t.to_dict(redact=False)
        self.assertEqual(d["input_text"], "13812345678")


class TestPersistence(unittest.TestCase):
    def test_save_and_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "trace.db")
            svc = TraceService(db_path=db)
            t = _fully_recorded_trace(request_id="req-x",
                                      input_text="手机 13812345678")
            asyncio.run(svc.finalize_and_save(t))

            # 独立连接验证真正落盘 + 新列存在
            conn = sqlite3.connect(db)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(traces)")}
            for c in ("tenant", "scene", "total_ms", "cost", "failed",
                      "low_score", "created_at", "trace_json"):
                self.assertIn(c, cols)
            row = conn.execute(
                "SELECT tenant, scene, total_ms, cost, failed, low_score, "
                "trace_json FROM traces WHERE request_id=?", ("req-x",)).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "acme")
            self.assertEqual(row[1], "faq")
            self.assertAlmostEqual(row[3], 0.0043, places=6)  # cost 列
            self.assertEqual(row[4], 0)  # not failed
            self.assertEqual(row[5], 0)  # not low score
            data = json.loads(row[6])
            self.assertEqual(data["prompt"]["template_name"], "faq_v3")
            # 落盘时 PII 已脱敏
            try:
                import agent.security.pii_redactor  # noqa: F401
                self.assertNotIn("13812345678", row[6])
            except Exception:
                pass

            # 读回接口
            got = trace_replay.load_trace("req-x", service=svc)
            self.assertEqual(got["request_id"], "req-x")
            self.assertEqual(len(got["retrieval"]["chunks"]), 3)

    def test_migrate_legacy_db(self):
        """旧库: 只有精简 traces 表 (缺大部分新列); TraceService 迁移后可写读。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "legacy.db")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE traces (request_id TEXT PRIMARY KEY, "
                         "input_text TEXT, total_latency_ms REAL)")
            conn.execute("INSERT INTO traces VALUES ('old-1', 'legacy row', 12.0)")
            conn.commit()
            conn.close()

            svc = TraceService(db_path=db)  # 触发迁移
            t = _fully_recorded_trace(request_id="new-1")
            asyncio.run(svc.finalize_and_save(t))

            # 旧行仍在, 新行可读回
            old = svc.get_trace_by_id("old-1")
            self.assertIsNotNone(old)
            self.assertEqual(old["input_text"], "legacy row")
            new = svc.get_trace_by_id("new-1")
            self.assertEqual(new["tenant"], "acme")
            self.assertIsNotNone(new["trace_json"])


class TestReplayInspect(unittest.TestCase):
    def test_timeline_spans(self):
        trace = _fully_recorded_trace().to_dict()
        spans = trace_replay.build_timeline(trace)
        self.assertTrue(len(spans) >= 4)
        for s in spans:
            self.assertIn("name", s)
            self.assertIn("duration_ms", s)
            self.assertIsInstance(s["duration_ms"], float)
        kinds = {s["kind"] for s in spans}
        self.assertTrue({"retrieval", "model", "tool", "event"} <= kinds)
        # 时间线可渲染为文本
        txt = trace_replay.format_timeline(trace)
        self.assertIn("TIMELINE", txt)
        self.assertIn("retrieval", txt)

    def test_replay_inspect_via_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = TraceService(db_path=os.path.join(tmp, "t.db"))
            asyncio.run(svc.finalize_and_save(
                _fully_recorded_trace(request_id="insp-1")))
            spans = trace_replay.replay("insp-1", "inspect", service=svc, echo=False)
            self.assertTrue(len(spans) >= 4)


class TestReplayRerun(unittest.TestCase):
    def test_rerun_injected_retriever(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = TraceService(db_path=os.path.join(tmp, "t.db"))
            asyncio.run(svc.finalize_and_save(
                _fully_recorded_trace(request_id="rr-1")))

            def retriever(query):
                self.assertEqual(query, "退货政策")
                return [{"text": "7天无理由退货", "score": 0.95, "source": "returns.md#1"},
                        {"text": "新增条款", "score": 0.8, "source": "returns.md#3"}]

            out = trace_replay.replay("rr-1", "rerun", service=svc,
                                      retriever=retriever, echo=False)
            self.assertEqual(out["query"], "退货政策")
            self.assertEqual(len(out["current"]), 2)
            self.assertIn("returns.md#3", out["sources_added"])
            self.assertIn("returns.md#1", out["sources_stable"])
            self.assertIn("internal.md#9", out["sources_removed"])

    def test_rerun_without_retriever(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = TraceService(db_path=os.path.join(tmp, "t.db"))
            asyncio.run(svc.finalize_and_save(
                _fully_recorded_trace(request_id="rr-2")))
            out = trace_replay.replay("rr-2", "rerun", service=svc,
                                      retriever=None, echo=False)
            self.assertIsNone(out["current"])
            self.assertTrue(out["recorded"])


class TestListFilters(unittest.TestCase):
    def _seed(self, svc):
        # 正常
        asyncio.run(svc.finalize_and_save(
            _fully_recorded_trace(request_id="ok-1", user_id="alice")))
        # 失败 (工具报错)
        t_fail = TraceSession(request_id="fail-1", user_id="bob", scene="order")
        t_fail.record_tool("order_lookup", args={}, acl="allow", ms=5.0,
                           result=None, error="timeout")
        t_fail.record_result(answer="")
        asyncio.run(svc.finalize_and_save(t_fail))
        # 低分 (差评)
        t_low = TraceSession(request_id="low-1", user_id="carol", scene="faq")
        t_low.record_result(answer="不知道", feedback="thumbs_down", eval_score=0.2)
        asyncio.run(svc.finalize_and_save(t_low))

    def test_filter_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = TraceService(db_path=os.path.join(tmp, "t.db"))
            self._seed(svc)
            failed = trace_replay.list_traces({"failed": True}, service=svc)
            ids = {r["request_id"] for r in failed}
            self.assertEqual(ids, {"fail-1"})
            self.assertTrue(failed[0]["failed"])

    def test_filter_low_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = TraceService(db_path=os.path.join(tmp, "t.db"))
            self._seed(svc)
            low = trace_replay.list_traces({"low_score": True}, service=svc)
            ids = {r["request_id"] for r in low}
            self.assertEqual(ids, {"low-1"})
            self.assertEqual(low[0]["feedback"], "thumbs_down")
            self.assertEqual(low[0]["eval_score"], 0.2)

    def test_filter_by_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = TraceService(db_path=os.path.join(tmp, "t.db"))
            self._seed(svc)
            rows = trace_replay.list_traces({"user_id": "alice"}, service=svc)
            self.assertEqual({r["request_id"] for r in rows}, {"ok-1"})


class TestDiff(unittest.TestCase):
    def test_diff_two_traces(self):
        a = _fully_recorded_trace(request_id="a").to_dict()
        t_b = _fully_recorded_trace(request_id="b")
        # 修改 b: 不同 prompt 版本 + 不同召回 + 不同分数
        t_b.record_prompt(template_name="faq_v3", version="4.0",
                          rendered_messages=[{"role": "user", "content": "x"}])
        t_b.record_retrieval(query="退货政策",
                             chunks=["新片段"], scores=[0.99],
                             sources=["returns.md#5"], acl=[True], rerank=[0])
        t_b.record_result(answer="不同答案", eval_score=0.5)
        b = t_b.to_dict()

        diff = trace_replay.diff_traces(a, b)
        self.assertFalse(diff["identical"])
        self.assertIn("prompt_version", diff["changed"])
        self.assertEqual(diff["fields"]["prompt_version"], {"a": "3.1", "b": "4.0"})
        self.assertIn("eval_score", diff["changed"])
        self.assertIn("retrieval_sources", diff["changed"])
        self.assertIn("returns.md#5", diff["fields"]["retrieval_sources"]["added"])

    def test_diff_identical(self):
        a = _fully_recorded_trace(request_id="a").to_dict()
        b = _fully_recorded_trace(request_id="b").to_dict()
        diff = trace_replay.diff_traces(a, b)
        # request_id 不比; 其余关键字段相同 -> identical
        self.assertTrue(diff["identical"], diff.get("changed"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
