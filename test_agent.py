# -*- coding: utf-8 -*-
"""
Automation test script - LangGraph Customer Service Agent

Tests all core features:
1. Intent identification (consult/complaint/chat)
2. Reply generation
3. Satisfaction check and retry mechanism
4. Escalation to human (interrupt)
5. Session resume
6. Edge cases

Run: python test_agent.py
"""

import sys
import os
import io
from uuid import uuid4

# Windows UTF-8 compatibility
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add project root to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langchain_core.messages import HumanMessage, AIMessage
from agent.graph import _build_core_graph
from langgraph.checkpoint.memory import MemorySaver


class TestResult:
    """Test result recorder"""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.tests = []

    def record(self, name, passed, message=""):
        self.tests.append({'name': name, 'passed': passed, 'message': message})
        if passed:
            self.passed += 1
            print(f"   [PASS] {name}")
        else:
            self.failed += 1
            print(f"   [FAIL] {name}: {message}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"Summary: {self.passed}/{total} passed, {self.failed} failed")
        print(f"{'='*60}")
        if self.failed > 0:
            print("\nFailed tests:")
            for test in self.tests:
                if not test['passed']:
                    print(f"  - {test['name']}: {test['message']}")


def _make_graph():
    """Create a compiled graph with shared in-memory checkpointer."""
    checkpointer = MemorySaver()
    graph = _build_core_graph()
    return graph.compile(checkpointer=checkpointer), checkpointer


def test_intent_identification():
    """Test intent identification"""
    print("\n[Test Group 1: Intent Identification]")
    print("-" * 40)

    results = TestResult()

    # --- Consult intent ---
    graph, _ = _make_graph()
    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    input_data = {
        "messages": [HumanMessage(content="How to use the product?")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass
        state = graph.get_state(config)
        intent = state.values.get('intent')
        results.record("Consult intent", intent == 'consult',
                        f"Expected 'consult', got '{intent}'")
    except Exception as e:
        results.record("Consult intent", False, str(e))

    # --- Complaint intent ---
    graph, _ = _make_graph()
    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    input_data = {
        "messages": [HumanMessage(content="我要投诉，产品有质量问题")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass
        state = graph.get_state(config)
        intent = state.values.get('intent')
        results.record("Complaint intent", intent == 'complaint',
                        f"Expected 'complaint', got '{intent}'")
    except Exception as e:
        results.record("Complaint intent", False, str(e))

    # --- Chat intent ---
    graph, _ = _make_graph()
    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    input_data = {
        "messages": [HumanMessage(content="hello")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass
        state = graph.get_state(config)
        intent = state.values.get('intent')
        results.record("Chat intent", intent == 'chat',
                        f"Expected 'chat', got '{intent}'")
    except Exception as e:
        results.record("Chat intent", False, str(e))

    return results


def test_reply_generation():
    """Test reply generation"""
    print("\n[Test Group 2: Reply Generation]")
    print("-" * 40)

    results = TestResult()
    graph, _ = _make_graph()

    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    input_data = {
        "messages": [HumanMessage(content="Product features")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass
        state = graph.get_state(config)
        messages = state.values.get('messages', [])

        has_ai_reply = any(isinstance(msg, AIMessage) for msg in messages)
        results.record("AI reply generated", has_ai_reply,
                        "No AI message detected" if not has_ai_reply else "")

        ai_messages = [msg for msg in messages if isinstance(msg, AIMessage)]
        if ai_messages:
            results.record("Reply not empty", len(ai_messages[-1].content) > 0,
                            "Reply content is empty")
    except Exception as e:
        results.record("Reply generation", False, str(e))

    return results


def test_satisfaction_and_retry():
    """Test satisfaction check and retry mechanism.

    The graph runs in a single stream call to completion (auto-retries).
    We verify the final state reflects the correct flow.
    """
    print("\n[Test Group 3: Satisfaction & Retry]")
    print("-" * 40)

    results = TestResult()
    graph, _ = _make_graph()

    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    # Feed a single user message; the graph will auto-run through
    # intent -> reply -> satisfaction check -> process (not satisfied)
    # -> retry -> ... -> escalate after 3 retries
    input_data = {
        "messages": [HumanMessage(content="How to use?")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass
        state = graph.get_state(config)
        retry_count = state.values.get('retry_count', 0)

        # After 3 auto-retries, count should be 3
        results.record(
            "Retry count reached 3 after auto-retries",
            retry_count == 3,
            f"Expected 3, got {retry_count}"
        )
    except Exception as e:
        results.record("Auto-retry flow", False, str(e))

    # --- Test explicit satisfaction === True path ---
    graph2, _ = _make_graph()
    session_id2 = str(uuid4())
    config2 = {"configurable": {"thread_id": session_id2}}

    # First turn: user asks something
    input_data = {
        "messages": [HumanMessage(content="Product info")],
        "session_id": session_id2,
        "retry_count": 0,
        "escalate": False
    }

    try:
        for _ in graph2.stream(input_data, config=config2, stream_mode="values"):
            pass

        # Second turn: user says satisfied
        input_data = {
            "messages": [HumanMessage(content="OK satisfied")]
        }
        for _ in graph2.stream(input_data, config=config2, stream_mode="values"):
            pass

        state = graph2.get_state(config2)
        satisfaction = state.values.get('satisfaction')

        results.record(
            "Satisfaction set to True",
            satisfaction is True,
            f"Expected True, got {satisfaction}"
        )
    except Exception as e:
        results.record("Satisfaction True path", False, str(e))

    return results


def test_escalation():
    """Test escalation to human via interrupt"""
    print("\n[Test Group 4: Escalation (interrupt)]")
    print("-" * 40)

    results = TestResult()
    graph, _ = _make_graph()

    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    # Pre-set retry_count=2 so next unsatisfied triggers escalation
    input_data = {
        "messages": [HumanMessage(content="Product has issues")],
        "session_id": session_id,
        "retry_count": 2,
        "escalate": False
    }

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass
        state = graph.get_state(config)
        escalate = state.values.get('escalate', False)

        results.record(
            "Escalation triggered",
            escalate is True or state.next == ('escalate_to_human',),
            f"escalate={escalate}, next={state.next}"
        )
    except Exception as e:
        if "interrupt" in str(e).lower() or "Interrupt" in str(e):
            results.record("Escalation triggered", True, "interrupt fired correctly")
        else:
            results.record("Escalation triggered", False, str(e))

    return results


def test_session_resume():
    """Test session resume with shared checkpointer."""
    print("\n[Test Group 5: Session Resume]")
    print("-" * 40)

    results = TestResult()

    # Use a single shared checkpointer so two graph instances can share state
    from langgraph.checkpoint.memory import MemorySaver
    shared_checkpointer = MemorySaver()

    graph1 = _build_core_graph().compile(checkpointer=shared_checkpointer)
    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    input_data = {
        "messages": [HumanMessage(content="Hello, I want to know about products")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    try:
        for _ in graph1.stream(input_data, config=config, stream_mode="values"):
            pass
        state1 = graph1.get_state(config)
        messages_count_1 = len(state1.values.get('messages', []))

        results.record(
            "Initial session created",
            messages_count_1 > 0,
            f"Message count: {messages_count_1}"
        )
    except Exception as e:
        results.record("Initial session creation", False, str(e))

    # Simulate restart: create a new graph instance but share the same checkpointer
    graph2 = _build_core_graph().compile(checkpointer=shared_checkpointer)

    try:
        state2 = graph2.get_state(config)

        if state2 and state2.values:
            messages_count_2 = len(state2.values.get('messages', []))
            results.record(
                "Session state restored",
                messages_count_2 == messages_count_1,
                f"Expected {messages_count_1} messages, got {messages_count_2}"
            )
        else:
            results.record("Session state restored", False,
                            "Could not restore session state")
    except Exception as e:
        results.record("Session state restore", False, str(e))

    return results


def test_edge_cases():
    """Test edge cases"""
    print("\n[Test Group 6: Edge Cases]")
    print("-" * 40)

    results = TestResult()
    graph, _ = _make_graph()

    # --- Empty message ---
    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    input_data = {
        "messages": [HumanMessage(content="")],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    try:
        for _ in graph.stream(input_data, config=config, stream_mode="values"):
            pass
        state = graph.get_state(config)
        results.record("Empty message handling", state is not None,
                        "Empty message caused error" if state is None else "")
    except Exception as e:
        results.record("Empty message handling", False, str(e))

    # --- Long message ---
    graph2, _ = _make_graph()
    session_id = str(uuid4())
    config = {"configurable": {"thread_id": session_id}}

    input_data = {
        "messages": [HumanMessage(content="product " * 100)],
        "session_id": session_id,
        "retry_count": 0,
        "escalate": False
    }

    try:
        for _ in graph2.stream(input_data, config=config, stream_mode="values"):
            pass
        state = graph2.get_state(config)
        results.record("Long message handling", state is not None,
                        "Long message caused error" if state is None else "")
    except Exception as e:
        results.record("Long message handling", False, str(e))

    return results


def main():
    """Run all tests"""
    print("=" * 60)
    print("[LangGraph Customer Service Agent - Full Test]")
    print("=" * 60)

    all_results = TestResult()

    for test_fn in [
        test_intent_identification,
        test_reply_generation,
        test_satisfaction_and_retry,
        test_escalation,
        test_session_resume,
        test_edge_cases,
    ]:
        results = test_fn()
        for test in results.tests:
            all_results.record(test['name'], test['passed'], test['message'])

    all_results.summary()

    return 0 if all_results.failed == 0 else 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
