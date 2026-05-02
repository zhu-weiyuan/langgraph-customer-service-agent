"""
Graph builder - LangGraph Customer Service Agent

Core concepts:
- StateGraph: defines a directed graph with nodes connected by edges
- add_node(): add processing nodes
- add_edge(): fixed edges (unconditional transitions)
- add_conditional_edges(): conditional edges (route based on function return value)
- compile(): compile the graph into an executable object

Checkpointer:
- SqliteSaver: persist graph state to SQLite database
- InMemorySaver: in-memory checkpointing for testing
- Supports session resume and breakpoint continuation via thread_id
"""

import tempfile
import os

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.memory import MemorySaver

from .state import CustomerServiceState
from .nodes import (
    identify_intent,
    generate_reply,
    check_satisfaction,
    process_satisfaction,
    escalate_to_human,
    finalize
)


def should_retry(state: dict) -> str:
    """
    Conditional edge function: decide whether to retry

    LangGraph conditional edges use this pattern to route to the next node.
    The function receives current state and returns a string matching a target node name.

    Args:
        state: current graph state

    Returns:
        Name of the next node
    """
    satisfaction = state.get('satisfaction')
    retry_count = state.get('retry_count', 0)

    if satisfaction is True:
        return 'finalize'
    elif satisfaction is False and retry_count < 3:
        return 'generate_reply'
    else:
        return 'escalate_to_human'


def route_after_intent(state: dict) -> str:
    """
    Router after intent identification

    Different intents can route to different handling logic.
    Simplified here to always go to generate_reply.

    Args:
        state: current graph state

    Returns:
        Name of the next node
    """
    intent = state.get('intent', 'consult')
    print(f"[Router] intent '{intent}' -> generate_reply")
    return 'generate_reply'


def _build_core_graph():
    """Build the StateGraph topology (nodes + edges). Returns the uncompiled graph."""
    graph = StateGraph(CustomerServiceState)

    # --- Nodes ---
    graph.add_node('identify_intent', identify_intent)
    graph.add_node('generate_reply', generate_reply)
    graph.add_node('check_satisfaction', check_satisfaction)
    graph.add_node('process_satisfaction', process_satisfaction)
    graph.add_node('escalate_to_human', escalate_to_human)
    graph.add_node('finalize', finalize)

    # --- Edges ---
    # Entry point: START -> identify_intent
    graph.add_edge(START, 'identify_intent')

    # After intent identification, route to reply generation
    graph.add_conditional_edges(
        'identify_intent',
        route_after_intent,
        {'generate_reply': 'generate_reply'}
    )

    # After generating reply, check satisfaction
    graph.add_edge('generate_reply', 'check_satisfaction')

    # After checking satisfaction, process user feedback
    graph.add_edge('check_satisfaction', 'process_satisfaction')

    # After processing satisfaction, conditionally route
    graph.add_conditional_edges(
        'process_satisfaction',
        should_retry,
        {
            'generate_reply': 'generate_reply',
            'escalate_to_human': 'escalate_to_human',
            'finalize': 'finalize'
        }
    )

    # After human escalation (if resolved), finalize
    graph.add_edge('escalate_to_human', 'finalize')

    # End after finalize
    graph.add_edge('finalize', END)

    return graph


def build_graph(checkpoint_db_path: str = "checkpoints.db"):
    """
    Build and compile the customer service Agent graph.

    Args:
        checkpoint_db_path: SQLite checkpoint DB path.
            Use ":memory:" for in-memory (no persistence across calls).
            Use a file path for persistent storage.

    Returns:
        compiled_graph: the compiled, executable graph
    """
    graph = _build_core_graph()

    if checkpoint_db_path == ":memory:":
        # For testing: use in-memory checkpointer (no DB lifecycle issues)
        checkpointer = MemorySaver()
    else:
        # For production: use SQLite file-based persistence.
        # SqliteSaver.from_conn_string returns a context manager;
        # we enter it to get the actual saver instance.
        # We keep the CM alive by storing it on the compiled graph.
        cm = SqliteSaver.from_conn_string(checkpoint_db_path)
        checkpointer = cm.__enter__()

    compiled_graph = graph.compile(checkpointer=checkpointer)

    print(f"[Graph] Customer service agent compiled")
    print(f"[Graph] Checkpoint: {checkpoint_db_path}")
    print(f"[Graph] Nodes: {list(graph.nodes.keys())}")
    print(f"[Graph] Edges: {[(k, v) for k, v in graph.edges]}")

    return compiled_graph


def create_agent(checkpoint_db_path: str = "checkpoints.db"):
    """Convenience function to create a compiled customer service agent."""
    return build_graph(checkpoint_db_path)
