"""Graph builder - LangGraph Customer Service Agent (P1-A rewrite).

Conversation flow
-----------------
1. User asks -> intent -> reply (no satisfaction probe yet)
2. User keeps asking -> keep answering
3. User signals ending ("bye", "thanks") -> ask satisfaction -> resolve /
   retry / escalate-to-human / finalize

P1-A fixes
----------
* The inline duplicate ``CustomerServiceState`` (whose ``x + [y]`` reducer
  nested message lists) is GONE — the single definition lives in
  :mod:`agent.state`.
* ``route_after_reply`` can return ``'finalize'``; the conditional-edge map
  now contains that target (previously a guaranteed crash on any retry turn).
* Escalation is reachable: after ``process_satisfaction``, two consecutive
  dissatisfied turns (retry_count >= 2) or strong negative emotion routes to
  ``escalate_to_human`` (which uses ``langgraph.types.interrupt`` internally).
* Checkpoints are PostgreSQL-only. SQLite and in-memory fallbacks are disabled
  so a configuration error fails fast instead of losing refreshed conversations.

Async usage (app layer)
-----------------------
``make_checkpointer()`` returns an **async context manager**::

    async with make_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)
        result = await graph.ainvoke(inputs, config)
        # or: async for event in graph.astream(inputs, config): ...

All langgraph imports are guarded or deferred so this module always imports
and compiles in environments without langgraph installed (routing functions
are pure and unit-testable there).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("agent.graph")

# ── Guarded third-party imports ─────────────────────────────────────
try:
    from langgraph.graph import StateGraph, START, END
except Exception:  # pragma: no cover - bare container
    StateGraph = None
    START = "__start__"
    END = "__end__"

from .state import CustomerServiceState

# ── Escalation policy (tunable) ─────────────────────────────────────
NEGATIVE_EMOTIONS = frozenset({"angry", "sad", "anxious"})
ESCALATE_RETRY_THRESHOLD = 2          # 连续不满意达到最大重试次数 → 转人工
ESCALATE_EMOTION_INTENSITY = 4        # 负面情绪强度 ≥ 4 → 转人工
MAX_RETRIES = 3



# ════════════════════════════════════════════════════════════════════
# Routing functions — pure, stdlib-only, unit-testable without langgraph
# ════════════════════════════════════════════════════════════════════

def route_after_intent(state: dict) -> str:
    """Route a resumed turn awaiting satisfaction or a normal new turn."""
    if state.get("awaiting_satisfaction", False):
        return "process_satisfaction"
    return "generate_reply"


def route_after_reply(state: dict) -> str:
    """After generating a reply, decide the next step.

    - user signalled ending           -> 'check_satisfaction'
    - this was a retry (retry>0)      -> 'finalize' and wait for next turn
    - otherwise                       -> END (wait for the next user message)
    """
    if state.get("ending", False):
        return "check_satisfaction"
    if state.get("retry_count", 0) > 0:
        # A retry turn has already passed through satisfaction handling;
        # persist the result and wait for the next user turn.
        return "finalize"
    return END


# Every value route_after_reply can return MUST appear as a key here.
ROUTE_AFTER_REPLY_MAP = {
    "check_satisfaction": "check_satisfaction",
    "finalize": "finalize",
    END: END,
}


def should_resolve(state: dict) -> str:
    """After processing satisfaction feedback, decide the next step.

    - satisfied                                        -> 'finalize'
    - dissatisfied twice in a row OR strong negative
      emotion (intensity >= 4 on angry/sad/anxious)    -> 'escalate_to_human'
    - dissatisfied, still under retry budget           -> 'generate_reply'
    - undeterminable / retries exhausted               -> 'finalize'
    """
    satisfaction = state.get("satisfaction")
    retry_count = state.get("retry_count", 0)
    emotion = state.get("emotion")
    intensity = state.get("emotion_intensity", 1)

    if satisfaction is True:
        return "finalize"

    strong_negative = (emotion in NEGATIVE_EMOTIONS
                       and intensity >= ESCALATE_EMOTION_INTENSITY)
    if retry_count >= ESCALATE_RETRY_THRESHOLD or strong_negative:
        return "escalate_to_human"

    if satisfaction is False and retry_count < MAX_RETRIES:
        return "generate_reply"

    return "finalize"


# Every value should_resolve can return MUST appear as a key here.
ROUTE_AFTER_SATISFACTION_MAP = {
    "generate_reply": "generate_reply",
    "escalate_to_human": "escalate_to_human",
    "finalize": "finalize",
}


# ════════════════════════════════════════════════════════════════════
# Graph construction
# ════════════════════════════════════════════════════════════════════

def _build_core_graph():
    """Build the StateGraph topology (deferred node imports)."""
    if StateGraph is None:
        raise RuntimeError(
            "langgraph is not installed — cannot build the StateGraph. "
            "Routing functions remain importable/testable without it.")

    # Deferred import: nodes pulls in LLM/RAG integrations that may not be
    # present in minimal environments.
    from .nodes import (
        identify_intent,
        generate_reply,
        check_satisfaction,
        process_satisfaction,
        escalate_to_human,
        finalize,
    )

    graph = StateGraph(CustomerServiceState)

    graph.add_node("identify_intent", identify_intent)
    graph.add_node("generate_reply", generate_reply)
    graph.add_node("check_satisfaction", check_satisfaction)
    graph.add_node("process_satisfaction", process_satisfaction)
    graph.add_node("escalate_to_human", escalate_to_human)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "identify_intent")
    graph.add_edge("identify_intent", "generate_reply")

    # FIXED: the map now covers ALL possible returns, including 'finalize'
    # (previously missing -> KeyError crash on every retry turn).
    graph.add_conditional_edges("generate_reply", route_after_reply,
                                dict(ROUTE_AFTER_REPLY_MAP))

    graph.add_edge("check_satisfaction", "process_satisfaction")

    # FIXED: escalate_to_human is now reachable via should_resolve.
    graph.add_conditional_edges("process_satisfaction", should_resolve,
                                dict(ROUTE_AFTER_SATISFACTION_MAP))

    # After the human intervenes (interrupt resumes), close out gracefully.
    graph.add_edge("escalate_to_human", "finalize")
    graph.add_edge("finalize", END)

    return graph


# ════════════════════════════════════════════════════════════════════
# Checkpointer factories
# ════════════════════════════════════════════════════════════════════

def _postgres_dsn(explicit: str | None = None) -> str:
    """Return the configured PostgreSQL DSN or fail fast.

    The live project is PostgreSQL-only. Silently switching to SQLite or an
    in-memory checkpointer would make refreshed conversations disappear.
    """
    resolved = explicit or os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL")
    if not resolved:
        raise RuntimeError(
            "POSTGRES_DSN (or DATABASE_URL) is required; SQLite fallback is disabled"
        )
    return resolved


def make_checkpointer(db_path: str | None = None):
    """Create the async PostgreSQL checkpointer context manager."""
    if db_path:
        raise RuntimeError("SQLite checkpoint paths are disabled")
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    logger.info("Checkpointer: AsyncPostgresSaver (PostgreSQL-only)")
    return AsyncPostgresSaver.from_conn_string(_postgres_dsn())


def make_sync_checkpointer(db_path: str | None = None):
    """Reject the removed SQLite checkpointer compatibility path."""
    raise RuntimeError(
        "SQLite checkpointer is disabled; use make_sync_postgres_checkpointer()"
    )


# ════════════════════════════════════════════════════════════════════
# Public build API
# ════════════════════════════════════════════════════════════════════

def make_sync_postgres_checkpointer(dsn: str | None = None):
    """创建同步 PostgresSaver 用于生产环境 checkpoint 持久化。

    内部打开一个 psycopg 长连接（进程生存期内保持打开）。
    调用方需持有返回的 saver 引用；saver 被 GC 时连接自动关闭。

    Args:
        dsn: PostgreSQL 连接串。未指定时从环境变量 ``POSTGRES_DSN`` 读取。
    """
    import psycopg
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres import PostgresSaver

    resolved = _postgres_dsn(dsn)
    logger.info("Checkpointer: creating sync PostgresSaver …")
    conn = psycopg.connect(
        resolved,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    )
    try:
        saver = PostgresSaver(conn)
        saver.setup()
    except Exception:
        conn.close()
        raise
    logger.info("Checkpointer: sync PostgresSaver ready")
    return saver


def build_graph(checkpointer=None, *, use_sqlite: bool = False,
                use_postgres: bool = False,
                db_path: str | None = None):
    """Build and compile the customer-service agent graph.

    Args:
        checkpointer: a ready checkpointer instance. Preferred path.
        use_sqlite: retained for source compatibility; True is rejected.
        use_postgres: must remain True unless a ready PostgreSQL checkpointer
            is injected explicitly.
        db_path: retained for source compatibility; any value is rejected.
    """
    graph = _build_core_graph()

    if use_sqlite or db_path:
        raise RuntimeError("SQLite checkpointing is disabled for this project")
    if checkpointer is None:
        if not use_postgres:
            raise RuntimeError(
                "A PostgreSQL checkpointer is required; in-memory fallback is disabled"
            )
        checkpointer = make_sync_postgres_checkpointer()

    compiled = graph.compile(checkpointer=checkpointer)
    logger.info("Customer service agent compiled; nodes=%s",
                list(graph.nodes.keys()))
    return compiled


def create_agent():
    """Convenience function (legacy)."""
    return build_graph(use_postgres=True)
