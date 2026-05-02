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

from .state import CustomerServiceState
from .nodes import (
    identify_intent,
    generate_reply,
    check_satisfaction,
    process_satisfaction,
    escalate_to_human,
    finalize
)


def route_after_reply(state: dict) -> str:
    """After generating reply, check if user signaled ending."""
    ending = state.get('ending', False)
    if ending:
        return 'check_satisfaction'
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

    # After retry reply, end (don't ask satisfaction again)
    graph.add_edge('generate_reply', END)  # this is the retry path
    
    # Finalize -> END
    graph.add_edge('finalize', END)

    return graph


def build_graph():
    """Build and compile the customer service agent graph."""
    graph = _build_core_graph()
    checkpointer = MemorySaver()
    compiled = graph.compile(checkpointer=checkpointer)

    print(f"[Graph] Customer service agent compiled (Real LLM)")
    print(f"[Graph] Nodes: {list(graph.nodes.keys())}")
    return compiled


def create_agent():
    """Convenience function."""
    return build_graph()
