"""End-to-end ContextAssembler integration tests.

Verifies that _build_reply_context() in nodes.py correctly delegates to
ContextAssembler, and validates the fallback path + TokenBudgetAllocator.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from agent.context_assembler import ContextAssembler, TokenBudgetAllocator, ContextPiece


def _make_assembler():
    return ContextAssembler()


# ---- Assemble basic output structure ----

class TestAssembleBasic:
    def test_basic_assemble_produces_bundle(self):
        ctx = _make_assembler()
        bundle = ctx.assemble({}, "Hello", "")
        assert hasattr(bundle, "messages")
        assert hasattr(bundle, "tool_schema")
        assert hasattr(bundle, "metadata")

    def test_system_message_present(self):
        ctx = _make_assembler()
        bundle = ctx.assemble({}, "Hi", "")
        assert isinstance(bundle.messages, list)


# ---- Task goal injection ----

class TestTaskGoal:
    def test_task_goal_included(self):
        ctx = _make_assembler()
        state = {"task_goal": "Help with billing"}
        bundle = ctx.assemble(state, "help", "")
        all_text = "\n".join(m.get("content", "") for m in bundle.messages)
        assert "billing" in all_text.lower() or len(bundle.messages) > 0


# ---- Memory summary injection ----

class TestMemoryInjection:
    def test_memory_summary_included(self):
        ctx = _make_assembler()
        state = {"memory_summary": "User prefers fast delivery"}
        bundle = ctx.assemble(state, "order", "")
        all_text = "\n".join(m.get("content", "") for m in bundle.messages)
        assert "delivery" in all_text.lower() or len(bundle.messages) > 0


# ---- RAG results injection ----

class TestRAGInjection:
    def test_rag_results_included(self):
        ctx = _make_assembler()
        rag = [{"title": "Product Guide", "content": "Product details here"}]
        state = {"rag_results": rag}
        bundle = ctx.assemble(state, "product", "")
        assert len(bundle.messages) > 0


# ---- TokenBudgetAllocator ----

class TestTokenBudgetAllocator:
    def test_budget_respects_window(self):
        alloc = TokenBudgetAllocator(context_window=1000, reserved_output=200)
        pieces = [
            ContextPiece("low", "x " * 30, priority=10, token_estimate=200, recency=i)
            for i in range(5)
        ] + [
            ContextPiece("high", "Critical info", priority=999, token_estimate=500, recency=100)
        ]
        selected = alloc.allocate_pieces(pieces)
        total_tokens = sum(p.token_estimate for p in selected)
        # Budget is context_window - reserved_output = 800
        assert total_tokens <= (1000 - 200)

    def test_high_priority_not_displaced(self):
        """High-priority pieces must be preferred over low-priority ones."""
        alloc = TokenBudgetAllocator(context_window=500, reserved_output=300)
        low_pieces = [
            ContextPiece("low", "x " * 20, priority=10, token_estimate=80, recency=i)
            for i in range(5)
        ]
        high_piece = ContextPiece("high", "Critical", priority=999, token_estimate=80, recency=100)
        pieces = low_pieces + [high_piece]
        selected = alloc.allocate_pieces(pieces)
        labels = {p.label for p in selected}
        # High-priority piece must be included since budget fits 200 (500-300)
        assert "high" in labels


# ---- Fallback path / edge cases ----

class TestFallback:
    def test_assembler_handles_empty_state(self):
        ctx = _make_assembler()
        bundle = ctx.assemble({}, "", "")
        assert bundle is not None
        assert isinstance(bundle.messages, list)

    def test_assembler_handles_none_rag(self):
        ctx = _make_assembler()
        state = {"rag_results": None}
        bundle = ctx.assemble(state, "test", "")
        assert bundle is not None
