# -*- coding: utf-8 -*-
"""P2 app 集成层纯 stdlib 单测（无 fastapi/langgraph/网络依赖）。

覆盖：
- agent/runner.py：initial state 组装、结果解析(__interrupt__)、请求级超时、
  流式帧序列（组合模式 + 降级模式）
- agent/http_helpers.py：SSE 帧序列化、幂等键提取、sessions/analytics/
  session-detail SQL（内存 sqlite 造数）、admin 授权判定与状态机分发(mock registry)

运行：python -m unittest tests.test_p2_pure -v
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent import runner                                             # noqa: E402
from agent.http_helpers import (                                     # noqa: E402
    admin_auth_status, admin_prompt_action, idempotency_key_from_headers,
    query_analytics, query_session_detail, query_sessions, sse_format)


def _run(coro):
    return asyncio.run(coro)


# ════════════════════════════════════════════════════════════════════
# runner: 状态组装 / 结果解析
# ════════════════════════════════════════════════════════════════════

class TestBuildInitialState(unittest.TestCase):
    def test_basic_shape(self):
        state = runner.build_initial_state("s1", "你好")
        self.assertEqual(state["session_id"], "s1")
        self.assertEqual(state["retry_count"], 0)
        self.assertEqual(state["emotion"], "neutral")
        self.assertEqual(state["emotion_intensity"], 1)
        self.assertEqual(len(state["messages"]), 1)
        self.assertEqual(state["messages"][0].content, "你好")
        self.assertNotIn("idempotency_key", state)
        self.assertNotIn("trace_session", state)

    def test_emotion_carryover_and_extras(self):
        trace = object()
        state = runner.build_initial_state(
            "s1", "hi", prev_values={"emotion": "angry", "emotion_intensity": 4},
            trace_session=trace, idempotency_key="idem-1")
        self.assertEqual(state["emotion"], "angry")
        self.assertEqual(state["emotion_intensity"], 4)
        # trace 对象不入 state(msgpack 序列化会崩),只放 request_id
        self.assertEqual(state["request_id"], getattr(trace, "request_id", ""))
        self.assertEqual(state["idempotency_key"], "idem-1")

    def test_falsy_prev_emotion_defaults(self):
        state = runner.build_initial_state(
            "s1", "hi", prev_values={"emotion": None, "emotion_intensity": 0})
        self.assertEqual(state["emotion"], "neutral")
        self.assertEqual(state["emotion_intensity"], 1)


class TestParseResult(unittest.TestCase):
    def _values(self, msgs, **extra):
        v = {"messages": msgs, "intent": "consult", "emotion": "neutral",
             "emotion_intensity": 1, "retry_count": 0}
        v.update(extra)
        return v

    def test_new_ai_replies_only(self):
        msgs = [runner.HumanMessage(content="旧问"),
                runner.AIMessage(content="旧答"),
                runner.HumanMessage(content="新问"),
                runner.AIMessage(content="这是新答复")]
        out = runner.parse_result(self._values(msgs), existing_count=2,
                                  session_id="s1")
        self.assertEqual(len(out["replies"]), 1)
        self.assertEqual(out["replies"][0]["content"], "这是新答复")
        self.assertEqual(out["replies"][0]["type"], "reply")
        self.assertFalse(out["interrupted"])
        self.assertEqual(out["next_action"], "Active")
        self.assertEqual(out["session_id"], "s1")

    def test_interrupt_detection(self):
        out = runner.parse_result(
            self._values([], __interrupt__=[{"value": "human"}]),
            existing_count=0, session_id="s1")
        self.assertTrue(out["interrupted"])
        self.assertEqual(out["reply_type"], "escalated")
        self.assertEqual(out["next_action"], "Escalated")

    def test_escalate_flag_detection(self):
        out = runner.parse_result(self._values([], escalate=True),
                                  existing_count=0, session_id="s1")
        self.assertTrue(out["interrupted"])

    def test_classify_types(self):
        self.assertEqual(runner.classify_message("请问您对服务还满意吗"), "satisfaction")
        self.assertEqual(runner.classify_message("再见，祝您生活愉快"), "closing")
        self.assertEqual(runner.classify_message("音箱重启即可"), "reply")

    def test_chunk_text(self):
        self.assertEqual(runner.chunk_text(""), [])
        self.assertEqual(runner.chunk_text("abcdef", 4), ["abcd", "ef"])
        self.assertEqual("".join(runner.chunk_text("x" * 100)), "x" * 100)


# ════════════════════════════════════════════════════════════════════
# runner: run() with mock graph（config 传递 + 超时）
# ════════════════════════════════════════════════════════════════════

class FakeSnapshot(SimpleNamespace):
    pass


class FakeGraph:
    def __init__(self, result_values=None, prev_values=None, delay=0.0):
        self.result_values = result_values or {}
        self.prev_values = prev_values or {}
        self.delay = delay
        self.seen_configs = []
        self.seen_states = []

    async def aget_state(self, config):
        return FakeSnapshot(values=self.prev_values)

    async def ainvoke(self, state, config=None):
        self.seen_states.append(state)
        self.seen_configs.append(config)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result_values


class TestRunnerRun(unittest.TestCase):
    def test_run_happy_path(self):
        prev = {"messages": [runner.HumanMessage("a"), runner.AIMessage("b")],
                "emotion": "sad", "emotion_intensity": 3}
        final = {"messages": [runner.HumanMessage("a"), runner.AIMessage("b"),
                              runner.HumanMessage("新问"), runner.AIMessage("新答")],
                 "intent": "complaint", "emotion": "sad",
                 "emotion_intensity": 3, "retry_count": 1}
        graph = FakeGraph(result_values=final, prev_values=prev)
        out = _run(runner.run("sess-9", "新问", graph=graph, timeout=5))
        self.assertEqual(graph.seen_configs[0],
                         {"configurable": {"thread_id": "sess-9"}})
        # 情绪从上一轮延续进 initial state
        self.assertEqual(graph.seen_states[0]["emotion"], "sad")
        self.assertEqual(out["replies"], [{"type": "reply", "content": "新答"}])
        self.assertEqual(out["intent"], "complaint")
        self.assertEqual(out["retry_count"], 1)

    def test_run_timeout(self):
        graph = FakeGraph(delay=0.5)
        with self.assertRaises(asyncio.TimeoutError):
            _run(runner.run("s", "hi", graph=graph, timeout=0.05))

    def test_run_records_trace_events(self):
        events = []

        class Trace:
            def add_event(self, event_type, data, duration_ms=0):
                events.append((event_type, data))

        graph = FakeGraph(result_values={"messages": []})
        _run(runner.run("s", "hi", graph=graph, timeout=5,
                        trace_session=Trace()))
        types = [e[0] for e in events]
        self.assertIn("graph_execution", types)
        statuses = [d.get("status") for _, d in events]
        self.assertIn("started", statuses)
        self.assertIn("completed", statuses)

    def test_graph_timeout_env_default(self):
        self.assertEqual(runner.DEFAULT_GRAPH_TIMEOUT, 120.0)
        self.assertGreater(runner.graph_timeout_seconds(), 0)


# ════════════════════════════════════════════════════════════════════
# runner: run_stream 帧序列
# ════════════════════════════════════════════════════════════════════

class CombinedStreamGraph(FakeGraph):
    """支持 stream_mode=["messages","updates"] 的 mock。"""

    def astream(self, state, config=None, stream_mode=None):
        assert stream_mode == ["messages", "updates"]

        async def gen():
            yield ("updates", {"identify_intent": {"intent": "consult"}})
            yield ("messages", (SimpleNamespace(content="你好"), {}))
            yield ("messages", (SimpleNamespace(content="，很高兴"), {}))
            yield ("updates", {"generate_reply": {"bot_reply": "你好，很高兴"}})
        return gen()


class UpdatesOnlyGraph(FakeGraph):
    """不支持组合 stream_mode（旧版 langgraph）→ 降级 'updates'。"""

    def astream(self, state, config=None, stream_mode=None):
        if isinstance(stream_mode, list):
            raise TypeError("stream_mode list unsupported")
        assert stream_mode == "updates"

        async def gen():
            yield {"identify_intent": {"intent": "consult", "ending": False}}
            yield {"generate_reply": {
                "bot_reply": "这是一段完整的节点级降级回复内容",
                "messages": [runner.AIMessage("这是一段完整的节点级降级回复内容")]}}
        return gen()


class InterruptStreamGraph(FakeGraph):
    def astream(self, state, config=None, stream_mode=None):
        async def gen():
            if isinstance(stream_mode, list):
                yield ("updates", {"process_satisfaction": {"retry_count": 2}})
                yield ("updates", {"__interrupt__": ({"value": "human"},)})
            else:  # pragma: no cover
                yield {"__interrupt__": ({"value": "human"},)}
        return gen()


class TestRunnerStream(unittest.TestCase):
    def _collect(self, graph):
        async def go():
            frames = []
            async for f in runner.run_stream("s1", "hi", graph=graph, timeout=5):
                frames.append(f)
            return frames
        return _run(go())

    def test_combined_mode_token_frames(self):
        frames = self._collect(CombinedStreamGraph())
        tokens = [f["token"] for f in frames if "token" in f]
        self.assertEqual("".join(tokens), "你好，很高兴")
        progress = [f["progress"] for f in frames if "progress" in f]
        self.assertIn("identify_intent", progress)
        done = frames[-1]
        self.assertTrue(done.get("done"))
        self.assertEqual(done["session_id"], "s1")
        self.assertIn("intent", done)
        self.assertIn("emotion", done)

    def test_fallback_mode_chunks_node_output(self):
        frames = self._collect(UpdatesOnlyGraph())
        tokens = [f["token"] for f in frames if "token" in f]
        self.assertEqual("".join(tokens), "这是一段完整的节点级降级回复内容")
        self.assertTrue(frames[-1].get("done"))

    def test_interrupt_marks_done_escalated(self):
        frames = self._collect(InterruptStreamGraph())
        done = frames[-1]
        self.assertTrue(done.get("done"))
        self.assertTrue(done.get("interrupted"))
        self.assertEqual(done.get("reply_type"), "escalated")


# ════════════════════════════════════════════════════════════════════
# SSE 帧序列化
# ════════════════════════════════════════════════════════════════════

class TestSseFormat(unittest.TestCase):
    def test_frame_wire_format(self):
        frame = sse_format({"token": "你好"})
        self.assertTrue(frame.startswith("data: "))
        self.assertTrue(frame.endswith("\n\n"))
        self.assertEqual(json.loads(frame[len("data: "):-2]), {"token": "你好"})
        self.assertIn("你好", frame)  # ensure_ascii=False：中文不转义

    def test_done_frame_roundtrip(self):
        payload = {"done": True, "session_id": "s", "intent": "consult",
                   "emotion": "neutral"}
        parsed = json.loads(sse_format(payload)[6:-2])
        self.assertEqual(parsed, payload)


# ════════════════════════════════════════════════════════════════════
# 幂等键透传
# ════════════════════════════════════════════════════════════════════

class TestIdempotencyKey(unittest.TestCase):
    def test_present(self):
        self.assertEqual(
            idempotency_key_from_headers({"X-Idempotency-Key": " abc-1 "}),
            "abc-1")

    def test_case_insensitive(self):
        self.assertEqual(
            idempotency_key_from_headers({"x-idempotency-key": "k1"}), "k1")

    def test_missing_blank_oversize(self):
        self.assertIsNone(idempotency_key_from_headers({}))
        self.assertIsNone(idempotency_key_from_headers({"X-Idempotency-Key": "  "}))
        self.assertIsNone(
            idempotency_key_from_headers({"X-Idempotency-Key": "x" * 200}))

    def test_flows_into_initial_state(self):
        state = runner.build_initial_state("s", "m", idempotency_key="k9")
        self.assertEqual(state["idempotency_key"], "k9")


# ════════════════════════════════════════════════════════════════════
# sessions / analytics SQL（内存 sqlite 造数）
# ════════════════════════════════════════════════════════════════════

def _seed_memory_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, user_message TEXT, bot_reply TEXT,
            intent TEXT, emotion TEXT, emotion_intensity INTEGER DEFAULT 1,
            resolved INTEGER DEFAULT 0, timestamp TEXT);
        CREATE TABLE ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            message_index INTEGER, stars INTEGER, rated_at TEXT);
        CREATE TABLE tickets (
            ticket_id TEXT PRIMARY KEY, session_id TEXT, issue_category TEXT,
            description TEXT, resolution TEXT, satisfaction TEXT, priority TEXT,
            emotion TEXT, emotion_intensity INTEGER, message_count INTEGER,
            created_at TEXT);
    """)
    rows = [
        ("s1", "wifi怎么连", "打开app配网", "consult", "neutral", 1, "2026-07-01T10:00:00"),
        ("s1", "还是不行", "请重启路由器", "consult", "anxious", 2, "2026-07-01T10:05:00"),
        ("s2", "我要退货", "先道歉再退货", "complaint", "angry", 4, "2026-07-02T09:00:00"),
    ]
    conn.executemany(
        "INSERT INTO conversation_history (session_id, user_message, bot_reply,"
        " intent, emotion, emotion_intensity, timestamp)"
        " VALUES (?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO ratings (session_id, message_index, stars, rated_at)"
                 " VALUES ('s1', 1, 5, '2026-07-01')")
    conn.execute("INSERT INTO ratings (session_id, message_index, stars, rated_at)"
                 " VALUES ('s2', 1, 1, '2026-07-02')")
    conn.execute("INSERT INTO tickets (ticket_id, session_id, priority)"
                 " VALUES ('t1', 's2', 'high')")
    conn.commit()


class TestSessionQueries(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        _seed_memory_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_sessions_shape_and_order(self):
        out = query_sessions(self.conn)
        self.assertEqual([s["session_id"] for s in out["sessions"]], ["s2", "s1"])
        s1 = out["sessions"][1]
        self.assertEqual(s1["message_count"], 2)
        self.assertEqual(s1["last_activity"], "2026-07-01T10:05:00")
        self.assertEqual(s1["preview"], "还是不行")
        self.assertIn("consult", s1["intents"])
        for key in ("session_id", "message_count", "last_activity", "intents",
                    "preview"):
            self.assertIn(key, s1)

    def test_sessions_search(self):
        out = query_sessions(self.conn, search="退货")
        self.assertEqual([s["session_id"] for s in out["sessions"]], ["s2"])
        self.assertEqual(query_sessions(self.conn, search="不存在词")["sessions"], [])

    def test_session_detail(self):
        out = query_session_detail(self.conn, "s1")
        self.assertEqual(out["session_id"], "s1")
        self.assertEqual(len(out["messages"]), 4)  # 2 轮 × (user+assistant)
        self.assertEqual(out["messages"][0],
                         {"role": "user", "content": "wifi怎么连",
                          "createdAt": "2026-07-01T10:00:00"})
        self.assertEqual(out["messages"][1]["role"], "assistant")
        self.assertEqual(out["intent"], "consult")
        self.assertEqual(out["emotion"], "anxious")

    def test_session_detail_unknown_session(self):
        out = query_session_detail(self.conn, "nope")
        self.assertEqual(out["messages"], [])
        self.assertEqual(out["message_count"], 0)

    def test_analytics_shape(self):
        out = query_analytics(self.conn)
        self.assertEqual(out["total_conversations"], 3)
        self.assertGreater(out["avg_reply_length"], 0)
        self.assertEqual(out["ratings"], {"total": 2, "average": 3.0})
        self.assertEqual(out["tickets"], {"total": 1, "by_priority": {"high": 1}})
        self.assertEqual(out["intents"], {"consult": 2, "complaint": 1})
        self.assertEqual(out["emotions"],
                         {"neutral": 1, "anxious": 1, "angry": 1})

    def test_missing_tables_graceful(self):
        empty = sqlite3.connect(":memory:")
        try:
            self.assertEqual(query_sessions(empty), {"sessions": []})
            out = query_analytics(empty)
            self.assertEqual(out["total_conversations"], 0)
            self.assertEqual(out["ratings"], {"total": 0, "average": 0})
            detail = query_session_detail(empty, "x")
            self.assertEqual(detail["messages"], [])
        finally:
            empty.close()


# ════════════════════════════════════════════════════════════════════
# admin 授权 + prompt 状态机分发（mock registry）
# ════════════════════════════════════════════════════════════════════

class TestAdminAuth(unittest.TestCase):
    def test_no_jwt_configured_403(self):
        allowed, status, _ = admin_auth_status(False, {"scope": "admin"})
        self.assertFalse(allowed)
        self.assertEqual(status, 403)

    def test_no_claims_401(self):
        allowed, status, _ = admin_auth_status(True, None)
        self.assertFalse(allowed)
        self.assertEqual(status, 401)

    def test_wrong_scope_403(self):
        allowed, status, _ = admin_auth_status(True, {"scope": "user"})
        self.assertFalse(allowed)
        self.assertEqual(status, 403)

    def test_admin_scope_ok(self):
        for scope in ("admin", "user admin", "read,admin"):
            allowed, status, _ = admin_auth_status(True, {"scope": scope})
            self.assertTrue(allowed, scope)
            self.assertEqual(status, 200)


class MockRegistry:
    def __init__(self):
        self.calls = []
        self._versions = {
            1: SimpleNamespace(version_no=1, version_id=11, status="released",
                               change_reason="seed", created_at="t1"),
            2: SimpleNamespace(version_no=2, version_id=22, status="pending_approval",
                               change_reason="improve", created_at="t2"),
        }

    def list_versions(self, name):
        self.calls.append(("list_versions", name))
        return list(self._versions.values())

    def get_active(self, name):
        self.calls.append(("get_active", name))
        return self._versions[1]

    def get_version(self, name, version_no):
        self.calls.append(("get_version", name, version_no))
        try:
            return self._versions[version_no]
        except KeyError:
            raise KeyError(f"no version {version_no}")

    def set_status(self, version_id, status):
        self.calls.append(("set_status", version_id, status))

    def release(self, name, version_no, percent):
        self.calls.append(("release", name, version_no, percent))
        return {"name": name, "version_no": version_no, "percent": percent}

    def promote_full(self, name):
        self.calls.append(("promote_full", name))
        return {"name": name, "percent": 100}

    def rollback(self, name):
        self.calls.append(("rollback", name))
        return {"name": name, "rolled_back_from": 2}


class TestAdminPromptAction(unittest.TestCase):
    def setUp(self):
        self.reg = MockRegistry()

    def test_list(self):
        status, body = admin_prompt_action(self.reg, "list")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["versions"]), 2)
        self.assertEqual(body["active"]["version_no"], 1)
        self.assertEqual(body["versions"][1]["status"], "pending_approval")

    def test_approve_state_machine_sequence(self):
        status, body = admin_prompt_action(
            self.reg, "approve", {"version": 2, "percent": 10})
        self.assertEqual(status, 200)
        self.assertIn(("get_version", "system_prompt", 2), self.reg.calls)
        self.assertIn(("set_status", 22, "approved"), self.reg.calls)
        self.assertIn(("release", "system_prompt", 2, 10), self.reg.calls)
        # set_status 先于 release（先审批后灰度）
        self.assertLess(self.reg.calls.index(("set_status", 22, "approved")),
                        self.reg.calls.index(("release", "system_prompt", 2, 10)))
        self.assertEqual(body["release"]["percent"], 10)

    def test_approve_validation(self):
        status, body = admin_prompt_action(self.reg, "approve", {})
        self.assertEqual(status, 400)
        status, _ = admin_prompt_action(self.reg, "approve",
                                        {"version": 2, "percent": 0})
        self.assertEqual(status, 400)
        status, _ = admin_prompt_action(self.reg, "approve",
                                        {"version": 2, "percent": 101})
        self.assertEqual(status, 400)
        status, _ = admin_prompt_action(self.reg, "approve", {"version": 99})
        self.assertEqual(status, 404)  # KeyError → 404

    def test_promote_and_rollback(self):
        status, body = admin_prompt_action(self.reg, "promote")
        self.assertEqual(status, 200)
        self.assertIn(("promote_full", "system_prompt"), self.reg.calls)
        status, body = admin_prompt_action(self.reg, "rollback",
                                           {"name": "judge_prompt"})
        self.assertEqual(status, 200)
        self.assertIn(("rollback", "judge_prompt"), self.reg.calls)
        self.assertEqual(body["release"]["rolled_back_from"], 2)

    def test_unknown_action(self):
        status, body = admin_prompt_action(self.reg, "delete")
        self.assertEqual(status, 404)
        self.assertIn("unknown admin action", body["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
