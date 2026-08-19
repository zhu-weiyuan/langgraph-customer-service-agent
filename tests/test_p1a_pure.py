"""P1-A pure-stdlib unit tests (no langgraph / langchain / tiktoken required).

Run:
    python3 -m unittest tests.test_p1a_pure -v
"""
import os
import sys
import time
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent import token_estimator
from agent.token_estimator import (
    estimate_tokens,
    estimate_messages_tokens,
    TokenEstimator,
    _heuristic_estimate,
)
from agent.context_assembler import (
    ContextAssembler,
    ContextPiece,
    TokenBudgetAllocator,
    TIER_FULL,
)
from agent.prompt_registry import PromptRegistry


def _make_registry():
    """Return an in-memory PromptRegistry immune to DATABASE_URL env pollution."""
    return PromptRegistry(db_path=":memory:")
from agent import graph as agent_graph
from agent.graph import (
    route_after_reply,
    should_resolve,
    ROUTE_AFTER_REPLY_MAP,
    ROUTE_AFTER_SATISFACTION_MAP,
    END,
)


class TestTokenEstimator(unittest.TestCase):
    """Heuristic: ceil(cjk*0.7 + ascii_words*1.3 + other*0.3)."""

    def test_empty_and_none(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens(None), 0)
        self.assertEqual(_heuristic_estimate("   "), 0)

    def test_pure_chinese(self):
        # 3 CJK chars * 0.7 = 2.1 -> ceil = 3
        self.assertEqual(_heuristic_estimate("你好吗"), 3)

    def test_pure_english(self):
        # 2 words * 1.3 = 2.6 + 1 space * 0.3 = 2.9 -> ceil = 3
        self.assertEqual(_heuristic_estimate("hello world"), 3)

    def test_mixed_cjk_english(self):
        # "你好world": 2 cjk * 0.7 = 1.4 + 1 word * 1.3 = 2.7 -> ceil = 3
        self.assertEqual(_heuristic_estimate("你好world"), 3)

    def test_estimate_tokens_falls_back_to_heuristic(self):
        # Force the optional encoder into its degraded state so this test is
        # deterministic even when tiktoken is installed locally.
        text = "订单 order-123 需要修复"
        with mock.patch.object(token_estimator, "_ENCODER", False), \
             mock.patch.object(token_estimator, "_ENCODER_FAILED_AT", time.monotonic()):
            self.assertEqual(estimate_tokens(text), _heuristic_estimate(text))
            self.assertGreater(estimate_tokens(text), 0)

    def test_estimate_messages_tokens(self):
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "hello"},
        ]
        expected = (estimate_tokens("你好") + 4) + (estimate_tokens("hello") + 4)
        self.assertEqual(estimate_messages_tokens(msgs), expected)
        self.assertEqual(estimate_messages_tokens([]), 0)
        self.assertEqual(estimate_messages_tokens(None), 0)

    def test_legacy_class_compat(self):
        est = TokenEstimator()
        self.assertEqual(est.estimate_text("你好world"), estimate_tokens("你好world"))
        self.assertEqual(
            est.estimate_messages([{"content": "hi"}]),
            estimate_messages_tokens([{"content": "hi"}]),
        )

    def test_monotonic_growth(self):
        self.assertGreater(estimate_tokens("这是一个比较长的中文句子，用来测试估算"),
                           estimate_tokens("短句"))


class TestAssemblerMessageOrder(unittest.TestCase):
    def _assemble(self, state, user_message):
        return ContextAssembler(registry=_make_registry()).assemble(
            state=state, user_message=user_message, session_id="s1")

    def test_current_question_is_last_history_old_to_new(self):
        state = {
            "messages": [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ],
        }
        bundle = self._assemble(state, "current question?")
        msgs = bundle.messages

        self.assertEqual(msgs[0]["role"], "system")
        # current question is ALWAYS the last message
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertEqual(msgs[-1]["content"], "current question?")
        # history preserved old -> new with explicit roles
        middle = msgs[1:-1]
        self.assertEqual([m["content"] for m in middle], ["q1", "a1", "q2", "a2"])
        self.assertEqual([m["role"] for m in middle],
                         ["user", "assistant", "user", "assistant"])

    def test_current_turn_not_duplicated_when_present_in_history(self):
        state = {
            "messages": [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "current?"},  # already in history
            ],
        }
        bundle = self._assemble(state, "current?")
        contents = [m["content"] for m in bundle.messages]
        self.assertEqual(contents.count("current?"), 1)
        self.assertEqual(bundle.messages[-1]["content"], "current?")

    def test_langchain_like_objects_roles_via_duck_typing(self):
        class FakeHuman:
            type = "human"
            def __init__(self, content): self.content = content

        class FakeAI:
            type = "ai"
            def __init__(self, content): self.content = content

        state = {"messages": [FakeHuman("hi"), FakeAI("hello!")]}
        bundle = self._assemble(state, "next question")
        middle = bundle.messages[1:-1]
        self.assertEqual([m["role"] for m in middle], ["user", "assistant"])


class TestBudgetTermination(unittest.TestCase):
    def test_loop_does_not_stop_early(self):
        """Regression: old loop broke on `used >= avail` (~50% of budget)."""
        alloc = TokenBudgetAllocator(context_window=1000, reserved_output=0)
        self.assertEqual(alloc.usable_budget, 600)

        # 20 pieces * ~20 tokens = ~400 total -> ALL must fit within 600.
        pieces = [
            ContextPiece("history", "word " * 15, 40, role="user",
                         source_id=f"history:{i}", recency=i)
            for i in range(20)
        ]
        per_piece = pieces[0].estimated()
        self.assertLessEqual(per_piece * 20, 600)

        selected = alloc.fit(alloc.rank(pieces))
        # Buggy `used >= avail` termination selects only ~half of these.
        self.assertEqual(len(selected), 20)
        used = sum(p.token_estimate for p in selected)
        self.assertLessEqual(used, alloc.usable_budget)

    def test_stops_when_budget_truly_exhausted(self):
        alloc = TokenBudgetAllocator(context_window=100, reserved_output=0)
        pieces = [
            ContextPiece("history", "字" * 200, 40, role="user",
                         source_id=f"history:{i}", recency=i)
            for i in range(10)
        ]
        selected = alloc.fit(alloc.rank(pieces))
        used = sum(p.token_estimate for p in selected)
        self.assertLessEqual(used, alloc.usable_budget)


class TestUtilizationDowngrade(unittest.TestCase):
    def test_over_60_percent_triggers_degradation_and_stays_capped(self):
        alloc = TokenBudgetAllocator(context_window=1000, reserved_output=0)
        assembler = ContextAssembler(allocator=alloc, registry=_make_registry())

        huge_context = "### 保修政策 " + ("智能音箱保修条款详细内容" * 120)
        self.assertGreater(estimate_tokens(huge_context), alloc.usable_budget)

        bundle = assembler.assemble(
            state={"messages": [],
                   "rag_results": [{"sufficient": True, "context": huge_context,
                                    "rounds": 1, "queries_tried": ["保修"]}]},
            user_message="保修多久？",
        )

        # utilisation never exceeds the 60% ceiling
        self.assertLessEqual(bundle.metadata["utilization"],
                             alloc.TARGET_HIGH + 1e-9)
        # the oversized RAG piece was degraded (tier != full)
        degraded = bundle.metadata["degraded"]
        self.assertTrue(degraded)
        self.assertIn("agentic_rag:round1",
                      [d["source_id"] for d in degraded])
        # evidence chain: the source id survives inside the truncated text
        system_content = bundle.messages[0]["content"]
        self.assertIn("agentic_rag:round1", system_content)

    def test_normal_load_stays_full_tier(self):
        assembler = ContextAssembler(registry=_make_registry())  # 128k window: tiny inputs stay full
        bundle = assembler.assemble(
            state={"messages": [{"role": "user", "content": "hi"}]},
            user_message="订单在哪？",
        )
        self.assertEqual(bundle.metadata["degraded"], [])
        self.assertLess(bundle.metadata["utilization"], 0.60)

    def test_reference_tier_keeps_source_id(self):
        alloc = TokenBudgetAllocator(context_window=100, reserved_output=0)
        piece = ContextPiece("rag", "内容" * 500, 70, source_id="doc:faq-42")
        stub = alloc._to_reference(piece)
        self.assertIn("doc:faq-42", stub.content)
        self.assertNotEqual(stub.tier, TIER_FULL)


class TestRagFieldAlignment(unittest.TestCase):
    def test_agentic_rag_shape_reaches_prompt(self):
        """nodes.py passes {sufficient, context, rounds, queries_tried}."""
        rag_info = {
            "sufficient": True,
            "context": "### 智能音箱保修政策：整机保修一年",
            "rounds": 2,
            "queries_tried": ["保修", "音箱保修"],
        }
        bundle = ContextAssembler(registry=_make_registry()).assemble(
            state={"messages": [], "rag_results": [rag_info]},
            user_message="音箱保修多久？",
        )
        system_content = bundle.messages[0]["content"]
        self.assertIn("智能音箱保修政策：整机保修一年", system_content)
        self.assertGreaterEqual(bundle.metadata["source_counts"].get("rag", 0), 1)

    def test_legacy_shape_still_supported(self):
        bundle = ContextAssembler(registry=_make_registry()).assemble(
            state={"messages": [],
                   "rag_results": [{"title": "退货政策", "content": "七天无理由退货",
                                    "score": 0.9, "relevant": True}]},
            user_message="怎么退货？",
        )
        self.assertIn("七天无理由退货", bundle.messages[0]["content"])

    def test_insufficient_empty_context_excluded(self):
        bundle = ContextAssembler(registry=_make_registry()).assemble(
            state={"messages": [],
                   "rag_results": [{"sufficient": False, "context": "",
                                    "rounds": 2, "queries_tried": ["x"]}]},
            user_message="hello",
        )
        self.assertEqual(bundle.metadata["source_counts"].get("rag", 0), 0)


class TestGraphRouting(unittest.TestCase):
    """Pure-function tests: langgraph absent, guarded imports must hold."""

    def test_langgraph_guard_active(self):
        # Both the bare stdlib fallback and the real deployment with LangGraph
        # installed are supported by the guarded import.
        self.assertTrue(
            agent_graph.StateGraph is None or callable(agent_graph.StateGraph)
        )
        self.assertEqual(END, "__end__")

    def test_route_after_reply_all_returns_mapped(self):
        cases = [
            ({"ending": True}, "check_satisfaction"),
            ({"ending": False, "retry_count": 1}, "finalize"),
            ({"ending": False, "retry_count": 0}, END),
            ({}, END),
        ]
        for state, expected in cases:
            result = route_after_reply(state)
            self.assertEqual(result, expected)
            self.assertIn(result, ROUTE_AFTER_REPLY_MAP)  # finalize crash fix

    def test_finalize_in_reply_map(self):
        self.assertIn("finalize", ROUTE_AFTER_REPLY_MAP)

    def test_should_resolve_all_returns_mapped(self):
        cases = [
            ({"satisfaction": True, "retry_count": 2}, "finalize"),
            ({"satisfaction": False, "retry_count": 1}, "generate_reply"),
            ({"satisfaction": False, "retry_count": 2}, "escalate_to_human"),
            ({"satisfaction": False, "retry_count": 1,
              "emotion": "angry", "emotion_intensity": 5}, "escalate_to_human"),
            ({"satisfaction": None, "retry_count": 0}, "finalize"),
        ]
        for state, expected in cases:
            result = should_resolve(state)
            self.assertEqual(result, expected, msg=f"state={state}")
            self.assertIn(result, ROUTE_AFTER_SATISFACTION_MAP)

    def test_escalation_reachable(self):
        # 连续不满意 >= 2
        self.assertEqual(
            should_resolve({"satisfaction": False, "retry_count": 2}),
            "escalate_to_human")
        # 强负面情绪
        self.assertEqual(
            should_resolve({"satisfaction": False, "retry_count": 0,
                            "emotion": "anxious", "emotion_intensity": 4}),
            "escalate_to_human")
        self.assertIn("escalate_to_human", ROUTE_AFTER_SATISFACTION_MAP)

    def test_satisfied_never_escalates(self):
        self.assertEqual(
            should_resolve({"satisfaction": True, "retry_count": 5,
                            "emotion": "angry", "emotion_intensity": 5}),
            "finalize")


class TestNodesPure(unittest.TestCase):
    """agent.nodes must import and build contexts in a bare stdlib container."""

    def test_nodes_importable_without_third_party(self):
        from agent import nodes
        self.assertTrue(callable(nodes.build_reply_context))

    def test_build_reply_context_tone_and_order(self):
        from agent import nodes
        ctx = nodes.build_reply_context(
            messages=[{"role": "user", "content": "音箱坏了"},
                      {"role": "assistant", "content": "抱歉，请问具体情况？"}],
            intent="complaint",
            user_query="我要退货，太生气了",
            session_id="",
            emotion="angry",
            emotion_intensity=5,
            registry=_make_registry(),
        )
        # emotion is wired: tone adjustment appended to system prompt
        self.assertIn("愤怒", ctx["system_prompt"])
        # current question is the last context message
        self.assertEqual(ctx["context_messages"][-1]["content"], "我要退货，太生气了")
        # tool schema passed through (empty for now)
        self.assertEqual(ctx["tool_schema"], [])
        # single budget authority: assembler metadata surfaces as token_budget
        self.assertIn("utilization", ctx["token_budget"])
        # These helpers are intentional safety mechanisms: history-recall
        # detection avoids unnecessary RAG, while the two trimming layers
        # protect both checkpoint state and the model input budget.
        self.assertTrue(callable(nodes._is_history_recall_query))
        self.assertTrue(nodes._is_history_recall_query("\u6211\u521a\u624d\u95ee\u4e86\u4ec0\u4e48\uff1f"))
        self.assertTrue(callable(nodes._trim_messages))
        self.assertTrue(callable(nodes._trim_messages_by_tokens))
        self.assertEqual(
            len(nodes._trim_messages([{"role": "user", "content": str(i)} for i in range(30)], keep_last=3)),
            6,
        )

    def test_node_timings_bounded(self):
        from agent import nodes
        nodes.reset_node_timings()
        for _ in range(1200):
            with nodes._time_node("unit_test_node"):
                pass
        self.assertEqual(len(nodes._node_timings["unit_test_node"]), 1000)
        nodes.reset_node_timings()


class TestStateModule(unittest.TestCase):
    def test_state_importable_and_fields_union(self):
        from agent.state import CustomerServiceState
        keys = set(CustomerServiceState.__annotations__)
        for field in ("messages", "intent", "ending", "bot_reply", "satisfaction",
                      "retry_count", "escalate", "session_id", "emotion",
                      "emotion_intensity", "rag_results", "rag_round",
                      "request_id"):
            self.assertIn(field, keys)

    def test_add_messages_is_flat_and_runtime_compatible(self):
        from agent.state import add_messages
        if add_messages.__module__.startswith("langgraph"):
            merged = add_messages(
                [{"role": "user", "content": "a"}],
                [{"role": "assistant", "content": "b"}],
            )
            self.assertEqual(len(merged), 2)
        else:
            merged = add_messages([1, 2], [3])
            self.assertEqual(merged, [1, 2, 3])
            merged_single = add_messages([1], 2)
            self.assertEqual(merged_single, [1, 2])



if __name__ == "__main__":
    unittest.main(verbosity=2)
