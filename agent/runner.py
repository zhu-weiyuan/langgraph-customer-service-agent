# -*- coding: utf-8 -*-
"""
agent/runner.py 鈥?async 鎵ц灞傦紙P2 闆嗘垚鐗堬級銆?

鏇夸唬 app_original_sync.run_agent / run_agent_stream 瀵?sync graph 鐨勪緷璧栵細

graph + checkpointer 妯″潡绾?lazy 鍗曚緥锛坅syncio.Lock 淇濇姢锛夛紱lifespan 閲?prewarm()锛宻hutdown() 閲屽叧闂?checkpointer async context銆?

浣跨敤鍚屾 PostgresSaver + graph.invoke()锛坰ync锛夛紝閬垮厤 AsyncPostgresSaver
鐨?asyncio.Lock 璺ㄤ簨浠跺惊鐜啿绐併€俫raph.invoke() 鍦?run_in_executor 绾跨▼姹犱腑鎵ц锛屼笉娑夊強 asyncio.Lock 鎴栬緟鍔╀簨浠跺惊鐜€?
run()锛氱粍瑁?initial state 鈫?graph.invoke锛坰ync锛宼hread pool锛夆啋 瑙ｆ瀽
run_stream()锛氱粍瑁?initial state + queue 鈫?绾跨▼涓?graph.invoke锛坰ync锛宼hread pool锛?
    鈫?涓诲崗绋嬭疆璇?queue 骞?yield token / progress / done 甯с€?
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage

try:
    from .token_estimator import estimate_messages_tokens, estimate_tokens
except Exception:  # pragma: no cover
    def estimate_tokens(text: str) -> int:
        return max(1, len(str(text or "")) // 4)

    def estimate_messages_tokens(messages) -> int:
        return sum(estimate_tokens(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")) for m in (messages or []))

try:
    from .metrics import record_llm_request, record_llm_tokens
except Exception:  # pragma: no cover
    def record_llm_request(*args, **kwargs):
        return None

    def record_llm_tokens(*args, **kwargs):
        return None

try:
    from .llm_gateway import set_gateway_context, reset_gateway_context
except Exception:  # pragma: no cover
    def set_gateway_context(**values):
        return None

    def reset_gateway_context(token):
        return None

logger = logging.getLogger("agent.runner")


def _llm_model_name() -> str:
    return (os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL") or
            os.getenv("MODEL_NAME") or "openai-compatible").strip()


def _safe_trace_call(trace_session: Any, method: str, *args, **kwargs) -> None:
    if trace_session is None:
        return
    try:
        getattr(trace_session, method)(*args, **kwargs)
    except Exception:
        logger.debug("trace.%s failed", method, exc_info=True)


def _record_trace_telemetry(trace_session: Any, item: Dict[str, Any]) -> None:
    """Consume internal node telemetry frames and store them in TraceSession."""
    if trace_session is None:
        return
    kind = str(item.get("telemetry") or "")
    payload = {k: v for k, v in item.items() if k != "telemetry"}
    duration_ms = float(item.get("duration_ms") or 0.0)
    _safe_trace_call(trace_session, "add_event", f"{kind}_telemetry", payload,
                     duration_ms=duration_ms)
    if kind == "retrieval":
        hits = item.get("hits") or []
        chunks = [h.get("text") or h.get("content") or h.get("title") or ""
                  for h in hits if isinstance(h, dict)]
        scores = [h.get("score") for h in hits if isinstance(h, dict)]
        sources = [h.get("source") or h.get("title") or ""
                   for h in hits if isinstance(h, dict)]
        _safe_trace_call(trace_session, "record_retrieval",
                         query=str(item.get("query") or ""),
                         chunks=chunks, scores=scores, sources=sources)
        if duration_ms:
            _safe_trace_call(trace_session, "record_latency", retrieval_ms=duration_ms)
    elif kind == "memory":
        normalized = []
        for h in (item.get("hits") or []):
            if isinstance(h, dict):
                d = dict(h)
                d.setdefault("confidence", d.get("relevance", d.get("score")))
                normalized.append(d)
        _safe_trace_call(trace_session, "record_memory", normalized)
    elif kind == "prompt":
        prompt_version = str(item.get("prompt_version") or "")
        if ":" in prompt_version:
            template_name, version = prompt_version.split(":", 1)
        else:
            template_name, version = "system", prompt_version
        _safe_trace_call(trace_session, "record_prompt",
                         template_name=template_name, version=version,
                         variables={"token_budget": item.get("token_budget"),
                                    "source_counts": item.get("source_counts")},
                         rendered_messages=[])

DEFAULT_GRAPH_TIMEOUT = 120

# Module-level graph singleton (sync checkpointer, no event loop binding)
_graph: Any = None
_checkpointer_cm: Any = None


def graph_timeout_seconds() -> float:
    """Request-level timeout (env GRAPH_TIMEOUT_SECONDS, default 120s)."""
    try:
        return float(os.getenv("GRAPH_TIMEOUT_SECONDS", str(DEFAULT_GRAPH_TIMEOUT)))
    except ValueError:
        return DEFAULT_GRAPH_TIMEOUT


def build_initial_state(
    session_id: str,
    user_message: str,
    prev_values: Optional[Dict[str, Any]] = None,
    trace_session: Any = None,
    idempotency_key: Optional[str] = None,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble langgraph initial state.

    Parameters
    ----------
    prev_values : dict or None
        Checkpoint values from the last message (not including the latest human message).

    Returns
    -------
    State dict with messages / replies / intent / emotion / ending keys.
    """
    if prev_values and prev_values.get("messages"):
        history = list(prev_values["messages"])
    else:
        history = []
    state: Dict[str, Any] = {
        "messages": history + [HumanMessage(content=user_message)],
        "replies": [],
        "intent": "chat",
        "emotion": "neutral",
        "emotion_intensity": 1,
        "ending": False,
        "human_input": user_message,
    "session_id": session_id,
    }
    # Note: do not put trace_session into graph state
    # checkpointer serialization will fail (TraceSession not msgpack-serializable)
    if idempotency_key is not None:
        state["_idempotency_key"] = idempotency_key
    if user_id:
        state["user_id"] = user_id
    return state


def chunk_text(text: str, size: int = 3) -> list:
    """Split text into fixed-length chunks (text guard for streaming fallback)."""
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]


def coalesce_stream_tokens(token_iter, *, max_chars: int = 24,
                           max_delay_s: float = 0.12):
    """Group tiny provider deltas without waiting for completion.

    Some OpenAI-compatible backends emit one character at a time. Forwarding
    each tiny delta to Vue can cause hundreds of reactive DOM updates per
    answer. This still streams while the model is generating: a chunk is flushed
    as soon as it is large enough or has waited ~120ms.
    """
    buf = ""
    last_flush = time.monotonic()
    for token in token_iter:
        if not token:
            continue
        buf += str(token)
        now = time.monotonic()
        if len(buf) >= max_chars or (now - last_flush) >= max_delay_s:
            yield buf
            buf = ""
            last_flush = now
    if buf:
        yield buf


def classify_message(text: str) -> str:
    """Simple reply type classification (stock fallback, not called by real graph)."""
    if any(w in text for w in ("escalate", "]", "Ticket", "switch")):
        return "escalated"
    return "reply"


def parse_result(values: Dict[str, Any], existing_count: int,
                 session_id: str,
                 interrupted: Optional[bool] = None) -> Dict[str, Any]:
    """Interpret the final graph.ainvoke / astream(updates) values dict.
    Returns the dict used by the app/json layer.
    """
    replies: list = values.get("replies", []) or []
    retry_count: int = values.get("retry_count", 0) or 0

    if not isinstance(retry_count, int):
        retry_count = int(retry_count) if retry_count else 0

    reply_text = ""
    for r in reversed(replies):
        if isinstance(r, dict) and r.get("type") == "ai":
            reply_text = r.get("content", "")
            break
    if not reply_text:
        msgs = values.get("messages", []) or []
        for msg in reversed(msgs):
            if isinstance(msg, AIMessage):
                reply_text = msg.content or ""
                break

    intent = values.get("intent") or ""
    emotion = values.get("emotion") or ""
    emotion_intensity = values.get("emotion_intensity", 1) or 1
    if isinstance(emotion_intensity, str):
        try:
            emotion_intensity = int(emotion_intensity)
        except ValueError:
            emotion_intensity = 1
    interrupted = interrupted or bool(values.get("_interrupt_"))

    return {
        "reply": reply_text,
        "intent": intent,
        "emotion": emotion,
        "emotion_intensity": emotion_intensity,
        "reply_type": "escalated" if interrupted else "reply",
        "interrupted": interrupted,
        "retry_count": retry_count,
        "ending": bool(values.get("ending", False)),
        "escalate": bool(values.get("escalate", False)) or bool(values.get("_interrupt_", False)),
    }


def _existing_message_count_sync(graph: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    """Read checkpoint state synchronously; returns {'count': int, 'values': dict}."""
    try:
        snapshot = graph.get_state(config)
    except Exception:
        return {"count": 0, "values": {}}
    values = getattr(snapshot, "values", None) or {}
    return {"count": len(values.get("messages", []) or []), "values": values}


async def run(session_id: str, user_message: str, *,
              trace_session: Any = None,
              idempotency_key: Optional[str] = None,
              timeout: Optional[float] = None,
              graph: Any = None,
              user_id: Optional[str] = None,
              tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Execute one round of conversation (graph.invoke + total timeout)."""
    graph = graph or _get_graph_sync()
    config = {"configurable": {"thread_id": session_id}}
    loop = asyncio.get_event_loop()
    snap = await loop.run_in_executor(
        None, _existing_message_count_sync, graph, config)
    state = build_initial_state(session_id, user_message,
                                prev_values=snap["values"],
                                trace_session=trace_session,
                                idempotency_key=idempotency_key,
                                user_id=user_id)

    t0 = time.time()
    if trace_session is not None:
        trace_session.add_event("graph_execution", {"status": "started"})

    effective_timeout = timeout if timeout is not None else graph_timeout_seconds()

    def _run_sync() -> Dict[str, Any]:
        token = set_gateway_context(tenant_id=tenant_id or "default", user_id=user_id,
                                    trace_id=getattr(trace_session, "request_id", "") or "",
                                    idempotency_key=idempotency_key, trace_session=trace_session,
                                    scene="chat")
        try:
            return graph.invoke(state, config=config)
        finally:
            reset_gateway_context(token)

    try:
        result_values = await asyncio.wait_for(
            loop.run_in_executor(None, _run_sync),
            timeout=effective_timeout)
    except asyncio.TimeoutError:
        raise

    if not isinstance(result_values, dict):
        result_values = {}

    parsed = parse_result(result_values, snap["count"], session_id)
    if trace_session is not None:
        duration = (time.time() - t0) * 1000
        trace_session.add_event("graph_execution", {
            "status": "completed",
            "interrupted": parsed["interrupted"],
            "duration_ms": round(duration, 2),
        }, duration_ms=duration)
        _safe_trace_call(trace_session, "record_result", parsed.get("reply", ""), parsed)
        model = _llm_model_name()
        in_tok = estimate_tokens(user_message or "")
        out_tok = estimate_tokens(parsed.get("reply", ""))
        _safe_trace_call(trace_session, "record_model",
                         provider=os.getenv("LLM_PROVIDER", "openai-compatible"),
                         model=model, params={"stream": False},
                         in_tok=in_tok, out_tok=out_tok, finish="stop",
                         stage="generate")
        _safe_trace_call(trace_session, "record_latency", total_ms=duration)
        with __import__('contextlib').suppress(Exception):
            record_llm_request(model, "success")
            record_llm_tokens(model, "chat", "input", in_tok)
            record_llm_tokens(model, "chat", "output", out_tok)
    return parsed


async def run_stream(session_id: str, user_message: str, *,
                     trace_session: Any = None,
                     idempotency_key: Optional[str] = None,
                     timeout: Optional[float] = None,
                     graph: Any = None,
                     user_id: Optional[str] = None,
                     tenant_id: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    """Real streaming: yield {"progress"} / {"token"} / {"done": True, ...} frames."""
    request_t0 = time.monotonic()
    current_stage = {"name": None, "at": request_t0}

    def _mark_progress(stage: str) -> None:
        now = time.monotonic()
        previous = current_stage.get("name")
        previous_at = float(current_stage.get("at") or request_t0)
        if previous == stage:
            return
        if previous:
            elapsed = (now - previous_at) * 1000
            _safe_trace_call(trace_session, "add_event", "stage_timing",
                             {"stage": previous, "next_stage": stage,
                              "duration_ms": round(elapsed, 2)},
                             duration_ms=elapsed)
        _safe_trace_call(trace_session, "add_event", "stage_progress",
                         {"stage": stage,
                          "elapsed_ms": round((now - request_t0) * 1000, 2)})
        current_stage["name"] = stage
        current_stage["at"] = now

    def _first_token_if_needed() -> None:
        nonlocal got_first_token, first_token_ms
        if got_first_token:
            return
        got_first_token = True
        first_token_ms = (time.monotonic() - request_t0) * 1000
        _safe_trace_call(trace_session, "add_event", "first_token",
                         {"ttft_ms": round(first_token_ms, 2)})
        _safe_trace_call(trace_session, "record_latency",
                         model_ttft_ms=first_token_ms)

    _mark_progress("analyzing")
    yield {"progress": "analyzing"}

    graph = graph or _get_graph_sync()
    config = {"configurable": {"thread_id": session_id}}
    loop = asyncio.get_event_loop()
    snap = await loop.run_in_executor(
        None, _existing_message_count_sync, graph, config)
    state = build_initial_state(session_id, user_message,
                                prev_values=snap["values"],
                                trace_session=trace_session,
                                idempotency_key=idempotency_key,
                                user_id=user_id)

    deadline = time.monotonic() + (timeout if timeout is not None else graph_timeout_seconds())

    import queue as queue_mod
    sync_q: queue_mod.Queue = queue_mod.Queue()
    from . import nodes as _nodes
    sentinel = object()

    if trace_session is not None:
        trace_session.add_event("graph_execution",
                                {"status": "started", "stream": True})

    exc_info: list = []

    def _run_in_thread() -> Dict[str, Any]:
        _nodes._stream_queue_local.queue = sync_q
        token = set_gateway_context(tenant_id=tenant_id or "default", user_id=user_id,
                                    trace_id=getattr(trace_session, "request_id", "") or "",
                                    idempotency_key=idempotency_key, trace_session=trace_session,
                                    scene="chat")
        try:
            return graph.invoke(state, config=config)
        except BaseException as e:
            exc_info.append(e)
            raise
        finally:
            reset_gateway_context(token)
            try:
                del _nodes._stream_queue_local.queue
            except AttributeError:
                pass
            sync_q.put(sentinel)

    main_loop = asyncio.get_event_loop()
    result_future = main_loop.run_in_executor(None, _run_in_thread)

    streamed_any = False
    got_first_token = False
    first_token_ms: Optional[float] = None
    output_chars = 0
    loop_finished = False
    last_heartbeat = time.monotonic()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result_future.cancel()
                raise asyncio.TimeoutError()

            now = time.monotonic()
            if not got_first_token and (now - last_heartbeat) >= 0.5:
                yield {"progress": "analyzing"}
                last_heartbeat = now

            try:
                item = sync_q.get(timeout=min(0.05, remaining))
            except queue_mod.Empty:
                if result_future.done():
                    break
                continue

            if item is sentinel:
                break

            if isinstance(item, dict):
                if item.get("telemetry"):
                    _record_trace_telemetry(trace_session, item)
                    continue
                if item.get("progress"):
                    _mark_progress(str(item.get("progress")))
                if item.get("token"):
                    if not got_first_token:
                        _first_token_if_needed()
                        yield {"progress": "first_token",
                               "ttft_ms": round(first_token_ms or 0, 2)}
                    streamed_any = True
                    output_chars += len(str(item.get("token") or ""))
                yield item
                continue

            if not got_first_token:
                _first_token_if_needed()
                yield {"progress": "first_token",
                       "ttft_ms": round(first_token_ms or 0, 2)}
            streamed_any = True
            output_chars += len(str(item or ""))
            yield {"token": item}

        await asyncio.sleep(0.03)
        while True:
            try:
                item = sync_q.get_nowait()
                if item is sentinel:
                    continue
                if isinstance(item, dict):
                    if item.get("telemetry"):
                        _record_trace_telemetry(trace_session, item)
                        continue
                    if item.get("progress"):
                        _mark_progress(str(item.get("progress")))
                    if item.get("token"):
                        if not got_first_token:
                            _first_token_if_needed()
                            yield {"progress": "first_token",
                                   "ttft_ms": round(first_token_ms or 0, 2)}
                        streamed_any = True
                        output_chars += len(str(item.get("token") or ""))
                    yield item
                else:
                    if not got_first_token:
                        _first_token_if_needed()
                        yield {"progress": "first_token",
                               "ttft_ms": round(first_token_ms or 0, 2)}
                    streamed_any = True
                    output_chars += len(str(item or ""))
                    yield {"token": item}
            except queue_mod.Empty:
                break
        loop_finished = True
    finally:
        if (not loop_finished) and result_future is not None and not result_future.done():
            result_future.cancel()

    if exc_info:
        raise exc_info[0]

    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        result_values = await asyncio.wait_for(result_future, timeout=remaining)
    except asyncio.TimeoutError:
        raise

    if not isinstance(result_values, dict):
        result_values = {}

    parsed = parse_result(result_values, snap["count"], session_id)
    total_ms = (time.monotonic() - request_t0) * 1000
    if trace_session is not None:
        _mark_progress("done")
        _safe_trace_call(trace_session, "add_event", "stream_summary",
                         {"streamed_any": streamed_any,
                          "first_token_ms": (None if first_token_ms is None else round(first_token_ms, 2)),
                          "output_chars": output_chars,
                          "duration_ms": round(total_ms, 2)},
                         duration_ms=total_ms)
        _safe_trace_call(trace_session, "record_result", parsed.get("reply", ""), parsed)
        model = _llm_model_name()
        in_tok = estimate_tokens(user_message or "")
        out_tok = estimate_tokens(parsed.get("reply", ""))
        _safe_trace_call(trace_session, "record_model",
                         provider=os.getenv("LLM_PROVIDER", "openai-compatible"),
                         model=model, params={"stream": True},
                         in_tok=in_tok, out_tok=out_tok, finish="stop",
                         ttft_ms=first_token_ms, stage="generate")
        _safe_trace_call(trace_session, "record_latency", total_ms=total_ms)
        with __import__('contextlib').suppress(Exception):
            record_llm_request(model, "success")
            record_llm_tokens(model, "chat", "input", in_tok)
            record_llm_tokens(model, "chat", "output", out_tok)
    yield {
        "done": True,
        "session_id": session_id,
        "reply": parsed.get("reply", ""),
        "intent": parsed["intent"],
        "emotion": parsed["emotion"],
        "emotion_intensity": parsed["emotion_intensity"],
        "reply_type": parsed["reply_type"],
        "interrupted": parsed["interrupted"],
        "ending": parsed["ending"],
        "escalate": parsed.get("escalate", False),
    }


# Module-level helpers (stubs for get_graph / prewarm / shutdown)
def _get_graph_sync() -> Any:
    """Get or create the sync graph singleton."""
    global _graph
    if _graph is not None:
        return _graph
    from agent.graph import build_graph
    _graph = build_graph(use_postgres=True)
    return _graph


async def prewarm() -> bool:
    """Lifespan startup: ensure graph is compiled and checkpointer ready.

    Returns False (instead of blocking or crashing) when PostgreSQL is
    unavailable so that health/readiness endpoints and unit tests can
    still run against the FastAPI lifespan without a live database.
    """
    try:
        graph = _get_graph_sync()
        logger.info("graph prewarm: ok (PostgreSQL checkpointer)")
        return True
    except Exception as exc:
        logger.warning("graph prewarm: unavailable (%s)", exc)
        return False


async def shutdown() -> None:
    """Close the PostgreSQL checkpointer and clear the graph singleton."""
    global _graph, _checkpointer_cm
    graph = _graph
    _graph = None
    _checkpointer_cm = None
    checkpointer = getattr(graph, "checkpointer", None) if graph is not None else None
    conn = getattr(checkpointer, "conn", None)
    if conn is not None and not getattr(conn, "closed", False):
        await asyncio.to_thread(conn.close)
        logger.info("PostgreSQL checkpointer connection closed")


__all__ = [
    "build_initial_state", "parse_result", "chunk_text", "classify_message",
    "get_graph", "prewarm", "shutdown",
    "run", "run_stream",
    "graph_timeout_seconds",
]

