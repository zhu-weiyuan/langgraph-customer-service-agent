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
import inspect
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
    previous = prev_values or {}
    if previous.get("messages"):
        history = list(previous["messages"])
    else:
        history = []
    try:
        retry_count = int(previous.get("retry_count", 0) or 0)
    except (TypeError, ValueError):
        retry_count = 0
    emotion = previous.get("emotion") or "neutral"
    try:
        emotion_intensity = int(previous.get("emotion_intensity", 1) or 1)
    except (TypeError, ValueError):
        emotion_intensity = 1
    state: Dict[str, Any] = {
        "messages": history + [HumanMessage(content=user_message)],
        "replies": [],
        "intent": "chat",
        "emotion": emotion,
        "emotion_intensity": emotion_intensity,
        "ending": False,
        "satisfaction": previous.get("satisfaction"),
        "retry_count": retry_count,
        "escalate": bool(previous.get("escalate", False)),
        "human_input": user_message,
    "session_id": session_id,
    }
    # Note: do not put trace_session into graph state
    # checkpointer serialization will fail (TraceSession not msgpack-serializable)
    if idempotency_key is not None:
        state["idempotency_key"] = idempotency_key
    if user_id:
        state["user_id"] = user_id
    if tenant_id:
        state["tenant_id"] = tenant_id
    if trace_session is not None:
        # Keep only the serializable correlation id, never the TraceSession
        # object itself.
        state["request_id"] = getattr(trace_session, "request_id", "") or ""
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
    """Classify a user-facing message for the lightweight stream fallback."""
    text = str(text or "")
    if any(w in text for w in ("\u6ee1\u610f", "\u6eff\u610f", "satisfied", "happy with")):
        return "satisfaction"
    if any(w in text for w in ("\u518d\u89c1", "\u518d\u898b", "\u62dc\u62dc", "goodbye", "bye")):
        return "closing"
    if any(w in text for w in ("escalate", "\u8f6c\u4eba\u5de5", "\u8f49\u4eba\u5de5", "Ticket", "switch")):
        return "escalated"
    return "reply"


def _message_content(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("content") or value.get("text") or "")
    return str(getattr(value, "content", value) or "")


def _is_ai_message(value: Any) -> bool:
    if isinstance(value, AIMessage):
        return True
    if isinstance(value, dict):
        role = str(value.get("role") or value.get("type") or "").lower()
        return role in {"assistant", "ai", "reply"}
    return str(getattr(value, "type", "")).lower() in {"ai", "assistant"}


def parse_result(values: Dict[str, Any], existing_count: int,
                 session_id: str,
                 interrupted: Optional[bool] = None) -> Dict[str, Any]:
    """Interpret the final graph result and return the HTTP/SSE contract."""
    values = values or {}
    messages = list(values.get("messages", []) or [])
    try:
        start = max(0, int(existing_count or 0))
    except (TypeError, ValueError):
        start = 0

    new_replies = []
    for msg in messages[start:]:
        if _is_ai_message(msg):
            content = _message_content(msg)
            if content:
                new_replies.append({"type": "reply", "content": content})

    raw_replies = values.get("replies", []) or []
    if not new_replies:
        for reply in raw_replies:
            content = _message_content(reply)
            if content:
                new_replies.append({"type": "reply", "content": content})

    reply_text = new_replies[-1]["content"] if new_replies else ""
    if not reply_text:
        for msg in reversed(messages):
            if _is_ai_message(msg):
                reply_text = _message_content(msg)
                break
    if not reply_text:
        reply_text = str(values.get("bot_reply") or "")

    intent = values.get("intent") or ""
    emotion = values.get("emotion") or ""
    emotion_intensity = values.get("emotion_intensity", 1) or 1
    try:
        emotion_intensity = int(emotion_intensity)
    except (TypeError, ValueError):
        emotion_intensity = 1

    has_interrupt = bool(
        interrupted
        or values.get("_interrupt_")
        or values.get("__interrupt__")
        or values.get("escalate")
    )
    return {
        "reply": reply_text,
        "replies": new_replies,
        "intent": intent,
        "emotion": emotion,
        "emotion_intensity": emotion_intensity,
        "reply_type": "escalated" if has_interrupt else "reply",
        "interrupted": has_interrupt,
        "retry_count": int(values.get("retry_count", 0) or 0),
        "ending": bool(values.get("ending", False)),
        "escalate": bool(values.get("escalate", False)) or has_interrupt,
        "next_action": "Escalated" if has_interrupt else "Active",
        "session_id": session_id,
    }


def _existing_message_count_sync(graph: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    """Read checkpoint state synchronously; returns {'count': int, 'values': dict}."""
    try:
        snapshot = graph.get_state(config)
    except Exception:
        return {"count": 0, "values": {}}
    values = getattr(snapshot, "values", None) or {}
    return {"count": len(values.get("messages", []) or []), "values": values}


async def _existing_message_count(graph: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    """Read a checkpoint from either an async or sync graph implementation."""
    aget_state = getattr(graph, "aget_state", None)
    if callable(aget_state):
        try:
            snapshot = aget_state(config)
            if inspect.isawaitable(snapshot):
                snapshot = await snapshot
            values = getattr(snapshot, "values", None) or {}
            return {"count": len(values.get("messages", []) or []),
                    "values": values}
        except Exception:
            # A test double or an older graph may expose a broken async method;
            # retain the sync fallback rather than losing conversation context.
            pass
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _existing_message_count_sync, graph, config)


async def run(session_id: str, user_message: str, *,
              trace_session: Any = None,
              idempotency_key: Optional[str] = None,
              timeout: Optional[float] = None,
              graph: Any = None,
              user_id: Optional[str] = None,
              tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Execute one round of conversation with sync and async graph support."""
    graph = graph or _get_graph_sync()
    config = {"configurable": {"thread_id": session_id}}
    snap = await _existing_message_count(graph, config)
    state = build_initial_state(session_id, user_message,
                                prev_values=snap["values"],
                                trace_session=trace_session,
                                idempotency_key=idempotency_key,
                                user_id=user_id,
                                tenant_id=tenant_id)

    t0 = time.time()
    if trace_session is not None:
        trace_session.add_event("graph_execution", {"status": "started"})

    effective_timeout = timeout if timeout is not None else graph_timeout_seconds()
    loop = asyncio.get_running_loop()
    has_sync_invoke = callable(getattr(graph, "invoke", None))
    has_async_invoke = callable(getattr(graph, "ainvoke", None))

    async def _invoke_async() -> Any:
        token = set_gateway_context(tenant_id=tenant_id or "default", user_id=user_id,
                                    trace_id=getattr(trace_session, "request_id", "") or "",
                                    idempotency_key=idempotency_key, trace_session=trace_session,
                                    scene="chat")
        try:
            result = graph.ainvoke(state, config=config)
            return await result if inspect.isawaitable(result) else result
        finally:
            reset_gateway_context(token)

    def _invoke_sync() -> Any:
        token = set_gateway_context(tenant_id=tenant_id or "default", user_id=user_id,
                                    trace_id=getattr(trace_session, "request_id", "") or "",
                                    idempotency_key=idempotency_key, trace_session=trace_session,
                                    scene="chat")
        try:
            return graph.invoke(state, config=config)
        finally:
            reset_gateway_context(token)

    if has_async_invoke and not has_sync_invoke:
        result_values = await asyncio.wait_for(_invoke_async(), timeout=effective_timeout)
    else:
        # Production currently uses the synchronous Postgres-backed graph. Keep
        # it off the event loop while still accepting async-only test doubles.
        result_values = await asyncio.wait_for(
            loop.run_in_executor(None, _invoke_sync), timeout=effective_timeout)

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
    """Stream progress/tokens from sync production graphs or async graph doubles."""
    request_t0 = time.monotonic()
    current_stage = {"name": None, "at": request_t0}
    got_first_token = False
    first_token_ms: Optional[float] = None
    streamed_any = False
    output_chars = 0

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

    def _emit_token(text: Any) -> Optional[Dict[str, Any]]:
        nonlocal streamed_any, output_chars
        text = str(text or "")
        if not text:
            return None
        _first_token_if_needed()
        streamed_any = True
        output_chars += len(text)
        return {"token": text}

    _mark_progress("analyzing")
    yield {"progress": "analyzing"}

    graph = graph or _get_graph_sync()
    config = {"configurable": {"thread_id": session_id}}
    snap = await _existing_message_count(graph, config)
    state = build_initial_state(session_id, user_message,
                                prev_values=snap["values"],
                                trace_session=trace_session,
                                idempotency_key=idempotency_key,
                                user_id=user_id,
                                tenant_id=tenant_id)
    deadline = time.monotonic() + (timeout if timeout is not None else graph_timeout_seconds())

    if trace_session is not None:
        trace_session.add_event("graph_execution",
                                {"status": "started", "stream": True})

    result_values: Dict[str, Any] = {}
    interrupted = False
    astream = getattr(graph, "astream", None)
    use_async_stream = callable(astream) and not callable(getattr(graph, "invoke", None))

    if use_async_stream:
        # Async-only graphs (and modern async LangGraph adapters) are consumed
        # through a small queue so the request timeout covers the whole stream.
        async_queue: asyncio.Queue = asyncio.Queue()

        async def _produce(mode: Any) -> None:
            token = set_gateway_context(tenant_id=tenant_id or "default", user_id=user_id,
                                        trace_id=getattr(trace_session, "request_id", "") or "",
                                        idempotency_key=idempotency_key, trace_session=trace_session,
                                        scene="chat")
            try:
                stream = astream(state, config=config, stream_mode=mode)
                if inspect.isawaitable(stream):
                    stream = await stream
                async for raw in stream:
                    await async_queue.put(("item", raw))
                await async_queue.put(("done", None))
            except BaseException as exc:
                await async_queue.put(("error", exc))
            finally:
                reset_gateway_context(token)

        stream_task: Optional[asyncio.Task] = None
        try:
            try:
                # Prefer token/message deltas plus state updates.
                stream_task = asyncio.create_task(_produce(["messages", "updates"]))
                kind, payload = await asyncio.wait_for(async_queue.get(),
                                                        timeout=max(0.01, deadline - time.monotonic()))
                if kind == "error" and isinstance(payload, TypeError):
                    stream_task.cancel()
                    with __import__('contextlib').suppress(asyncio.CancelledError):
                        await stream_task
                    async_queue = asyncio.Queue()
                    stream_task = asyncio.create_task(_produce("updates"))
                    kind, payload = await asyncio.wait_for(async_queue.get(),
                                                            timeout=max(0.01, deadline - time.monotonic()))
                # Process the first item below, then continue normally.
                pending = (kind, payload)
            except asyncio.TimeoutError:
                raise

            while True:
                kind, raw = pending
                if kind == "error":
                    raise raw
                if kind == "done":
                    break
                if isinstance(raw, tuple) and len(raw) == 2 and raw[0] in {"messages", "updates"}:
                    mode, payload = raw
                    if mode == "messages":
                        chunk = payload[0] if isinstance(payload, tuple) and payload else payload
                        was_first = not got_first_token
                        token_frame = _emit_token(_message_content(chunk))
                        if was_first and token_frame:
                            yield {"progress": "first_token",
                                   "ttft_ms": round(first_token_ms or 0, 2)}
                        if token_frame:
                            yield token_frame
                    else:
                        update = payload if isinstance(payload, dict) else {}
                        for node, delta in update.items():
                            node_name = str(node)
                            if node_name in {"__interrupt__", "_interrupt_"}:
                                interrupted = True
                                continue
                            if isinstance(delta, dict):
                                _mark_progress(node_name)
                                yield {"progress": node_name}
                                result_values.update(delta)
                            else:
                                result_values[node_name] = delta
                                _mark_progress(node_name)
                                yield {"progress": node_name}
                elif isinstance(raw, dict):
                    if raw.get("telemetry"):
                        _record_trace_telemetry(trace_session, raw)
                    elif raw.get("progress"):
                        _mark_progress(str(raw["progress"]))
                        yield raw
                    elif raw.get("token"):
                        was_first = not got_first_token
                        token_frame = _emit_token(raw["token"])
                        if was_first:
                            yield {"progress": "first_token",
                                   "ttft_ms": round(first_token_ms or 0, 2)}
                        if token_frame:
                            yield token_frame
                    else:
                        for node, delta in raw.items():
                            if str(node) in {"__interrupt__", "_interrupt_"}:
                                interrupted = True
                                continue
                            if isinstance(delta, dict):
                                _mark_progress(str(node))
                                yield {"progress": str(node)}
                                result_values.update(delta)
                            else:
                                result_values[str(node)] = delta
                                _mark_progress(str(node))
                                yield {"progress": str(node)}
                else:
                    was_first = not got_first_token
                    token_frame = _emit_token(raw)
                    if was_first:
                        yield {"progress": "first_token",
                               "ttft_ms": round(first_token_ms or 0, 2)}
                    if token_frame:
                        yield token_frame

                if kind == "done":
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                pending = await asyncio.wait_for(async_queue.get(), timeout=remaining)

            # Older update-only graphs expose only the completed node output.
            # Keep the HTTP contract streaming-shaped while making the fallback
            # visible to the client instead of waiting silently.
            if not streamed_any and result_values.get("bot_reply"):
                for part in chunk_text(str(result_values["bot_reply"])):
                    was_first = not got_first_token
                    token_frame = _emit_token(part)
                    if was_first:
                        yield {"progress": "first_token",
                               "ttft_ms": round(first_token_ms or 0, 2)}
                    if token_frame:
                        yield token_frame
                    await asyncio.sleep(0)
        finally:
            if stream_task is not None and not stream_task.done():
                stream_task.cancel()
                with __import__('contextlib').suppress(asyncio.CancelledError):
                    await stream_task
    else:
        # Production path: synchronous Postgres-backed graph.invoke() runs in a
        # worker thread, while nodes publish true provider deltas to this queue.
        import queue as queue_mod
        from . import nodes as _nodes
        sync_q: queue_mod.Queue = queue_mod.Queue()
        sentinel = object()
        exc_info: list = []

        def _run_in_thread() -> Dict[str, Any]:
            _nodes._stream_queue_local.queue = sync_q
            token = set_gateway_context(tenant_id=tenant_id or "default", user_id=user_id,
                                        trace_id=getattr(trace_session, "request_id", "") or "",
                                        idempotency_key=idempotency_key, trace_session=trace_session,
                                        scene="chat")
            try:
                return graph.invoke(state, config=config)
            except BaseException as exc:
                exc_info.append(exc)
                raise
            finally:
                reset_gateway_context(token)
                with __import__('contextlib').suppress(AttributeError):
                    del _nodes._stream_queue_local.queue
                sync_q.put(sentinel)

        loop = asyncio.get_running_loop()
        result_future = loop.run_in_executor(None, _run_in_thread)
        background_result_reported = False

        def _report_background_result(completed: asyncio.Future) -> None:
            try:
                completed.result()
            except asyncio.CancelledError:
                logger.warning("graph worker was cancelled after stream timeout: session=%s", session_id)
            except Exception:
                logger.warning(
                    "graph worker finished after stream timeout with an error: session=%s",
                    session_id, exc_info=True,
                )
            else:
                logger.warning(
                    "graph worker finished after stream timeout; its late result was not sent: session=%s",
                    session_id,
                )

        def _observe_background_worker() -> None:
            nonlocal background_result_reported
            if not background_result_reported:
                background_result_reported = True
                result_future.add_done_callback(_report_background_result)

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Cancelling the asyncio Future cannot stop an already-running
                    # synchronous graph.invoke() thread. Keep observing it instead
                    # of claiming cancellation succeeded, and return the timeout to
                    # the caller while concurrency/rate limits protect the service.
                    _observe_background_worker()
                    logger.warning(
                        "graph stream timed out; synchronous worker may continue in background: session=%s",
                        session_id,
                    )
                    raise asyncio.TimeoutError()
                try:
                    item = await asyncio.wait_for(
                        loop.run_in_executor(None, sync_q.get, True, min(0.05, remaining)),
                        timeout=min(0.1, remaining))
                except (asyncio.TimeoutError, queue_mod.Empty):
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
                        _mark_progress(str(item["progress"]))
                    if item.get("token"):
                        was_first = not got_first_token
                        token_frame = _emit_token(item["token"])
                        if was_first:
                            yield {"progress": "first_token",
                                   "ttft_ms": round(first_token_ms or 0, 2)}
                        if token_frame:
                            yield token_frame
                    elif item.get("progress"):
                        yield item
                    else:
                        yield item
                else:
                    was_first = not got_first_token
                    token_frame = _emit_token(item)
                    if was_first:
                        yield {"progress": "first_token",
                               "ttft_ms": round(first_token_ms or 0, 2)}
                    if token_frame:
                        yield token_frame
            if exc_info:
                raise exc_info[0]
            remaining = deadline - time.monotonic()
            result_values = await asyncio.wait_for(
                asyncio.shield(result_future), timeout=max(0.01, remaining))
        finally:
            if not result_future.done():
                _observe_background_worker()

    if not isinstance(result_values, dict):
        result_values = {}
    if interrupted:
        result_values["__interrupt__"] = True

    parsed = parse_result(result_values, snap["count"], session_id,
                          interrupted=interrupted)
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
        "reply_type": "escalated" if interrupted else parsed["reply_type"],
        "interrupted": bool(interrupted or parsed["interrupted"]),
        "ending": parsed["ending"],
        "escalate": parsed.get("escalate", False) or interrupted,
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
    "prewarm", "shutdown",
    "run", "run_stream",
    "graph_timeout_seconds",
]

