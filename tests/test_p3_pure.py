"""
P3 可观测性栈 — 纯 stdlib 单元测试 (无三方依赖, 覆盖降级路径)。

运行:
    cd <repo-root> && python -m unittest tests.test_p3_pure -v
"""

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.metrics import Metrics
from agent.observability import AlertService, TraceService, TraceSession
from agent.logging_setup import (
    JsonFormatter,
    bind_request_context,
    clear_request_context,
)

# Prometheus 文本格式正则
METRIC_NAME_RE = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
TYPE_LINE_RE = re.compile(
    rf"^# TYPE ({METRIC_NAME_RE}) (counter|gauge|histogram|summary|untyped)$")
HELP_LINE_RE = re.compile(rf"^# HELP ({METRIC_NAME_RE}) .*$")
SAMPLE_LINE_RE = re.compile(
    rf'^({METRIC_NAME_RE})'
    r'(\{[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*"'
    r'(,[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*")*\})?'
    r' (-?\d+(\.\d+)?([eE][+-]?\d+)?|\+Inf|-Inf|NaN)$')


class TestFallbackPrometheusFormat(unittest.TestCase):
    """降级模式下 Prometheus 文本格式合法性。"""

    def setUp(self):
        self.m = Metrics(use_prometheus_client=False)
        self.assertFalse(self.m.using_prometheus_client)
        # 填充所有指标
        self.m.record_http_request("POST", "/api/chat", 200, 0.42)
        self.m.record_http_request("POST", "/api/chat", 500, 1.5)
        self.m.record_http_request("GET", "/api/health", 200, 0.003)
        self.m.record_llm_tokens("qwen3.5", "faq", "input", 120)
        self.m.record_llm_tokens("qwen3.5", "faq", "output", 300)
        self.m.record_llm_cost("qwen3.5", 0.05)
        self.m.record_llm_request("qwen3.5", "success")
        self.m.record_node_duration("rag_retrieve", 0.2)
        self.m.set_rag_hit_ratio(0.87)
        self.m.record_rate_limit_event("free")
        self.m.set_circuit_breaker_state("llm", "open")
        self.m.record_cache_event("redis", "hit")
        self.m.record_cache_event("redis", "miss")
        self.m.record_feedback("thumbs_up")
        self.text, self.content_type = self.m.render()

    def test_content_type(self):
        self.assertIn("text/plain", self.content_type)

    def test_every_line_valid(self):
        """每一行必须是 HELP / TYPE / 合法样本行。"""
        for line in self.text.strip().split("\n"):
            self.assertTrue(
                TYPE_LINE_RE.match(line)
                or HELP_LINE_RE.match(line)
                or SAMPLE_LINE_RE.match(line),
                f"invalid prometheus line: {line!r}",
            )

    def test_type_lines_have_bare_names(self):
        """# TYPE 行只含裸指标名: 无大括号、无 label。"""
        for line in self.text.split("\n"):
            if line.startswith("# TYPE"):
                self.assertNotIn("{", line, f"TYPE line contains labels: {line!r}")
                self.assertNotIn("}", line, f"TYPE line contains labels: {line!r}")
                self.assertTrue(TYPE_LINE_RE.match(line), f"bad TYPE line: {line!r}")

    def test_labels_on_sample_lines(self):
        """label 出现在样本行且格式正确。"""
        m = re.search(
            r'^http_requests_total\{method="POST",endpoint="/api/chat",status="200"\} 1$',
            self.text, re.M)
        self.assertIsNotNone(m, "labelled counter sample missing")

    def test_histogram_structure(self):
        """histogram 输出 _bucket{le=} / _sum / _count, 且 +Inf 桶存在。"""
        self.assertRegex(
            self.text,
            r'http_request_duration_seconds_bucket\{method="POST",endpoint="/api/chat",le="\+Inf"\} 2')
        self.assertRegex(
            self.text,
            r'http_request_duration_seconds_count\{method="POST",endpoint="/api/chat"\} 2')
        self.assertRegex(
            self.text,
            r'http_request_duration_seconds_sum\{method="POST",endpoint="/api/chat"\} ')
        # 桶单调性
        buckets = re.findall(
            r'http_request_duration_seconds_bucket\{method="POST",endpoint="/api/chat",le="([^"]+)"\} (\d+)',
            self.text)
        counts = [int(c) for _, c in buckets]
        self.assertEqual(counts, sorted(counts), "bucket counts must be cumulative")

    def test_gauge_and_expected_metrics_present(self):
        for name in ("rag_hit_ratio", "circuit_breaker_state", "llm_tokens_total",
                     "llm_cost_yuan_total", "llm_requests_total",
                     "node_duration_seconds", "rate_limit_events_total",
                     "cache_events_total", "feedback_events_total"):
            self.assertIn(f"# TYPE {name} ", self.text, f"metric {name} missing")
        self.assertIsNotNone(
            re.search(r"^rag_hit_ratio 0\.87$", self.text, re.M),
            "unlabelled gauge sample missing")
        self.assertRegex(self.text, r'circuit_breaker_state\{name="llm"\} 2')


class TestSlidingWindowAlerts(unittest.TestCase):
    """滑动窗口告警: 注入时间函数, 验证窗口过期与冷却。"""

    def setUp(self):
        self.now = [1000.0]
        self.svc = AlertService(time_fn=lambda: self.now[0])

    def test_window_expiry(self):
        self.svc.add_rule("high_latency", "latency_ms", threshold=500,
                          agg="avg", window_seconds=60, cooldown_seconds=0)
        # 窗口内: 高延迟观测
        self.svc.record("latency_ms", 900)
        self.svc.record("latency_ms", 800)
        fired = self.svc.check_and_alert()
        self.assertEqual([f["alert"] for f in fired], ["high_latency"])
        # 时间前进 120s: 旧样本过期, 窗口空 -> 不触发
        self.now[0] += 120
        self.assertEqual(self.svc.check_and_alert(), [])
        # 新的低延迟样本 -> 仍不触发
        self.svc.record("latency_ms", 100)
        self.assertEqual(self.svc.check_and_alert(), [])

    def test_cooldown(self):
        self.svc.add_rule("err", "errors", threshold=2, agg="count",
                          window_seconds=300, cooldown_seconds=600)
        for _ in range(5):
            self.svc.record("errors", 1)
        self.assertEqual(len(self.svc.check_and_alert()), 1)
        # 冷却期内不重复触发
        self.now[0] += 60
        self.assertEqual(self.svc.check_and_alert(), [])
        # 冷却结束且窗口内仍有足够样本 -> 再次触发
        self.now[0] += 600
        for _ in range(5):
            self.svc.record("errors", 1)
        self.assertEqual(len(self.svc.check_and_alert()), 1)

    def test_handler_called(self):
        seen = []
        self.svc.add_handler(seen.append)
        self.svc.add_rule("max_rule", "v", threshold=10, agg="max",
                          window_seconds=60, cooldown_seconds=0)
        self.svc.record("v", 99)
        self.svc.check_and_alert()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["alert"], "max_rule")
        self.assertEqual(seen[0]["value"], 99)


class TestJsonLogging(unittest.TestCase):
    """降级 JSON 日志: 单行、含 request_id/session_id。"""

    def test_json_log_contains_request_id(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        lg = logging.getLogger("test.p3.json")
        lg.handlers = [handler]
        lg.setLevel(logging.INFO)
        lg.propagate = False

        tokens = bind_request_context(request_id="req-123", session_id="sess-9")
        try:
            lg.info("hello world")
        finally:
            clear_request_context(tokens)
        lg.info("after clear")

        lines = stream.getvalue().strip().split("\n")
        self.assertEqual(len(lines), 2)
        rec1 = json.loads(lines[0])
        self.assertEqual(rec1["msg"], "hello world")
        self.assertEqual(rec1["request_id"], "req-123")
        self.assertEqual(rec1["session_id"], "sess-9")
        for key in ("ts", "level", "logger"):
            self.assertIn(key, rec1)
        # 清理后 request_id 为空
        rec2 = json.loads(lines[1])
        self.assertEqual(rec2["request_id"], "")


class TestTraceService(unittest.TestCase):
    """trace 落 SQLite 可读回 (finalize_and_save 异步入口)。"""

    def test_finalize_and_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "trace.db")
            svc = TraceService(db_path=db_path)

            trace = TraceSession(request_id="req-abc", user_id="u1",
                                 input_text="我的手机号是 13812345678",
                                 session_id="sess-1")
            trace.add_event("node_start", {"node": "rag"}, duration_ms=12.5)
            trace.add_event("node_end", {"node": "rag"}, duration_ms=30.0)

            asyncio.run(svc.finalize_and_save(trace))

            # 通过独立连接读回, 验证真正落盘
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT request_id, session_id, user_id, input_text, trace_json "
                "FROM traces WHERE request_id=?", ("req-abc",)).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "req-abc")
            self.assertEqual(row[1], "sess-1")
            self.assertEqual(row[2], "u1")
            data = json.loads(row[4])
            self.assertEqual(len(data["events"]), 2)
            self.assertEqual(data["request_id"], "req-abc")
            self.assertGreaterEqual(data["total_latency_ms"], 0)

            # PII 钩子: pii_redactor 可导入时手机号必须被脱敏
            try:
                import agent.security.pii_redactor  # noqa: F401
                has_redactor = True
            except Exception:
                has_redactor = False
            if has_redactor:
                self.assertNotIn("13812345678", row[3])

            # 服务层查询接口
            got = svc.get_trace_by_id("req-abc")
            self.assertIsNotNone(got)
            self.assertEqual(got["request_id"], "req-abc")
            stats = svc.get_stats()
            self.assertEqual(stats["total_requests"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
