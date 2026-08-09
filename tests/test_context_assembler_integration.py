import pytest
from agent.context_assembler import ContextAssembler, ContextBundle, TokenBudgetAllocator

def test_assemble_with_full_components():
    # Setup mock state with all components
    registry = MockPromptRegistry()
    allocator = TokenBudgetAllocator(context_window=8000, reserved_output=512)
    assembler = ContextAssembler(registry=registry, allocator=allocator)
    
    state = {
        "task_goal": "Answer user questions about product features",
        "constraints": ["Be concise", "Avoid technical jargon"],
        "memory_summary": "User previously asked about warranty policy",
        "rag_results": [
            {"title": "Product Manual", "content": "Detailed specs for Model X", "relevant": True},
            {"title": "FAQ", "summary": "Common setup issues", "relevant": False}
        ],
        "messages": [
            {"role": "user", "content": "How do I connect?"},
            {"role": "assistant", "content": "Use the app."}
        ]
    }
    
    bundle: ContextBundle = assembler.assemble(state, "How does voice control work?", "sess-123")
    
    # Assertions
    assert len(bundle.messages) > 2, "Should include system, history, and new user message"
    assert any("voice control" in m["content"] for m in bundle.messages), "New query should be included"
    assert any("Model X" in m["content"] for m in bundle.messages), "RAG content should be included"
    assert any("warranty" in m["content"] for m in bundle.messages), "Memory should be included"
    assert bundle.metadata["session_id"] == "sess-123"
    assert bundle.metadata["token_estimate"] > 0
    assert bundle.metadata["source_counts"]["rag"] == 1, "Only relevant RAG item should count"
    assert "Be concise" in bundle.messages[0]["content"], "Constraints should be in system prompt"


def test_budget_truncation_behavior():
    # Test that allocator respects token budget
    allocator = TokenBudgetAllocator(context_window=1000, reserved_output=256)
    assembler = ContextAssembler(registry=MockPromptRegistry(), allocator=allocator)
    
    # Create oversized state
    long_text = "word " * 400  # ~600 tokens
    state = {
        "task_goal": long_text,
        "memory_summary": long_text,
        "rag_results": [{"title": "doc", "content": long_text}],
        "messages": [
            {"role": "user", "content": long_text},
            {"role": "assistant", "content": long_text}
        ] * 5  # 10 messages
    }
    
    bundle = assembler.assemble(state, "query", "sess-test")
    
    # Should stay under budget
    assert bundle.metadata["token_estimate"] <= (1000 - 256), f"Budget exceeded: {bundle.metadata['token_estimate']}"
    
    # Higher priority items should survive
    system_content = bundle.messages[0]["content"]
    assert "task_goal" in system_content
    assert "doc:" in system_content or "doc" in system_content  # RAG title may appear
    
    # Very old messages might be dropped
    assert len([m for m in bundle.messages if m["role"] == "user"]) <= 10


def test_priority_ordering_correctness():
    # Test that pieces are selected by priority, not just size
    allocator = TokenBudgetAllocator(context_window=500, reserved_output=128)
    assembler = ContextAssembler(registry=MockPromptRegistry(), allocator=allocator)
    
    state = {
        "task_goal": "URGENT: Handle refund request immediately",
        "constraints": [],
        "memory_summary": "User is angry about late delivery",
        "rag_results": [
            {"title": "Policy", "content": "Refund process takes 5-7 days", "relevant": True}
        ],
        "messages": [
            {"role": "user", "content": "This is unacceptable!"},
            {"role": "assistant", "content": "Sorry for the delay."}
        ]
    }
    
    # Overload with low-priority filler
    filler_content = "filler " * 100
    for i in range(20):
        state["messages"].append({"role": "user", "content": filler_content})
        state["messages"].append({"role": "assistant", "content": filler_content})
    
    bundle = assembler.assemble(state, "I want my money back now!", "sess-prio")
    
    system_content = bundle.messages[0]["content"]
    assert "URGENT" in system_content, "High-priority goal should survive"
    assert "Refund process" in system_content, "Relevant RAG should survive"
    assert "unacceptable" in system_content or "money back" in system_content, "Recent emotional content should survive"
    assert "filler" not in system_content, "Filler content should be truncated"


def test_rag_score_weighting():
    # Test that rag_results with relevance scores are weighted properly
    allocator = TokenBudgetAllocator(context_window=1000, reserved_output=256)
    assembler = ContextAssembler(registry=MockPromptRegistry(), allocator=allocator)
    
    state = {
        "task_goal": "Answer question accurately",
        "rag_results": [
            {"title": "Manual", "content": "Step-by-step guide", "relevant": True, "score": 0.95},
            {"title": "Forum", "content": "Unverified user tip", "relevant": False, "score": 0.4},
            {"title": "KB", "content": "Official solution", "relevant": True, "score": 0.98}
        ],
        "messages": [
            {"role": "user", "content": "How to reset?"},
            {"role": "assistant", "content": "Try power cycling."}
        ]
    }
    
    bundle = assembler.assemble(state, "Reset not working", "sess-rag")
    system_content = bundle.messages[0]["content"]
    
    # High-scoring relevant RAG should dominate
    assert "Official solution" in system_content
    assert "Step-by-step guide" in system_content
    assert "Unverified user tip" not in system_content, "Low-relevance, low-score RAG should be excluded"
    
    # Metadata should reflect source counts correctly
    assert bundle.metadata["source_counts"]["rag"] == 2, "Two relevant RAG items should count"


class MockPromptRegistry:
    def __init__(self):
        self._versions = {"system": MockPromptVersion()}
    
    def register(self, name, content):
        pass
    
    def get(self, name):
        return self._versions.get(name)

    def get_active(self, name, tenant=None, session_seed=None, *, env="prod", log_run=False):
        return self._versions.get(name)

    def record_run(self, pv, session_id=""):
        pass

class MockPromptVersion:
    name = "system"
    version_no = 1
    content = "You are a helpful assistant."
    