"""
Graph builder - LangGraph Customer Service Agent (Real LLM, new flow)

New conversation flow:
1. User asks question -> bot replies (NO satisfaction check yet)
2. User continues asking -> bot keeps answering  
3. User signals ending ("bye", "thanks") -> ask satisfaction -> resolve/escalate

This is a single unified graph with conditional routing.
"""

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
import os

from typing import TypedDict, Annotated
from langchain_core.messages import AnyMessage

class CustomerServiceState(TypedDict):
    messages: Annotated[list[AnyMessage], lambda x, y: x + [y]]
    intent: str
    ending: bool
    emotion: str
    emotion_intensity: int
    satisfaction: bool | None
    retry_count: int
    escalate: bool
from .nodes import (
    identify_intent,
    generate_reply,
    check_satisfaction,
    process_satisfaction,
    escalate_to_human,
    finalize
)


def route_after_reply(state: dict) -> str:
    """After generating reply, decide next step.

    - If user signaled ending → check satisfaction
    - If this is a retry (retry_count > 0) → finalize directly
    - Otherwise → END (wait for next user message)
    """
    ending = state.get('ending', False)
    retry_count = state.get('retry_count', 0)
    if ending:
        return 'check_satisfaction'
    elif retry_count > 0:
        return 'finalize'
    else:
        return END  # just end, wait for next user message


def should_resolve(state: dict) -> str:
    """After processing satisfaction, decide next step."""
    satisfaction = state.get('satisfaction')
    retry_count = state.get('retry_count', 0)

    if satisfaction is True:
        return 'finalize'
    elif satisfaction is False and retry_count < 3:
        return 'generate_reply'
    else:
        return 'finalize'  # max retries, just end with apology


def _build_core_graph():
    """Build the StateGraph topology."""
    graph = StateGraph(CustomerServiceState)

    # Nodes
    graph.add_node('identify_intent', identify_intent)
    graph.add_node('generate_reply', generate_reply)
    graph.add_node('check_satisfaction', check_satisfaction)
    graph.add_node('process_satisfaction', process_satisfaction)
    graph.add_node('escalate_to_human', escalate_to_human)
    graph.add_node('finalize', finalize)

    # Entry: always identify intent first
    graph.add_edge(START, 'identify_intent')

    # After intent, generate reply
    graph.add_edge('identify_intent', 'generate_reply')

    # After reply: if user signaled ending -> ask satisfaction; else -> END (wait for next message)
    graph.add_conditional_edges(
        'generate_reply',
        route_after_reply,
        {'check_satisfaction': 'check_satisfaction', '__end__': END}
    )

    # Satisfaction check -> process feedback
    graph.add_edge('check_satisfaction', 'process_satisfaction')

    # Process satisfaction -> resolve/retry/escalate
    graph.add_conditional_edges(
        'process_satisfaction',
        should_resolve,
        {
            'generate_reply': 'generate_reply',
            'finalize': 'finalize',
        }
    )

    # After retry reply, route through conditional edge (no separate unconditional edge)
    # The conditional edge above handles: ending → satisfaction, retry → finalize, normal → END
    
    # Finalize -> END
    graph.add_edge('finalize', END)

    return graph


def build_graph(use_sqlite: bool = False, db_path: str = "checkpoints.db"):
    """Build and compile the customer service agent graph.

    Args:
        use_sqlite: Use SQLite persistence instead of in-memory (for production)
        db_path: SQLite database file path
    """
    graph = _build_core_graph()

    if use_sqlite:
        import sqlite3
        conn = sqlite3.connect(db_path)
        checkpointer = SqliteSaver(conn)
        checkpointer.setup()
        print(f"[Graph] Using SQLite checkpointer: {db_path}")
    else:
        checkpointer = MemorySaver()
        print("[Graph] Using in-memory checkpointer (test mode)")

    compiled = graph.compile(checkpointer=checkpointer)

    print(f"[Graph] Customer service agent compiled (Real LLM)")
    print(f"[Graph] Nodes: {list(graph.nodes.keys())}")
    return compiled


def create_agent():
    """Convenience function."""
    return build_graph()
