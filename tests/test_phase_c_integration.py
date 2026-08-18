from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.context_assembler import ContextAssembler, ContextPiece, TokenBudgetAllocator
from agent.prompt_registry import PromptRegistry
from tests.eval_harness import CustomerServiceEvaluator, EvaluationRunner


@pytest.fixture
def _mem_registry():
    """In-memory PromptRegistry immune to DATABASE_URL pollution."""
    return PromptRegistry(db_path=":memory:")


def test_context_assembler_produces_llm_message_structure(_mem_registry):
    bundle = ContextAssembler(registry=_mem_registry).assemble(
        {"messages": [HumanMessage(content="earlier question"), AIMessage(content="earlier reply")],
         "available_tools": [{"name": "lookup"}]},
        "current question", "session-1")
    assert bundle.messages[0]["role"] == "system"
    assert bundle.messages[-1] == {"role": "user", "content": "current question"}
    assert bundle.tool_schema == [{"name": "lookup"}]
    assert bundle.metadata["session_id"] == "session-1"


def test_token_budget_allocator_excludes_low_priority_over_budget_content():
    allocator = TokenBudgetAllocator(context_window=20, reserved_output=10)
    selected = allocator.allocate_pieces([
        ContextPiece("important", "one two", 100, token_estimate=8),
        ContextPiece("old", "three four", 1, token_estimate=8),
    ])
    assert [piece.label for piece in selected] == ["important"]


def test_progressive_disclosure_uses_metadata_for_irrelevant_document(_mem_registry):
    bundle = ContextAssembler(registry=_mem_registry).assemble(
        {"rag_results": [{"title": "Warranty", "summary": "Warranty metadata", "content": "FULL SECRET CONTENT", "relevant": False}]},
        "question")
    system = bundle.messages[0]["content"]
    assert "Warranty metadata" in system
    assert "FULL SECRET CONTENT" not in system


def test_rule_based_eval_harness_against_sample_data(tmp_path):
    cases = EvaluationRunner().load_dataset(Path(__file__).parent / "data" / "sample_eval.json")
    for case in cases:
        expected = case["expected_intent"]
        reply = "已为您升级至人工专员处理。" if case["context"].get("severity") == "high" or case["context"].get("requires_escalation") else "已收到您的问题。"
        case["actual"] = {"intent": expected, "reply": reply}
    runner = EvaluationRunner(export_path=tmp_path / "summary.json")
    summary = runner.run(cases)
    assert summary["total"] == len(cases)
    assert summary["pass_rate"] == 1
    assert (tmp_path / "summary.json").exists()


def test_prompt_registry_render_and_validate(_mem_registry):
    registry = _mem_registry
    assert registry.render_and_validate("Hello {name}", {"name": "customer"}) == "Hello customer"
    with pytest.raises(ValueError):
        registry.render_and_validate("Hello {name}", {})
