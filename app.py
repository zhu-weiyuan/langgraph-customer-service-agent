# -*- coding: utf-8 -*-
"""
Web server for LangGraph Customer Service Agent (Real LLM version).

Flow: user asks -> bot replies -> ... -> user says bye -> satisfaction check -> resolve

Run: python app.py
Visit: http://localhost:7860
"""

import sys
import io
import json
import time
import platform
from uuid import uuid4
from datetime import datetime
from collections import defaultdict

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from langchain_core.messages import HumanMessage, AIMessage
from agent.graph import build_graph
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import os

# ── Rate Limiter (sliding window) ───────────────────────────────
class RateLimiter:
    """Simple in-memory sliding-window rate limiter per IP.

    Configurable via env vars:
      RATE_LIMIT_REQUESTS  鈥?max requests per window (default: 60)
      RATE_LIMIT_WINDOW    鈥?window size in seconds (default: 60)
    """
    def __init__(self, max_requests=60, window_seconds=60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests = defaultdict(list)

    def is_allowed(self, key):
        now = time.time()
        cutoff = now - self.window
        self._requests[key] = [t for t in self._requests[key] if t > cutoff]
        if len(self._requests[key]) >= self.max_requests:
            return False
        self._requests[key].append(now)
        return True

_rate_limiter = RateLimiter(
    max_requests=int(os.environ.get("RATE_LIMIT_REQUESTS", "60")),
    window_seconds=int(os.environ.get("RATE_LIMIT_WINDOW", "60")),
)

# ── Request counters ────────────────────────────────────────────
_request_counter = {"total": 0, "errors": 0}

PORT = 7860

_graph = None


def _check_llm_connectivity() -> bool:
    """Check if the LLM API is reachable.

    Returns True if a successful HTTP response is received from the LLM endpoint.
    """
    import urllib.request as ur
    from agent.llm_client import get_llm_client

    client = get_llm_client()
    # Use /v1/models as a lightweight connectivity probe
    models_url = client.api_url.replace("/chat/completions", "") + "/models"
    try:
        req = ur.Request(
            models_url,
            headers={"Authorization": f"Bearer {client.api_key}"},
            method="GET",
        )
        with ur.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def init():
    """Initialize the agent graph.

    Set USE_SQLITE=1 environment variable for persistent checkpointing.
    """
    global _graph
    use_sqlite = os.environ.get('USE_SQLITE', '0') == '1'
    db_path = os.environ.get('CHECKPOINT_DB', 'checkpoints.db')
    _graph = build_graph(use_sqlite=use_sqlite, db_path=db_path)
    print(f"[Server] Agent initialized (Real LLM via llama.cpp, sqlite={use_sqlite})")

    # LLM connectivity check (non-blocking warning)
    llm_ok = _check_llm_connectivity()
    if llm_ok:
        print("[Server] ✅ LLM API reachable")
    else:
        print("[Server] ⚠️  LLM API unreachable — agent will run in degraded mode")
        print("[Server]    Ensure llama.cpp is running on port 8080")


def stream_llm_reply(messages, system_prompt, max_tokens=384):
    """Stream LLM reply tokens via llama.cpp streaming API.

    Yields individual token strings as they arrive from the LLM.
    Falls back to non-streaming if streaming fails.
    """
    import urllib.request as _ur
    from agent.llm_client import get_llm_client

    client = get_llm_client()
    api_url = client.api_url
    api_key = client.api_key

    payload = {
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
    }

    data = json.dumps(payload).encode("utf-8")
    req = _ur.Request(
        api_url,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )

    try:
        with _ur.urlopen(req, timeout=180) as resp:
            buf = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                lines = buf.split(b"\n")
                buf = lines.pop() or b""
                for line in lines:
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if line_str.startswith("data: "):
                        json_str = line_str[6:]
                        if json_str == "[DONE]":
                            return
                        try:
                            obj = json.loads(json_str)
                            delta = obj.get("choices", [{}])[0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                yield token
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
    except Exception as e:
        print(f"[Streaming 閿欒] {e}")
        # Fallback: non-streaming call
        from agent.nodes import _call_llm
        fallback = _call_llm(messages, system_prompt, max_tokens)
        yield fallback


def run_agent_stream(session_id, user_message):
    """Run the agent and stream tokens via SSE.

    Returns a generator of SSE-formatted strings.
    """
    config = {"configurable": {"thread_id": session_id}}
    human_msg = HumanMessage(content=user_message)

    current_state = _graph.get_state(config)
    existing_count = 0
    if current_state and current_state.values:
        existing_count = len(current_state.values.get('messages', []))

    prev_emotion = 'neutral'
    prev_intensity = 1
    if existing_count > 0 and current_state and current_state.values:
        prev_emotion = current_state.values.get('emotion', 'neutral') or 'neutral'
        prev_intensity = current_state.values.get('emotion_intensity', 1) or 1

    input_data = {
        "messages": [human_msg],
        "session_id": session_id,
        "retry_count": 0,
        "emotion": prev_emotion,
        "emotion_intensity": prev_intensity,
    }

    # Manually orchestrate: identify_intent -> generate_reply (streamed)
    from agent.nodes import identify_intent, _trim_messages
    from agent.agentic_rag import agentic_rag
    from agent.nodes import SYSTEM_PROMPT, RAG_SYSTEM_PROMPT_TEMPLATE
    from agent.memory import build_memory_context as _build_mem_ctx
    from agent.sentiment import get_tone_adjustment as _tone_adj

    # Step 1: Identify intent (non-streaming)
    state = dict(input_data)

    # Send progress event: analyzing
    yield 'data: ' + json.dumps({"progress": "analyzing"}, ensure_ascii=False) + '\n\n'

    intent_result = identify_intent(state)
    state.update(intent_result)

    intent = state.get('intent', 'consult')
    emotion = state.get('emotion', 'neutral')
    intensity = state.get('emotion_intensity', 1)

    # Step 2: Build context for reply (same as generate_reply but we stream)
    messages = state.get('messages', [])
    trimmed = _trim_messages(messages, keep_last=6)
    context_messages = []
    for msg in trimmed:
        if isinstance(msg, HumanMessage):
            context_messages.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            context_messages.append({"role": "assistant", "content": msg.content})

    # RAG context — use Agentic RAG (same as non-streaming path)
    rag_context = ""
    rag_info = None
    if intent == 'consult':
        rag_info = agentic_rag(user_message, max_rounds=2)
        rag_context = rag_info.get('context', '')

    sys_prompt = RAG_SYSTEM_PROMPT_TEMPLATE.format(rag_context=rag_context) if rag_context else SYSTEM_PROMPT

    # Memory context
    if session_id:
        memory_ctx = _build_mem_ctx(session_id)
        if memory_ctx:
            sys_prompt = sys_prompt + memory_ctx

    # Tone adjustment
    tone_adj = _tone_adj(emotion, intensity)
    sys_prompt = sys_prompt + tone_adj

    # Stream tokens
    full_reply = ""
    for token in stream_llm_reply(context_messages, sys_prompt, max_tokens=384):
        full_reply += token
        token_json = json.dumps({"token": token}, ensure_ascii=False)
        yield "data: " + token_json + "\n\n"

    # Save to memory
    if session_id:
        from agent.memory import save_conversation as _save_conv
        _save_conv(
            session_id=session_id,
            user_message=user_message,
            bot_reply=full_reply,
            intent=intent,
            emotion=emotion,
            emotion_intensity=intensity,
        )

    # Add to graph state (write both user + bot messages)
    human_message = HumanMessage(content=user_message)
    ai_message = AIMessage(content=full_reply)
    _graph.update_state(config, {'messages': [human_message, ai_message], 'bot_reply': full_reply})

    # Final metadata event
    meta = {
        "done": True,
        "intent": intent,
        "emotion": emotion,
        "emotion_intensity": intensity,
        "reply_type": _classify_message(full_reply),
        "session_id": session_id,
        "rag_rounds": rag_info.get('rounds', 0) if rag_info else 0,
        "rag_sufficient": rag_info.get('sufficient', False) if rag_info else False,
    }
    yield "data: " + json.dumps(meta, ensure_ascii=False) + "\n\n"


def run_agent(session_id, user_message):
    """Run the agent for a user message."""
    config = {"configurable": {"thread_id": session_id}}
    human_msg = HumanMessage(content=user_message)

    # Get current state to know how many messages already exist
    current_state = _graph.get_state(config)
    existing_count = 0
    if current_state and current_state.values:
        existing_count = len(current_state.values.get('messages', []))

    # Restore emotion state from previous turn if exists
    prev_emotion = 'neutral'
    prev_intensity = 1
    if existing_count > 0 and current_state and current_state.values:
        prev_emotion = current_state.values.get('emotion', 'neutral') or 'neutral'
        prev_intensity = current_state.values.get('emotion_intensity', 1) or 1

    input_data = {
        "messages": [human_msg],
        "session_id": session_id,
        "retry_count": 0,
        "emotion": prev_emotion,
        "emotion_intensity": prev_intensity,
    }

    all_new_messages = []
    interrupted = False

    try:
        for event in _graph.stream(input_data, config=config, stream_mode="values"):
            if event and event.get('messages'):
                new_msgs = event['messages'][existing_count:]
                all_new_messages.extend(new_msgs)
    except Exception as e:
        if "interrupt" in str(e).lower():
            interrupted = True

    # Get final state
    state = _graph.get_state(config)
    intent = 'unknown'
    retry_count = 0

    if state and state.values:
        intent = state.values.get('intent', 'unknown') or 'unknown'
        retry_count = state.values.get('retry_count', 0)
        emotion = state.values.get('emotion', 'neutral') or 'neutral'
        emotion_intensity = state.values.get('emotion_intensity', 1) or 1

    # Extract replies
    replies = []
    for msg in all_new_messages:
        if isinstance(msg, AIMessage):
            content = msg.content
            msg_type = _classify_message(content)
            replies.append({"type": msg_type, "content": content})

    next_action = "Active"
    if interrupted:
        next_action = "Escalated"

    return {
        "replies": replies,
        "interrupted": interrupted,
        "intent": intent,
        "retry_count": retry_count,
        "emotion": emotion,
        "emotion_intensity": emotion_intensity,
        "next_action": next_action,
        "session_id": session_id
    }


def _classify_message(content):
    """Classify bot message for UI styling."""
    if any(kw in content for kw in ["满意", "satisfied", "satisfy", "rate this"]):
        return "satisfaction"
    elif any(kw in content for kw in ["再见", "goodbye", "thank you for contacting", "祝您", "祝您生活愉快", "欢迎回来"]):
        return "closing"
    else:
        return "reply"


# ============================================================
# HTML UI (English)
# ============================================================


CHAT_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>智能客服</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif;
  background:#f0f2f5;
  display:flex;align-items:center;justify-content:center;
  -webkit-font-smoothing:antialiased;
}
.chat-window{
  width:400px;height:600px;background:#fff;border-radius:16px;
  box-shadow:0 12px 40px rgba(0,0,0,0.15);
  display:flex;flex-direction:column;overflow:hidden;
}
.c-header{
  background:linear-gradient(135deg,#5b5fc7,#7c3aed);color:#fff;
  padding:16px 18px;display:flex;align-items:center;gap:12px;flex-shrink:0;
}
.c-header .av{
  width:36px;height:36px;background:rgba(255,255,255,0.2);border-radius:50%;
  display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;
}
.c-header .info{flex:1;min-width:0}
.c-header .name{font-size:15px;font-weight:600}
.c-header .st{font-size:12px;opacity:0.85;display:flex;align-items:center;gap:5px;margin-top:2px}
.c-header .st::before{content:'';width:7px;height:7px;background:#4ade80;border-radius:50%;display:inline-block;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.6;transform:scale(0.85)}}
.c-header .act{display:flex;gap:4px}
.c-header .act button{
  background:rgba(255,255,255,0.15);border:none;color:#fff;
  width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:13px;
  display:flex;align-items:center;justify-content:center;transition:background 0.15s;
}
.c-header .act button:hover{background:rgba(255,255,255,0.25)}
.c-chat{
  flex:1;overflow-y:auto;padding:16px;
  display:flex;flex-direction:column;gap:12px;background:#f8f9fb;
}
.c-chat::-webkit-scrollbar{width:4px}
.c-chat::-webkit-scrollbar-track{background:transparent}
.c-chat::-webkit-scrollbar-thumb{background:#d1d5db;border-radius:2px}
.msg{display:flex;gap:8px;max-width:85%;animation:fadeUp 0.2s ease}
@keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.msg.user{flex-direction:row-reverse;align-self:flex-end}
.msg.bot{align-self:flex-start}
.msg .av{
  width:28px;height:28px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;margin-top:2px;
}
.msg.user .av{background:#5b5fc7;color:#fff}
.msg.bot .av{background:#e8e5ff;color:#5b5fc7}
.msg .body{min-width:0;display:flex;flex-direction:column;gap:2px}
.msg .bub{
  padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.6;
  white-space:pre-wrap;word-break:break-word;
}
.msg .bub strong,.msg .bub b{font-weight:700;color:#1a1a2e}
[data-theme=dark] .msg .bub strong,[data-theme=dark] .msg .bub b{color:#e0e0e0}
.msg .bub code{
  background:#f3f4f6;border:1px solid #e5e7eb;border-radius:4px;
  padding:1px 5px;font-size:12.5px;font-family:'SF Mono','Consolas',monospace;
  color:#c7254e;
}
[data-theme=dark] .msg .bub code{background:#2a2a4a;border-color:#3a3a5a;color:#f0abfc}
.msg .bub ul,.msg .bub ol{margin:6px 0 4px 16px;padding:0}
.msg .bub li{margin-bottom:3px}
.msg .bub h1,.msg .bub h2,.msg .bub h3{
  font-size:14px;font-weight:700;margin:8px 0 4px;color:#1a1a2e;
}
[data-theme=dark] .msg .bub h1,[data-theme=dark] .msg .bub h2,
[data-theme=dark] .msg .bub h3{color:#e0e0e0}
.msg .bub pre{
  background:#f8f9fb;border:1px solid #e5e7eb;border-radius:6px;
  padding:8px 10px;margin:6px 0;font-size:12.5px;overflow-x:auto;
  font-family:'SF Mono','Consolas',monospace;line-height:1.4;
}
[data-theme=dark] .msg .bub pre{background:#1e1e3a;border-color:#333355}
.msg.user .bub{background:#5b5fc7;color:#fff;border-bottom-right-radius:4px}
.msg.bot .bub{background:#fff;color:#1a1a2e;border:1px solid #e5e7eb;border-bottom-left-radius:4px}
.msg.bot .bub.satisfaction{border-color:#f59e0b;background:#fffbeb}
.msg.bot .bub.closing{border-color:#10b981;background:#ecfdf5}
.msg .meta{display:flex;gap:6px;align-items:center;padding:0 4px;font-size:11px;color:#9ca3af}
.msg.user .meta{justify-content:flex-end}
.msg .meta .cp{background:none;border:none;cursor:pointer;color:#9ca3af;font-size:11px;padding:0 2px;opacity:0;transition:opacity 0.15s}
.msg:hover .meta .cp{opacity:1}
.msg .meta .cp:hover{color:#5b5fc7}
.msg .reactions{display:flex;gap:2px;margin-top:4px;padding-left:4px}
.msg .reactions button{
  background:none;border:none;font-size:13px;padding:2px 6px;
  cursor:pointer;border-radius:6px;opacity:0;transition:all 0.15s;
}
.msg:hover .reactions button{opacity:0.7}
.msg .reactions button:hover{opacity:1;background:#f3f4f6}
[data-theme=dark] .msg .reactions button:hover{background:#2a2a4a}
.msg .reactions button.active{opacity:1}
.msg .reactions button.active::after{content:'';display:inline-block;margin-left:2px;font-size:9px;color:#5b5fc7}
.sys{align-self:center;font-size:11px;color:#9ca3af;padding:3px 10px;background:#fff;border:1px solid #e5e7eb;border-radius:10px}
.qr{display:flex;gap:5px;flex-wrap:wrap;align-self:flex-start;max-width:85%}
.qr button{
  padding:5px 12px;border:1px solid #e5e7eb;border-radius:14px;
  font-size:12px;background:#fff;color:#6b7280;cursor:pointer;transition:all 0.12s;
}
.qr button:hover{border-color:#5b5fc7;color:#5b5fc7;background:#f5f3ff}
.typing{display:flex;gap:4px;padding:12px 14px;align-items:center}
.typing span{width:6px;height:6px;background:#d1d5db;border-radius:50%;animation:bounce 1.4s infinite}
.typing span:nth-child(2){animation-delay:0.2s}
.typing span:nth-child(3){animation-delay:0.4s}
@keyframes bounce{0%,60%,100%{transform:translateY(0);opacity:0.4}30%{transform:translateY(-5px);opacity:1}}
.cur::after{content:'\2588';animation:blink 0.8s infinite;color:#5b5fc7;font-size:13px}
@keyframes blink{0%,50%{opacity:1}51%,100%{opacity:0}}
.stars{display:flex;gap:1px;align-items:center;margin-top:4px}
.stars .lbl{font-size:11px;color:#9ca3af;margin-right:4px}
.stars button{
  background:none;border:none;cursor:pointer;font-size:14px;padding:1px 2px;
  filter:grayscale(1) opacity(0.25);transition:filter 0.12s,transform 0.12s;
}
.stars button:hover{filter:grayscale(0) opacity(1);transform:scale(1.2)}
.stars button.on{filter:grayscale(0) opacity(1)}
.stars .thx{font-size:12px;color:#10b981;margin-top:2px}
.c-input{
  padding:12px 16px;border-top:1px solid #e5e7eb;
  display:flex;gap:8px;align-items:center;background:#fff;flex-shrink:0;
}
.c-input input{
  flex:1;padding:9px 14px;border:1px solid #e5e7eb;border-radius:10px;
  font-size:14px;outline:none;background:#f8f9fb;transition:border-color 0.15s;
}
.c-input input:focus{border-color:#5b5fc7;background:#fff}
.c-input input::placeholder{color:#9ca3af}
.char-count{
  font-size:10px;color:#b0b0b0;white-space:nowrap;padding-right:4px;
  user-select:none;flex-shrink:0;
}
.char-count.warn{color:#ef4444}
.c-input .send{
  width:36px;height:36px;background:#5b5fc7;border:none;border-radius:10px;
  color:#fff;cursor:pointer;font-size:15px;
  display:flex;align-items:center;justify-content:center;transition:opacity 0.15s;flex-shrink:0;
}
.c-input .send:hover{opacity:0.85}
.c-input .send:disabled{opacity:0.4;cursor:not-allowed}
.c-info{
  padding:4px 16px;border-top:1px solid #f0f0f0;
  font-size:10px;color:#b0b0b0;display:flex;gap:10px;
  background:#fff;flex-shrink:0;overflow-x:auto;scrollbar-width:none;
}
.c-info::-webkit-scrollbar{display:none}
.c-info .iv{display:flex;align-items:center;gap:3px;white-space:nowrap}
.c-info .ik{font-weight:500}
.c-info .v{font-family:'SF Mono','Consolas',monospace;color:#888}
.em-bar{display:inline-flex;gap:1px;vertical-align:middle}
.em-bar i{width:4px;height:8px;border-radius:1px;background:#e5e7eb;font-style:normal}
.em-bar i.on{background:#5b5fc7}
.em-bar i.hi{background:#ef4444}
.floating-controls{position:fixed;top:16px;right:16px;display:flex;gap:6px;z-index:50}
.fab{
  padding:6px 14px;border-radius:20px;border:1px solid #e5e7eb;
  background:#fff;color:#6b7280;font-size:12px;cursor:pointer;
  box-shadow:0 2px 8px rgba(0,0,0,0.08);transition:all 0.15s;
}
.fab:hover{border-color:#5b5fc7;color:#5b5fc7}
.fab.active{background:#5b5fc7;color:#fff;border-color:#5b5fc7}
.test-floating{
  position:fixed;bottom:16px;left:16px;right:430px;
  display:flex;gap:4px;flex-wrap:wrap;align-items:center;z-index:50;
}
.test-floating .lbl{font-size:11px;color:#9ca3af;margin-right:2px}
.tc{
  padding:4px 10px;border:1px solid #e5e7eb;border-radius:12px;
  font-size:11px;background:#fff;color:#6b7280;cursor:pointer;transition:all 0.12s;
}
.tc:hover{border-color:#5b5fc7;color:#5b5fc7}
.tc.r{border-color:#fca5a5;color:#dc2626}
.tc.r:hover{background:#fef2f2}
.tc.g{border-color:#86efac;color:#16a34a}
.tc.g:hover{background:#f0fdf4}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:100;align-items:center;justify-content:center}
.modal-bg.show{display:flex}
.modal-box{
  background:#fff;border-radius:12px;padding:20px;
  width:92%;max-width:500px;max-height:70vh;overflow-y:auto;
  box-shadow:0 8px 32px rgba(0,0,0,0.2);
}
.modal-box h3{font-size:15px;margin-bottom:12px;color:#1a1a2e}
.modal-box pre{
  background:#f8f9fb;border:1px solid #e5e7eb;padding:12px;border-radius:8px;
  font-size:12px;font-family:'SF Mono','Consolas',monospace;
  overflow-x:auto;max-height:40vh;white-space:pre-wrap;word-break:break-all;
}
.modal-btns{display:flex;gap:6px;margin-top:12px;justify-content:flex-end}
.modal-btns button{padding:6px 14px;border-radius:8px;border:1px solid #e5e7eb;cursor:pointer;background:#fff;color:#6b7280;font-size:12px}
.modal-btns button.pri{background:#5b5fc7;color:#fff;border-color:#5b5fc7}
[data-theme=dark] body{background:#111}
[data-theme=dark] .chat-window{background:#1a1a2e;box-shadow:0 12px 40px rgba(0,0,0,0.4)}
[data-theme=dark] .c-chat{background:#16162a}
[data-theme=dark] .msg.bot .bub{background:#222240;color:#e0e0e0;border-color:#333355}
[data-theme=dark] .msg.bot .bub.satisfaction{background:#2a2200;border-color:#b45309;color:#fbbf24}
[data-theme=dark] .msg.bot .bub.closing{background:#0a2a13;border-color:#059669;color:#34d399}
[data-theme=dark] .sys{background:#222240;border-color:#333355;color:#888}
[data-theme=dark] .qr button{background:#222240;border-color:#333355;color:#aaa}
[data-theme=dark] .qr button:hover{border-color:#7c3aed;color:#a78bfa;background:#1a1a3e}
[data-theme=dark] .c-input{background:#1a1a2e;border-color:#333355}
[data-theme=dark] .c-input input{background:#222240;border-color:#333355;color:#e0e0e0}
[data-theme=dark] .c-input input:focus{border-color:#7c3aed;background:#1a1a3e}
[data-theme=dark] .c-info{background:#1a1a2e;border-color:#333355;color:#666}
[data-theme=dark] .fab{background:#222240;border-color:#333355;color:#aaa}
[data-theme=dark] .fab:hover{border-color:#7c3aed;color:#a78bfa}
[data-theme=dark] .fab.active{background:#7c3aed;color:#fff;border-color:#7c3aed}
[data-theme=dark] .tc{background:#222240;border-color:#333355;color:#aaa}
[data-theme=dark] .tc:hover{border-color:#7c3aed;color:#a78bfa}
[data-theme=dark] .tc.r{border-color:#7f1d1d;color:#fca5a5}
[data-theme=dark] .tc.g{border-color:#14532d;color:#86efac}
[data-theme=dark] .modal-box{background:#1a1a2e}
[data-theme=dark] .modal-box h3{color:#e0e0e0}
[data-theme=dark] .modal-box pre{background:#222240;border-color:#333355;color:#e0e0e0}
[data-theme=dark] .modal-btns button{background:#222240;border-color:#333355;color:#aaa}
[data-theme=dark] .modal-btns button.pri{background:#7c3aed;border-color:#7c3aed;color:#fff}
[data-theme=dark] .char-count{color:#666}
[data-theme=dark] .char-count.warn{color:#f87171}
.sess-search{
  padding:6px 12px;border-radius:20px;border:1px solid #e5e7eb;
  background:#fff;color:#6b7280;font-size:12px;width:160px;
  box-shadow:0 2px 8px rgba(0,0,0,0.08);outline:none;transition:all 0.15s;
}
.sess-search:focus{border-color:#5b5fc7;width:180px}
[data-theme=dark] .sess-search{background:#222240;border-color:#333355;color:#aaa}
[data-theme=dark] .scroll-bottom{
  position:fixed;bottom:120px;right:20px;width:36px;height:36px;
  border-radius:50%;background:#5b5fc7;color:#fff;border:none;
  cursor:pointer;font-size:16px;display:none;align-items:center;
  justify-content:center;box-shadow:0 2px 8px rgba(91,95,199,0.4);
  transition:opacity 0.2s;z-index:50;
}
.scroll-bottom:hover{background:#7c3aed}
.scroll-bottom.show{display:flex}
[data-theme=dark] .scroll-bottom{background:#7c3aed;box-shadow:0 2px 8px rgba(124,58,237,0.4)}
[data-theme=dark] .scroll-bottom:hover{background:#6d28d9}
.sess-search:focus{border-color:#7c3aed}
@media(max-width:480px){
  .chat-window{width:100%;height:100%;border-radius:0}
  .test-floating{left:8px;right:8px;bottom:8px}
  .floating-controls{top:8px;right:8px}
}
</style>
</head>
<body>
<div class="floating-controls">
  <button class="fab active" id="themeBtn" onclick="toggleTheme()">🌙 暗色</button>
  <button class="fab" onclick="toggleInfo()">📊 信息</button>
  <button class="fab" onclick="exportSession()">📋 导出</button>
  <button class="fab" onclick="reloadKB()">🔄 知识库</button>
  <button class="fab" onclick="copyAllConversation()">📄 复制全部</button>
  <button class="fab" onclick="window.open('/analytics','_blank')">📊 数据面板</button>
  <button class="fab" onclick="resetAll()">🗑 重置</button>
</div>
<div class="floating-controls" style="top:auto;bottom:60px">
  <input type="text" class="sess-search" id="sessSearch" placeholder="🔍 搜索会话..." oninput="searchSessions(this.value)" />
</div>
<button class="scroll-bottom" id="scrollBottom" onclick="scrollToBottom()">↓</button>
<div class="test-floating">
  <span class="lbl">测试</span>
  <button class="tc" onclick="qt('产品怎么用？')">产品咨询</button>
  <button class="tc r" onclick="qt('我要投诉，产品质量太差了')">投诉</button>
  <button class="tc" onclick="qt('你好')">打招呼</button>
  <button class="tc g" onclick="qt('谢谢，没问题了')">感谢并结束</button>
  <button class="tc r" onclick="qt('再见')">说再见</button>
  <button class="tc g" onclick="qt('满意')">满意</button>
  <button class="tc r" onclick="qt('不满意')">不满意</button>
  <button class="tc" onclick="runFull()">▶ 自动流程</button>
</div>
<div class="chat-window">
  <div class="c-header">
    <div class="av">🤖</div>
    <div class="info">
      <div class="name">智能客服助手</div>
      <div class="st">在线 · 通常秒回</div>
    </div>
    <div class="act">
      <button onclick="newSess()" title="新会话">＋</button>
      <button onclick="clearChat()" title="清空">🗑</button>
    </div>
  </div>
  <div class="c-chat" id="chat"></div>
  <div class="c-info" id="infoBar" style="display:none">
    <div class="iv"><span class="ik">会话</span> <span class="v" id="iSess">-</span></div>
    <div class="iv"><span class="ik">意图</span> <span class="v" id="iIntent">-</span></div>
    <div class="iv"><span class="ik">情绪</span> <span id="iEmo">-</span> <span id="emBar" class="em-bar"></span></div>
    <div class="iv"><span class="ik">消息</span> <span class="v" id="iMsg">0</span></div>
  </div>
  <div class="c-input">
    <input type="text" id="inp" placeholder="按 Enter 发送 · Ctrl+Enter 换行" autocomplete="off" maxlength="4000" />
    <span class="char-count" id="charCount">0/4000</span>
    <button class="send" id="sbtn" onclick="sendMsg()">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
    </button>
  </div>
</div>
<div class="modal-bg" id="modal">
  <div class="modal-box">
    <h3>📋 会话导出</h3>
    <pre id="mContent">加载中...</pre>
    <div class="modal-btns">
      <button onclick="cpExport()">复制</button>
      <button onclick="dlExport()">下载 JSON</button>
      <button class="pri" onclick="closeModal()">关闭</button>
    </div>
  </div>
</div>
<script>
let sess=null,busy=false,cnt=0,bidx=0,showI=false;
const QR={def:['产品怎么用？','我要投诉','价格是多少？','有保修吗？'],reply:['能详细说说吗？','还有其他问题','谢谢，没问题了'],sat:['满意','不满意']};
function toggleTheme(){const h=document.documentElement,b=document.getElementById('themeBtn');if(h.getAttribute('data-theme')==='dark'){h.removeAttribute('data-theme');b.textContent='🌙 暗色';localStorage.setItem('th','light')}else{h.setAttribute('data-theme','dark');b.textContent='☀️ 亮色';localStorage.setItem('th','dark')}}
if(localStorage.getItem('th')==='dark'){document.documentElement.setAttribute('data-theme','dark');setTimeout(()=>{const b=document.getElementById('themeBtn');if(b)b.textContent='☀️ 亮色'},0)}
function toggleInfo(){showI=!showI;document.getElementById('infoBar').style.display=showI?'flex':'none'}
const chat=document.getElementById('chat'),inp=document.getElementById('inp'),sbtn=document.getElementById('sbtn');
inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.ctrlKey&&!busy)sendMsg();else if(e.key==='Enter'&&e.ctrlKey){e.preventDefault();inp.value+='
';updateCharCount()}});
// ── Character Counter ───────────────────────────────────────
function updateCharCount(){const c=document.getElementById('charCount');if(!c)return;const n=inp.value.length;c.textContent=n+'/4000';c.classList.toggle('warn',n>3500)}
inp.addEventListener('input',updateCharCount);
// ── Lightweight Markdown Renderer ───────────────────────────────
function md(text){
  if(!text)return text;
  // Escape HTML first
  let s=text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // Code blocks (```...```) — must be before inline code
  s=s.replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>');
  // Inline code (`...`)
  s=s.replace(/`([^`]+)`/g,'<code>$1</code>');
  // Bold (**...** or __...__)
  s=s.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/__(.+?)__/g,'<strong>$1</strong>');
  // Italic (*...* or _..._)
  s=s.replace(/\*(.+?)\*/g,'<em>$1</em>');
  s=s.replace(/(?<!\w)_(.+?)_(?!\w)/g,'<em>$1</em>');
  // Headers (# ## ###)
  s=s.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  s=s.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  s=s.replace(/^# (.+)$/gm,'<h1>$1</h1>');
  // Unordered lists (- or * at line start)
  s=s.replace(/^(?:[-*])\s+(.+)$/gm,'<li>$1</li>');
  s=s.replace(/((?:<li>.*<\/li>\n?)+)/g,'<ul>$1</ul>');
  // Horizontal rule
  s=s.replace(/^---$/gm,'<hr>');
  // Line breaks (double newline → <br><br>, single → keep as is for pre-wrap)
  s=s.replace(/\n\n/g,'<br><br>');
  return s;
}
let msgUid=0;
function addMsg(role,text,type,anim){cnt++;msgUid++;if(showI)document.getElementById('iMsg').textContent=cnt;const row=document.createElement('div');row.className='msg '+role;if(role==='bot')row.id='bot-'+msgUid;const av=document.createElement('div');av.className='av';av.textContent=role==='user'?'你':'🤖';const body=document.createElement('div');body.className='body';const bub=document.createElement('div');bub.className='bub '+(type||'');const meta=document.createElement('div');meta.className='meta';const ts=document.createElement('span');ts.textContent=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});meta.appendChild(ts);if(role==='bot'&&text){const rt=document.createElement('span');rt.textContent=/[\u4e00-\u9fff]/.test(text)?Math.ceil(text.length/5)+'秒':Math.ceil(text.split(/\s+/).filter(w=>w).length/2.7)+'s';meta.appendChild(rt);const cp=document.createElement('button');cp.className='cp';cp.textContent='复制';cp.onclick=()=>{navigator.clipboard.writeText(bub.dataset.raw||bub.textContent).then(()=>{cp.textContent='✓';setTimeout(()=>cp.textContent='复制',1500)})};meta.appendChild(cp)}body.appendChild(bub);body.appendChild(meta);row.appendChild(av);row.appendChild(body);chat.appendChild(row);chat.scrollTop=chat.scrollHeight;rmQR();if(anim&&role==='bot'){const spd=/[\u4e00-\u9fff]/.test(text)?35:20;bub.classList.add('cur');bub.dataset.raw=text;let i=0;(function t(){if(i<text.length){bub.innerHTML=md(text.substring(0,i+1));i++;chat.scrollTop=chat.scrollHeight;setTimeout(t,spd)}else bub.classList.remove('cur')})()}else{if(role==='bot'){bub.dataset.raw=text;bub.innerHTML=md(text)}else bub.textContent=text}if(role==='bot'&&type!=='satisfaction'&&type!=='closing'){bidx++;addStars(body,bidx);addReactions(body,'bot-'+msgUid)}return row}
// ── Emoji Reactions on Bot Messages ───────────────────────────────
function addReactions(parent,msgId){const d=document.createElement('div');d.className='reactions';const emojis=['👍','👎','💡'];emojis.forEach(e=>{const b=document.createElement('button');b.textContent=e;b.title=e==='👍'?'有帮助':e==='👎'?'没帮助':'有启发';b.onclick=()=>{b.classList.toggle('active');fetch('/api/reaction',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sess,message_id:msgId,emoji:e,active:b.classList.contains('active')})}).catch(()=>{})};d.appendChild(b)});parent.appendChild(d)}
function addSys(t){const d=document.createElement('div');d.className='sys';d.textContent=t;chat.appendChild(d);chat.scrollTop=chat.scrollHeight}
function addTyping(){const row=document.createElement('div');row.className='msg bot';row.id='typing';row.innerHTML='<div class="av">🤖</div><div class="body"><div class="bub"><div class="typing"><span></span><span></span><span></span></div></div></div>';chat.appendChild(row);chat.scrollTop=chat.scrollHeight}
function rmTyping(){const e=document.getElementById('typing');if(e)e.remove()}
function showQR(replies){rmQR();const c=document.createElement('div');c.className='qr';c.id='qr';for(const t of replies){const b=document.createElement('button');b.textContent=t;b.onclick=()=>qt(t);c.appendChild(b)}chat.appendChild(c);chat.scrollTop=chat.scrollHeight}
function rmQR(){const e=document.getElementById('qr');if(e)e.remove()}
function ctxQR(lt){if(lt==='satisfaction')return QR.sat;if(lt==='closing')return[];if(lt==='reply')return QR.reply;return QR.def}
function addStars(parent,idx){const d=document.createElement('div');d.className='stars';const l=document.createElement('span');l.className='lbl';l.textContent='有帮助？';d.appendChild(l);for(let i=1;i<=5;i++){const b=document.createElement('button');b.textContent='⭐';b.title=i+'星';b.onclick=()=>rate(idx,i,d);b.onmouseenter=()=>d.querySelectorAll('button').forEach((x,j)=>{if(x.classList.contains('thx'))return;x.style.filter=j<i?'grayscale(0) opacity(1)':'grayscale(1) opacity(0.25)'});b.onmouseleave=()=>{if(!d.querySelector('.on'))d.querySelectorAll('button').forEach(x=>{if(x.classList.contains('thx'))return;x.style.filter='grayscale(1) opacity(0.25)'})};d.appendChild(b)}parent.appendChild(d)}
function rate(idx,stars,d){d.querySelectorAll('button').forEach((b,j)=>{if(j<stars&&!b.classList.contains('thx'))b.classList.add('on');b.onclick=null;b.style.cursor='default'});const l=d.querySelector('.lbl');if(l)l.remove();const th=document.createElement('div');th.className='thx';th.textContent='感谢评价！'+stars+'⭐';d.replaceWith(th);if(sess)fetch('/api/rating',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sess,message_index:idx,stars})}).catch(()=>{})}
async function sendMsg(text){if(busy)return;const msg=text||inp.value.trim();if(!msg)return;inp.value='';addMsg('user',msg);busy=true;sbtn.disabled=true;addTyping();try{const s=sess||crypto.randomUUID();if(!sess){sess=s;if(showI)document.getElementById('iSess').textContent=s.slice(0,8)+'...'}const resp=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,session_id:s,stream:true})});rmTyping();if(resp.headers.get('content-type')?.includes('text/event-stream')){await handleStream(resp)}else{const data=await resp.json();if(data.error){addMsg('bot','Error: '+data.error,'',false)}else{let lt='';for(const r of data.replies){const m={satisfaction:'satisfaction',closing:'closing'};lt=m[r.type]||'reply';addMsg('bot',r.content,lt,true)}const s2=ctxQR(lt);if(s2.length)setTimeout(()=>showQR(s2),800);updInfo(data)}}}catch(e){rmTyping();addMsg('bot','连接错误: '+e.message,'')}busy=false;sbtn.disabled=false;inp.focus()}
async function handleStream(resp){const reader=resp.body.getReader(),dec=new TextDecoder();let buf='',full='',lt='reply',meta=null;const div=addMsg('bot','', 'reply',false);const bub=div.querySelector('.bub');bub.classList.add('cur');const deadline=Date.now()+180000;let finished=false;try{while(Date.now()<deadline&&!finished){const{done,value}=await reader.read();if(done){finished;break}buf+=dec.decode(value,{stream:true});const lines=buf.split('\n');buf=lines.pop()||'';for(const ln of lines){if(!ln.startsWith('data: '))continue;try{const d=JSON.parse(ln.slice(6));if(d.done){meta=d;bub.classList.remove('cur');finished=true;break}else if(d.progress==='analyzing')bub.textContent='🤔 分析中...';else if(d.token!==undefined){full+=d.token;bub.dataset.raw=full;bub.innerHTML=md(full);chat.scrollTop=chat.scrollHeight}}catch(e){}}}if(!meta){bub.classList.remove('cur');lt='reply';updInfo({})}reader.cancel().catch(()=>{})}catch(e){bub.classList.remove('cur')}if(meta){lt=meta.reply_type||'reply';bub.className='bub '+lt;updInfo(meta);const s=ctxQR(lt);if(s.length)setTimeout(()=>showQR(s),800)}}
function updInfo(d){if(!showI)return;if(d.intent)document.getElementById('iIntent').textContent=d.intent;if(d.emotion){const em={neutral:'😐',angry:'😠',sad:'😢',anxious:'😰',happy:'😊'};document.getElementById('iEmo').textContent=(em[d.emotion]||'😐')+' '+d.emotion+(d.emotion_intensity?'('+d.emotion_intensity+'/5)':'');updEmBar(d.emotion,d.emotion_intensity)}document.getElementById('iMsg').textContent=cnt}
function updEmBar(em,intensity){const bar=document.getElementById('emBar');if(!bar||!intensity){bar.innerHTML='';return}let h='';for(let i=1;i<=5;i++)h+='<i class="'+(i<=intensity?(intensity>=4?'on hi':'on'):'')+'"></i>';bar.innerHTML=h}
function newSess(){sess=crypto.randomUUID();if(showI)document.getElementById('iSess').textContent=sess.slice(0,8)+'...';chat.innerHTML='';cnt=0;bidx=0;addSys('新会话已启动');setTimeout(()=>{addMsg('bot','👋 您好！我是智能客服助手。\n\n可以帮您：\n• 📦 产品咨询与使用指导\n• 🔧 故障排查与技术支持\n• 💰 价格与保修政策\n• 📞 投诉与建议\n\n请问有什么可以帮您的？','reply',true);setTimeout(()=>showQR(['产品怎么用？','价格是多少？','我要投诉','有保修吗？']),1000)},300)}
function clearChat(){chat.innerHTML='';cnt=0;bidx=0}
function resetAll(){sess=null;cnt=0;bidx=0;chat.innerHTML='';addSys('已重置')}
function qt(t){inp.value=t;sendMsg(t)}
async function exportSession(){if(!sess){addSys('请先开始会话');return}document.getElementById('mContent').textContent='加载中...';document.getElementById('modal').classList.add('show');try{const r=await fetch('/api/export/'+sess);const d=await r.json();if(d.error)document.getElementById('mContent').textContent='错误: '+d.error;else{window._exp=d;document.getElementById('mContent').textContent=JSON.stringify(d,null,2)}}catch(e){document.getElementById('mContent').textContent='网络错误: '+e.message}}
function closeModal(){document.getElementById('modal').classList.remove('show')}
function cpExport(){navigator.clipboard.writeText(document.getElementById('mContent').textContent).then(()=>addSys('已复制')).catch(()=>addSys('复制失败'))}
function dlExport(){if(!window._exp)return;const b=new Blob([JSON.stringify(window._exp,null,2)],{type:'application/json'});const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download='session-'+(window._exp.session_id||'export')+'.json';a.click();URL.revokeObjectURL(u)}
// ── Copy All Conversation ────────────────────────────────────────
function copyAllConversation(){const msgs=chat.querySelectorAll('.msg');if(!msgs.length){addSys('暂无对话可复制');return}let text='=== 智能客服会话记录 ===\n';text+='会话ID: '+(sess?''+sess:'未设置')+'\n';text+='时间: '+new Date().toLocaleString('zh-CN')+'\n';text+='消息数: '+cnt+'\n\n---\n';msgs.forEach(m=>{const role=m.classList.contains('user')?'你':'客服';const bub=m.querySelector('.bub');if(bub){const raw=bub.dataset.raw||bub.textContent;text+=`[${new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}] ${role}: ${raw}\n\n`}});text+='---\n结束';navigator.clipboard.writeText(text).then(()=>{addSys('✅ 全部对话已复制到剪贴板 ('+cnt+'条消息)')}).catch(()=>{addSys('❌ 复制失败，请手动选择复制')}}
async function reloadKB(){try{const r=await fetch('/api/rag/reload');const d=await r.json();if(d.error)addSys('重载失败: '+d.error);else addSys('✅ 知识库已重载: '+d.documents+' 文档, '+d.sections+' 章节')}catch(e){addSys('网络错误: '+e.message)}}
// ── Session Search ─────────────────────────────────────────────
let _searchTimer=null;
async function searchSessions(q){if(_searchTimer)clearTimeout(_searchTimer);_searchTimer=setTimeout(async()=>{q=q.trim();if(!q){addSys('会话搜索已取消');return}try{const r=await fetch('/api/sessions?search='+encodeURIComponent(q));const d=await r.json();if(d.error){addSys('搜索失败: '+d.error);return}if(!d.sessions.length){addSys('未找到匹配 "'+q+'" 的会话');return}let info='找到 '+d.sessions.length+' 个匹配会话:\n';d.sessions.slice(0,5).forEach(s=>{info+='• ['+s.session_id.slice(0,8)+'] '+s.message_count+'条 · '+s.preview+'\n'});addSys(info)}catch(e){addSys('网络错误: '+e.message)}},300)}
async function runFull(){clearChat();newSess();await new Promise(r=>setTimeout(r,500));const steps=[{msg:'产品怎么用？',label:'步骤1：咨询'},{msg:'谢谢，没问题了',label:'步骤2：结束'},{msg:'满意',label:'步骤3：反馈'}];for(const s of steps){addSys(s.label);await new Promise(r=>setTimeout(r,500));inp.value=s.msg;await sendMsg(s.msg);while(busy)await new Promise(r=>setTimeout(r,200));await new Promise(r=>setTimeout(r,1500))}addSys('演示完成！')}
// ── Scroll to Bottom Button ───────────────────────────────────────
function scrollToBottom(){chat.scrollTo({top:chat.scrollHeight,behavior:'smooth'})}
function checkScrollPosition(){const sb=document.getElementById('scrollBottom');if(!sb)return;const distFromBottom=chat.scrollHeight-chat.scrollTop-chat.clientHeight;if(distFromBottom>200)sb.classList.add('show');else sb.classList.remove('show')}
window.addEventListener('DOMContentLoaded',()=>{if(!sess)newSess();chat.addEventListener('scroll',checkScrollPosition,{passive:true})});
</script>
</body>
</html>"""

# ============================================================
# Analytics Dashboard HTML
# ============================================================

ANALYTICS_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>客服数据分析面板</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif;background:#f0f2f5;color:#1a1a2e;padding:24px}
h1{font-size:22px;margin-bottom:20px;display:flex;align-items:center;gap:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}
.card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,0.08)}
.card h3{font-size:13px;color:#6b7280;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px}
.card .val{font-size:28px;font-weight:700}
.card .sub{font-size:12px;color:#9ca3af;margin-top:4px}
.chart-section{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}
.chart-card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,0.08)}
.chart-card h3{font-size:14px;color:#6b7280;margin-bottom:16px}
.bar-row{display:flex;align-items:center;margin-bottom:8px;gap:8px}
.bar-label{width:80px;font-size:12px;text-align:right;flex-shrink:0}
.bar-track{flex:1;height:24px;background:#f3f4f6;border-radius:6px;overflow:hidden;position:relative}
.bar-fill{height:100%;border-radius:6px;display:flex;align-items:center;justify-content:flex-end;padding-right:6px;font-size:11px;color:#fff;font-weight:600;transition:width 0.5s ease}
.bar-count{font-size:12px;color:#6b7280;width:36px;text-align:right;flex-shrink:0}
.stars-visual{display:flex;gap:4px;margin-top:8px}
.stars-visual .star{font-size:20px}
.timeline-table{width:100%;border-collapse:collapse;font-size:13px}
.timeline-table th{text-align:left;padding:8px;border-bottom:2px solid #e5e7eb;color:#6b7280;font-weight:600}
.timeline-table td{padding:8px;border-bottom:1px solid #f3f4f6}
.timeline-table tr:hover td{background:#f9fafb}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.badge-consult{background:#dbeafe;color:#1d4ed8}
.badge-complaint{background:#fee2e2;color:#dc2626}
.badge-chat{background:#d1fae5;color:#059669}
.badge-ending{background:#fef3c7;color:#d97706}
.back-link{display:inline-flex;align-items:center;gap:4px;color:#5b5fc7;text-decoration:none;font-size:13px;margin-bottom:16px;padding:6px 12px;border-radius:8px;background:#f5f3ff}
.back-link:hover{background:#ede9fe}
.auto-refresh{display:flex;align-items:center;gap:8px;font-size:12px;color:#6b7280;margin-left:auto}
.auto-refresh input{accent-color:#5b5fc7}
@media(max-width:768px){.chart-section{grid-template-columns:1fr}.grid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<a href="/" class="back-link">← 返回客服</a>
<div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px">
  <h1>📊 客服数据分析面板</h1>
  <div class="auto-refresh"><input type="checkbox" id="autoRefresh" checked><label for="autoRefresh">自动刷新 (30s)</label></div>
</div>
<div class="grid" id="kpiGrid"></div>
<div class="chart-section">
  <div class="chart-card"><h3>🎯 意图分布</h3><div id="intentChart"></div></div>
  <div class="chart-card"><h3>😊 情绪分布</h3><div id="emotionChart"></div></div>
</div>
<div class="chart-section">
  <div class="chart-card"><h3>⭐ 评分统计</h3><div id="ratingChart"></div></div>
  <div class="chart-card"><h3>🎫 工单优先级</h3><div id="ticketChart"></div></div>
</div>
<div class="chart-section" style="grid-template-columns:1fr">
  <div class="chart-card"><h3>📋 最近会话记录</h3><div id="sessionTable"></div></div>
</div>
<script>
const intentColors={consult:'#3b82f6',complaint:'#ef4444',chat:'#10b981',ending:'#f59e0b'};
const emotionEmojis={neutral:'😐',angry:'😠',sad:'😢',anxious:'😰',happy:'😊'};
const emotionColors={neutral:'#6b7280',angry:'#ef4444',sad:'#8b5cf6',anxious:'#f97316',happy:'#22c55e'};
function intentBadge(t){return'<span class="badge badge-'+(t||'consult')+'">'+(t||'-')+'</span>'}
function renderBars(container,data,colorMap,emojiMap){
  const entries=Object.entries(data).sort((a,b)=>b[1]-a[1]);
  if(!entries.length){container.innerHTML='<p style="color:#9ca3af;font-size:13px">暂无数据</p>';return}
  const max=entries[0][1];
  container.innerHTML=entries.map(([k,v])=>{
    const pct=Math.max(8,(v/max*100)).toFixed(0);
    const color=colorMap[k]||'#6b7280';
    const emoji=emojiMap?emojiMap[k]||'': '';
    return'<div class="bar-row"><span class="bar-label">'+emoji+' '+k+'</span><div class="bar-track"><div class="bar-fill" style="width:'+pct+'%;background:'+color+'">'+v+'</div></div><span class="bar-count">'+v+'</span></div>';
  }).join('');
}
function renderRating(ratings){
  const c=document.getElementById('ratingChart');
  if(!ratings||!ratings.total){c.innerHTML='<p style="color:#9ca3af;font-size:13px">暂无评分</p>';return}
  const avg=(ratings.average||0).toFixed(1);
  let stars='';for(let i=1;i<=5;i++)stars+='<span class="star" style="opacity:'+(i<=Math.round(avg)?'1':'0.2')+'">⭐</span>';
  c.innerHTML='<div class="val" style="font-size:36px">'+avg+'</div><div class="stars-visual">'+stars+'</div><p class="sub">共 '+ratings.total+' 条评价</p>';
}
function renderTickets(tickets){
  const c=document.getElementById('ticketChart');
  if(!tickets||!tickets.total){c.innerHTML='<p style="color:#9ca3af;font-size:13px">暂无工单</p>';return}
  const dist=tickets.by_priority||{};
  const entries=Object.entries(dist).sort((a,b)=>b[1]-a[1]);
  const max=entries.length?entries[0][1]:1;
  const colors={high:'#ef4444',medium:'#f59e0b',low:'#22c55e'};
  c.innerHTML='<p class="val" style="font-size:24px">'+tickets.total+' 工单</p>'+entries.map(([k,v])=>{
    const pct=Math.max(8,(v/max*100)).toFixed(0);
    return'<div class="bar-row"><span class="bar-label">'+k+'</span><div class="bar-track"><div class="bar-fill" style="width:'+pct+'%;background:'+(colors[k]||'#6b7280')+'">'+v+'</div></div></div>';
  }).join('');
}
function renderSessions(sessions){
  const c=document.getElementById('sessionTable');
  if(!sessions.length){c.innerHTML='<p style="color:#9ca3af;font-size:13px">暂无会话</p>';return}
  let html='<table class="timeline-table"><thead><tr><th>会话ID</th><th>消息数</th><th>意图</th><th>最后活动</th><th>预览</th></tr></thead><tbody>';
  sessions.slice(0,20).forEach(s=>{
    const sid=s.session_id?s.session_id.slice(0,8)+'...':'-';
    const ts=s.last_activity?new Date(s.last_activity).toLocaleString('zh-CN'):'-';
    html+='<tr><td style="font-family:monospace;font-size:12px">'+sid+'</td><td>'+s.message_count+'</td><td>'+(s.intents?s.intents.map(i=>intentBadge(i)).join(' '):'-')+'</td><td>'+ts+'</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+(s.preview||'-')+'</td></tr>';
  });
  html+='</tbody></table>';
  c.innerHTML=html;
}
async function load(){
  try{
    const [analyticsResp,sessionsResp]=await Promise.all([
      fetch('/api/analytics').then(r=>r.json()),
      fetch('/api/sessions').then(r=>r.json())
    ]);
    const a=analyticsResp;
    // KPI cards
    document.getElementById('kpiGrid').innerHTML=[
      {label:'总对话数',val:a.total_conversations||0,sub:'所有会话的消息总数'},
      {label:'平均回复长度',val:(a.avg_reply_length||0).toFixed(0),sub:'字符数'},
      {label:'评分数',val:(a.ratings?a.ratings.total:0),sub:'平均 '+((a.ratings&&a.ratings.average)?a.ratings.average.toFixed(1):'-')+' ⭐'},
      {label:'工单总数',val:(a.tickets?a.tickets.total:0),sub:'待处理优先级分布'},
    ].map(k=>'<div class="card"><h3>'+k.label+'</h3><div class="val">'+k.val+'</div><div class="sub">'+k.sub+'</div></div>').join('');
    renderBars(document.getElementById('intentChart'),a.intents||{},intentColors,null);
    renderBars(document.getElementById('emotionChart'),a.emotions||{},emotionColors,emotionEmojis);
    renderRating(a.ratings||{total:0,average:0});
    renderTickets(a.tickets||{total:0,by_priority:{}});
    renderSessions((sessionsResp.sessions||[]));
  }catch(e){console.error('Load error:',e)}
}
load();
let timer=setInterval(()=>{if(document.getElementById('autoRefresh').checked)load()},30000);
document.getElementById('autoRefresh').addEventListener('change',function(){timer=this.checked?setInterval(()=>load(),30000):clearInterval(timer)});
</script>
</body>
</html>"""


class ChatHandler(BaseHTTPRequestHandler):
    def _check_rate_limit(self):
        """Check rate limit and reject if exceeded. Returns True if allowed."""
        client_ip = self.client_address[0] if hasattr(self, 'client_address') else 'unknown'
        if not _rate_limiter.is_allowed(client_ip):
            self.send_response(429)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            body = json.dumps({"error": "Rate limit exceeded", "retry_after": _rate_limiter.window}, ensure_ascii=False)
            self.wfile.write(body.encode('utf-8'))
            return False
        return True

    def do_GET(self):
        # Health check endpoint (no rate limit)
        if self.path == '/api/health':
            self._send_health()
            return

        # Rate limit all other GET endpoints
        if not self._check_rate_limit():
            return

        if self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(CHAT_HTML.encode('utf-8'))
        elif self.path.startswith('/api/session/'):
            # GET /api/session/<session_id> - get session state
            session_id = self.path.split('/')[-1]
            try:
                config = {"configurable": {"thread_id": session_id}}
                state = _graph.get_state(config)
                if state and state.values:
                    msgs = []
                    for m in state.values.get('messages', []):
                        msgs.append({
                            'role': 'user' if isinstance(m, HumanMessage) else 'assistant',
                            'content': m.content
                        })
                    result = {
                        'messages': msgs,
                        'intent': state.values.get('intent', 'unknown'),
                        'emotion': state.values.get('emotion', 'neutral'),
                        'retry_count': state.values.get('retry_count', 0),
                    }
                else:
                    result = {'messages': [], 'intent': 'unknown', 'emotion': 'neutral', 'retry_count': 0}
            except Exception as e:
                result = {'error': str(e)}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
        elif self.path == '/api/analytics':
            # GET /api/analytics - conversation analytics dashboard data
            try:
                from agent.memory import _get_connection
                conn = _get_connection()

                # Total conversations
                total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]

                # Intent distribution
                intent_rows = conn.execute(
                    "SELECT intent, COUNT(*) as cnt FROM conversations GROUP BY intent ORDER BY cnt DESC"
                ).fetchall()
                intents = {row[0]: row[1] for row in intent_rows}

                # Emotion distribution
                emotion_rows = conn.execute(
                    "SELECT emotion, COUNT(*) as cnt FROM conversations GROUP BY emotion ORDER BY cnt DESC"
                ).fetchall()
                emotions = {row[0]: row[1] for row in emotion_rows}

                # Average response length (from bot_reply)
                avg_len_row = conn.execute(
                    "SELECT AVG(LENGTH(bot_reply)) FROM conversations WHERE bot_reply IS NOT NULL"
                ).fetchone()
                avg_reply_length = round(avg_len_row[0], 1) if avg_len_row and avg_len_row[0] else 0

                # Rating stats
                rating_row = conn.execute(
                    "SELECT COUNT(*), COALESCE(AVG(stars), 0) FROM ratings"
                ).fetchone()
                total_ratings = rating_row[0]
                avg_rating = round(rating_row[1], 2) if total_ratings > 0 else 0

                # Ticket stats
                ticket_count = 0
                priority_dist = {}
                try:
                    ticket_count = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
                    priority_rows = conn.execute(
                        "SELECT priority, COUNT(*) as cnt FROM tickets GROUP BY priority"
                    ).fetchall()
                    priority_dist = {row[0]: row[1] for row in priority_rows}
                except Exception:
                    pass

                analytics = {
                    "total_conversations": total,
                    "intents": intents,
                    "emotions": emotions,
                    "avg_reply_length": avg_reply_length,
                    "ratings": {"total": total_ratings, "average": avg_rating},
                    "tickets": {"total": ticket_count, "by_priority": priority_dist},
                }
            except Exception as e:
                analytics = {"error": str(e)}

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(analytics, ensure_ascii=False).encode('utf-8'))
        elif self.path.startswith('/api/export/'): 
            # GET /api/export/<session_id> - export session history as JSON
            session_id = self.path.split('/')[-1]
            try:
                config = {"configurable": {"thread_id": session_id}}
                state = _graph.get_state(config)
                if state and state.values:
                    msgs = []
                    for m in state.values.get('messages', []):
                        msgs.append({
                            'role': 'user' if isinstance(m, HumanMessage) else 'assistant',
                            'content': m.content
                        })
                    export_data = {
                        'session_id': session_id,
                        'exported_at': datetime.now().isoformat(),
                        'message_count': len(msgs),
                        'messages': msgs,
                        'intent': state.values.get('intent', 'unknown'),
                        'emotion': state.values.get('emotion', 'neutral'),
                        'emotion_intensity': state.values.get('emotion_intensity', 1),
                        'retry_count': state.values.get('retry_count', 0),
                    }
                else:
                    export_data = {'session_id': session_id, 'messages': [], 'exported_at': datetime.now().isoformat()}
            except Exception as e:
                export_data = {'error': str(e)}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(export_data, ensure_ascii=False).encode('utf-8'))
        elif self.path == '/api/rag/reload':
            # GET /api/rag/reload - hot reload knowledge base
            try:
                from agent.rag import reload as _rag_reload
                docs = _rag_reload()
                result = {'reloaded': True, 'documents': len(docs), 'sections': sum(len(d['sections']) for d in docs)}
            except Exception as e:
                result = {'error': str(e)}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
        elif self.path.startswith('/api/sessions'):
            # GET /api/sessions - list all sessions from memory DB with summary
            # Supports ?search=<query> for keyword search across messages
            try:
                from agent.memory import _get_connection
                conn = _get_connection()

                # Parse query params for search
                search_query = ''
                if '?' in self.path:
                    from urllib.parse import parse_qs, urlparse
                    params = parse_qs(urlparse(self.path).query)
                    search_query = params.get('search', [''])[0].strip()

                if search_query:
                    # Search across user_message and bot_reply fields
                    like_pattern = f'%{search_query}%'
                    rows = conn.execute(
                        """SELECT session_id, COUNT(*) as msg_count,
                                 MAX(timestamp) as last_at,
                                 GROUP_CONCAT(DISTINCT intent) as intents
                          FROM conversation_history
                          WHERE user_message LIKE ? OR bot_reply LIKE ?
                          GROUP BY session_id
                          ORDER BY last_at DESC
                          LIMIT 50""",
                        (like_pattern, like_pattern)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT session_id, COUNT(*) as msg_count,
                                 MAX(timestamp) as last_at,
                                 GROUP_CONCAT(DISTINCT intent) as intents
                          FROM conversation_history
                          GROUP BY session_id
                          ORDER BY last_at DESC
                          LIMIT 50"""
                    ).fetchall()

                sessions = []
                for row in rows:
                    sid = row[0]
                    # Get the latest user message for preview
                    last_msg_row = conn.execute(
                        "SELECT user_message FROM conversation_history WHERE session_id = ? ORDER BY timestamp DESC LIMIT 1",
                        (sid,)
                    ).fetchone()
                    preview = ''
                    if last_msg_row:
                        preview = last_msg_row[0][:60]

                    sessions.append({
                        'session_id': sid,
                        'message_count': row[1],
                        'last_activity': row[2] or '',
                        'intents': row[3].split(',') if row[3] else [],
                        'preview': preview,
                    })

                result = {'sessions': sessions, 'total': len(sessions), 'search': search_query}
            except Exception as e:
                result = {'error': str(e), 'sessions': []}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
        elif self.path == '/api/stats':
            # GET /api/stats - get memory database stats + rating summary
            try:
                from agent.memory import get_stats
                result = get_stats()
                # Add rating summary
                try:
                    from agent.memory import _get_connection
                    conn = _get_connection()
                    cur = conn.execute("SELECT COUNT(*), AVG(stars) FROM ratings")
                    row = cur.fetchone()
                    if row and row[0] > 0:
                        result['total_ratings'] = row[0]
                        result['avg_rating'] = round(row[1], 2)
                    conn.close()
                except Exception:
                    pass
            except Exception as e:
                result = {'error': str(e)}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
        elif self.path == '/analytics':
            # GET /analytics - Analytics Dashboard UI (HTML page with charts)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(ANALYTICS_HTML.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def _send_health(self):
        """Health check endpoint 鈥?returns system status, LLM connectivity, DB stats."""
        try:
            # LLM connectivity
            llm_reachable = False
            try:
                import urllib.request as ur
                resp = ur.urlopen("http://127.0.0.1:8080/v1/models", timeout=3)
                llm_reachable = resp.status == 200
            except Exception:
                pass

            # DB stats
            db_stats = {}
            try:
                from agent.memory import _get_connection
                conn = _get_connection()
                conversations = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tickets'").fetchone() else 0
                ratings = conn.execute("SELECT COUNT(*), COALESCE(AVG(stars),0) FROM ratings").fetchone()
                db_stats = {
                    "conversations": conversations,
                    "tickets": tickets,
                    "total_ratings": ratings[0],
                    "avg_rating": round(ratings[1], 2) if ratings[0] > 0 else 0,
                }
                conn.close()
            except Exception as e:
                db_stats = {"error": str(e)}

            # KB stats
            kb_stats = {}
            try:
                from agent.rag import _load_knowledge_base, _documents
                docs = _load_knowledge_base()
                kb_stats = {
                    "documents": len(docs),
                    "sections": sum(len(d.get("sections", [])) for d in docs),
                }
            except Exception as e:
                kb_stats = {"error": str(e)}

            health = {
                "ok": True,
                "service": "LangGraph Customer Service Agent",
                "port": PORT,
                "platform": f"{platform.system()} {platform.release()}",
                "python": platform.python_version(),
                "llm": {
                    "reachable": llm_reachable,
                    "url": "http://127.0.0.1:8080",
                },
                "database": db_stats,
                "knowledge_base": kb_stats,
                "requests": {
                    "total": _request_counter["total"],
                    "errors": _request_counter["errors"],
                },
            }

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(health, ensure_ascii=False).encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode('utf-8'))

    def do_POST(self):
        # Rate limit all POST endpoints except /api/rating, /api/reaction (fire-and-forget)
        if self.path not in ('/api/rating', '/api/reaction') and not self._check_rate_limit():
            return

        _request_counter["total"] += 1
        start_time = time.time()
        print(f"[POST] {self.path} from {self.client_address[0]}")

        try:
            self._handle_post(start_time)
        except Exception as e:
            _request_counter["errors"] += 1
            import traceback
            traceback.print_exc()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode('utf-8'))

    def _handle_post(self, start_time):
        """Handle POST requests with timing header."""
        if self.path == '/api/rating':
            # POST /api/rating - log user satisfaction rating
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            session_id = data.get('session_id', '')
            msg_index = data.get('message_index', 0)
            stars = data.get('stars', 0)
            print(f"[Rating] session={session_id}, msg={msg_index}, stars={stars}")

            # Store rating in memory DB
            try:
                from agent.memory import _get_connection
                conn = _get_connection()
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS ratings (" +
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, " +
                    "session_id TEXT, message_index INTEGER, stars INTEGER, rated_at TEXT)"
                )
                conn.execute(
                    "INSERT INTO ratings (session_id, message_index, stars, rated_at) VALUES (?, ?, ?, ?)",
                    (session_id, msg_index, stars, datetime.now().isoformat())
                )
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[Rating DB Error] {e}")

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode('utf-8'))
        elif self.path == '/api/reaction':
            # POST /api/reaction - log emoji reaction on bot message
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            session_id = data.get('session_id', '')
            msg_id = data.get('message_id', '')
            emoji = data.get('emoji', '👍')
            active = data.get('active', True)
            print(f"[Reaction] session={session_id}, msg={msg_id}, emoji={emoji}, active={active}")

            # Store reaction in memory DB
            try:
                from agent.memory import _get_connection
                conn = _get_connection()
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS reactions (" +
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, " +
                    "session_id TEXT, message_id TEXT, emoji TEXT, active INTEGER, reacted_at TEXT)"
                )
                conn.execute(
                    "INSERT INTO reactions (session_id, message_id, emoji, active, reacted_at) VALUES (?, ?, ?, ?, ?)",
                    (session_id, msg_id, emoji, 1 if active else 0, datetime.now().isoformat())
                )
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[Reaction DB Error] {e}")

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode('utf-8'))
        elif self.path == '/api/chat':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"[Chat] Invalid JSON: {e}")
                self.send_response(400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid request body"}, ensure_ascii=False).encode('utf-8'))
                return

            user_message = data.get('message', '')
            session_id = data.get('session_id', str(uuid4()))
            stream = data.get('stream', False)

            # Input validation (LangGraph best practice: validate before entering graph)
            if not user_message or not user_message.strip():
                self.send_response(400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Message cannot be empty"}, ensure_ascii=False).encode('utf-8'))
                return
            if len(user_message) > 4000:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Message too long (max 4000 chars)"}, ensure_ascii=False).encode('utf-8'))
                return
            user_message = user_message.strip()

            if stream:
                # SSE streaming response
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                self.send_header('X-Accel-Buffering', 'no')
                self.end_headers()
                # Flush immediately so client sees headers
                try:
                    self.wfile.flush()
                except Exception:
                    pass
                try:
                    for chunk in run_agent_stream(session_id, user_message):
                        self.wfile.write(chunk.encode('utf-8'))
                        self.wfile.flush()
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    err = json.dumps({'error': str(e)}, ensure_ascii=False)
                    self.wfile.write(f"data: {err}\n\n".encode('utf-8'))
                    self.wfile.flush()
                finally:
                    # Log streaming response time
                    elapsed = time.time() - start_time
                    print(f"[Stream] Response time: {elapsed*1000:.1f}ms")
                    # Close connection explicitly so client knows stream ended
                    try:
                        self.wfile.flush()
                    except Exception:
                        pass
            else:
                # Standard JSON response
                try:
                    result = run_agent(session_id, user_message)
                    response = json.dumps(result, ensure_ascii=False)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    response = json.dumps({'error': str(e)}, ensure_ascii=False)

                elapsed = time.time() - start_time
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('X-Response-Time', f'{elapsed*1000:.1f}ms')
                self.end_headers()
                self.wfile.write(response.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")


def main():
    init()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), ChatHandler)
    print(f"[Server] Running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
