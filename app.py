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
from http.server import HTTPServer, BaseHTTPRequestHandler
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


def init():
    """Initialize the agent graph.

    Set USE_SQLITE=1 environment variable for persistent checkpointing.
    """
    global _graph
    use_sqlite = os.environ.get('USE_SQLITE', '0') == '1'
    db_path = os.environ.get('CHECKPOINT_DB', 'checkpoints.db')
    _graph = build_graph(use_sqlite=use_sqlite, db_path=db_path)
    print(f"[Server] Agent initialized (Real LLM via llama.cpp, sqlite={use_sqlite})")


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
    from agent.rag import build_context as _rag_build
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

    # RAG context
    rag_context = ""
    if intent == 'consult':
        rag_context = _rag_build(user_message)

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

    # Add to graph state (write directly)
    ai_message = AIMessage(content=full_reply)
    _graph.update_state(config, {'messages': [ai_message], 'bot_reply': full_reply})

    # Final metadata event
    meta = {
        "done": True,
        "intent": intent,
        "emotion": emotion,
        "emotion_intensity": intensity,
        "reply_type": _classify_message(full_reply),
        "session_id": session_id
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
:root {
  --c-bg: #edeef0;
  --c-sidebar: #1a1d23;
  --c-sidebar-hover: #2a2d35;
  --c-sidebar-active: #3a3d45;
  --c-card: #ffffff;
  --c-border: #e0e2e6;
  --c-text: #1a1d23;
  --c-text2: #6b7280;
  --c-text3: #9ca3af;
  --c-accent: #5b5fc7;
  --c-accent-light: #eef0ff;
  --c-green: #1a7f37;
  --c-green-bg: #dafbe1;
  --c-red: #cf222e;
  --c-red-bg: #ffebe9;
  --c-orange: #bf8700;
  --c-orange-bg: #fff8c5;
  --c-user: #5b5fc7;
  --c-bot: #ffffff;
  --r: 8px;
  --shadow: 0 1px 3px rgba(0,0,0,0.08);
}
[data-theme=dark] {
  --c-bg: #0d1117;
  --c-sidebar: #010409;
  --c-sidebar-hover: #161b22;
  --c-sidebar-active: #1c2128;
  --c-card: #161b22;
  --c-border: #30363d;
  --c-text: #e6edf3;
  --c-text2: #8b949e;
  --c-text3: #6e7681;
  --c-accent: #79c0ff;
  --c-accent-light: #0c2d6b;
  --c-green: #3fb950;
  --c-green-bg: #0b2d13;
  --c-red: #f85149;
  --c-red-bg: #3d1214;
  --c-orange: #d29922;
  --c-orange-bg: #2e1e00;
  --c-user: #79c0ff;
  --c-bot: #161b22;
  --shadow: 0 1px 3px rgba(0,0,0,0.3);
}
* { margin:0; padding:0; box-sizing:border-box; }
html,body { height:100%; overflow:hidden; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif;
  background: var(--c-bg);
  color: var(--c-text);
  display: flex;
  -webkit-font-smoothing: antialiased;
}

/* ─── Sidebar ─── */
.sidebar {
  width: 260px;
  background: var(--c-sidebar);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  color: #c9d1d9;
}
.sidebar-header {
  padding: 20px 16px 12px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
}
.sidebar-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.sidebar-brand .icon {
  width: 28px; height: 28px;
  background: var(--c-accent);
  border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  font-size: 14px;
}
.sidebar-brand .name {
  font-size: 14px;
  font-weight: 600;
  color: #e6edf3;
}
.sidebar-brand .ver {
  font-size: 11px;
  color: #6e7681;
}
.new-chat-btn {
  width: 100%;
  padding: 8px 12px;
  background: var(--c-accent);
  color: #fff;
  border: none;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center; gap: 6px;
  transition: opacity 0.15s;
}
.new-chat-btn:hover { opacity: 0.85; }

.sidebar-section {
  padding: 12px 16px 4px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #6e7681;
}
.sidebar-nav {
  flex: 1;
  overflow-y: auto;
  padding: 4px 8px;
}
.sidebar-nav::-webkit-scrollbar { width: 4px; }
.sidebar-nav::-webkit-scrollbar-thumb { background: #30363d; border-radius: 2px; }
.nav-item {
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  display: flex; align-items: center; gap: 8px;
  transition: background 0.1s;
  color: #c9d1d9;
  margin-bottom: 2px;
}
.nav-item:hover { background: var(--c-sidebar-hover); }
.nav-item.active { background: var(--c-sidebar-active); color: #e6edf3; }
.nav-item .ico { font-size: 14px; width: 20px; text-align: center; }

.sidebar-footer {
  padding: 12px 16px;
  border-top: 1px solid rgba(255,255,255,0.06);
  display: flex; align-items: center; justify-content: space-between;
}
.theme-switch {
  background: none; border: 1px solid #30363d;
  color: #8b949e; cursor: pointer;
  padding: 4px 8px; border-radius: 6px;
  font-size: 13px;
  transition: all 0.15s;
}
.theme-switch:hover { border-color: #6e7681; color: #c9d1d9; }
.status-dot {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: #3fb950;
}
.status-dot::before {
  content: '';
  width: 8px; height: 8px;
  background: #3fb950;
  border-radius: 50%;
  display: inline-block;
}

/* ─── Main ─── */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

/* ─── Toolbar ─── */
.main-toolbar {
  background: var(--c-card);
  border-bottom: 1px solid var(--c-border);
  padding: 8px 20px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.toolbar-btn {
  padding: 5px 12px;
  border: 1px solid var(--c-border);
  background: var(--c-card);
  color: var(--c-text2);
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.12s;
  white-space: nowrap;
}
.toolbar-btn:hover {
  border-color: var(--c-accent);
  color: var(--c-accent);
  background: var(--c-accent-light);
}
.toolbar-btn.danger:hover {
  border-color: var(--c-red);
  color: var(--c-red);
  background: var(--c-red-bg);
}
.toolbar-sep {
  width: 1px; height: 20px;
  background: var(--c-border);
  margin: 0 4px;
}

/* ─── Test Bar ─── */
.test-bar {
  background: var(--c-card);
  border-bottom: 1px solid var(--c-border);
  padding: 6px 20px;
  display: flex;
  gap: 5px;
  align-items: center;
  overflow-x: auto;
  scrollbar-width: none;
  flex-shrink: 0;
}
.test-bar::-webkit-scrollbar { display: none; }
.test-bar .lbl {
  font-size: 11px; color: var(--c-text3);
  font-weight: 600; margin-right: 2px; flex-shrink: 0;
}
.chip {
  padding: 3px 10px;
  border: 1px solid var(--c-border);
  border-radius: 12px;
  font-size: 12px;
  background: var(--c-card);
  color: var(--c-text2);
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.12s;
}
.chip:hover { border-color: var(--c-accent); color: var(--c-accent); }
.chip.r { border-color: var(--c-red); color: var(--c-red); }
.chip.r:hover { background: var(--c-red-bg); }
.chip.g { border-color: var(--c-green); color: var(--c-green); }
.chip.g:hover { background: var(--c-green-bg); }

/* ─── Chat ─── */
.chat {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.chat::-webkit-scrollbar { width: 6px; }
.chat::-webkit-scrollbar-track { background: transparent; }
.chat::-webkit-scrollbar-thumb { background: var(--c-border); border-radius: 3px; }

/* ─── Messages ─── */
.bubble-row {
  display: flex;
  gap: 8px;
  max-width: 680px;
  animation: slideUp 0.25s ease;
}
@keyframes slideUp {
  from { opacity:0; transform:translateY(8px); }
  to { opacity:1; transform:translateY(0); }
}
.bubble-row.user { flex-direction: row-reverse; margin-left: auto; }
.bubble-row.bot { margin-right: auto; }

.bubble-av {
  width: 30px; height: 30px;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; flex-shrink: 0; margin-top: 2px;
}
.bubble-row.user .bubble-av { background: var(--c-accent); color: #fff; }
.bubble-row.bot .bubble-av { background: #da3633; color: #fff; }

.bubble-content { min-width: 0; }
.bubble {
  padding: 8px 14px;
  border-radius: var(--r);
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.bubble-row.user .bubble {
  background: var(--c-user);
  color: #fff;
}
.bubble-row.bot .bubble {
  background: var(--c-bot);
  border: 1px solid var(--c-border);
  color: var(--c-text);
}
.bubble-row.bot .bubble.satisfaction { border-color: var(--c-orange); background: var(--c-orange-bg); }
.bubble-row.bot .bubble.closing { border-color: var(--c-green); background: var(--c-green-bg); }

.bubble-meta {
  display: flex; gap: 6px; align-items: center;
  padding: 3px 2px;
}
.bubble-row.user .bubble-meta { justify-content: flex-end; }
.bubble-meta span {
  font-size: 11px; color: var(--c-text3);
}
.bubble-meta .copy-btn {
  font-size: 11px; color: var(--c-text3);
  background: none; border: none; cursor: pointer;
  padding: 0 3px; border-radius: 3px;
  opacity: 0; transition: opacity 0.15s;
}
.bubble-row:hover .copy-btn { opacity: 1; }
.copy-btn:hover { color: var(--c-accent); }

/* Stars */
.star-row {
  display: flex; gap: 1px; align-items: center;
  margin-top: 4px; padding-left: 2px;
}
.star-row .lbl { font-size: 11px; color: var(--c-text3); margin-right: 4px; }
.star-btn {
  background: none; border: none; cursor: pointer;
  font-size: 14px; padding: 1px 2px;
  filter: grayscale(1) opacity(0.25);
  transition: filter 0.12s, transform 0.12s;
}
.star-btn:hover { filter: grayscale(0) opacity(1); transform: scale(1.2); }
.star-btn.on { filter: grayscale(0) opacity(1); }
.star-thanks { font-size: 12px; color: var(--c-green); margin-top: 3px; }

/* System */
.sys {
  align-self: center;
  font-size: 12px; color: var(--c-text3);
  padding: 4px 12px;
  background: var(--c-card);
  border: 1px solid var(--c-border);
  border-radius: 12px;
}

/* Quick replies */
.qr-row {
  display: flex; gap: 5px; flex-wrap: wrap;
  margin-left: 38px;
}
.qr-btn {
  padding: 4px 12px;
  border: 1px solid var(--c-border);
  border-radius: 14px;
  font-size: 12px;
  background: var(--c-card);
  color: var(--c-text2);
  cursor: pointer;
  transition: all 0.12s;
}
.qr-btn:hover {
  border-color: var(--c-accent);
  color: var(--c-accent);
  background: var(--c-accent-light);
}

/* Typing */
.typing-dots {
  display: inline-flex; gap: 4px; padding: 10px 14px; align-items: center;
}
.typing-dots span {
  width: 6px; height: 6px;
  background: var(--c-text3);
  border-radius: 50%;
  animation: tbounce 1.4s infinite;
}
.typing-dots span:nth-child(2) { animation-delay: 0.2s; }
.typing-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes tbounce {
  0%,60%,100% { transform:translateY(0); opacity:0.4; }
  30% { transform:translateY(-5px); opacity:1; }
}
.typing-cursor::after {
  content: '\2588';
  animation: blink 0.8s infinite;
  color: var(--c-accent);
  font-size: 13px;
}
@keyframes blink { 0%,50% { opacity:1; } 51%,100% { opacity:0; } }

/* Emotion bar */
.em-bar { display: inline-flex; gap: 2px; vertical-align: middle; }
.em-bar i {
  width: 5px; height: 10px;
  border-radius: 1px;
  background: var(--c-border);
  font-style: normal;
}
.em-bar i.on { background: var(--c-accent); }
.em-bar i.hi { background: var(--c-red); }

/* ─── Info Bar ─── */
.info-strip {
  background: var(--c-card);
  border-top: 1px solid var(--c-border);
  padding: 5px 20px;
  font-size: 11px;
  color: var(--c-text3);
  display: flex; gap: 14px;
  flex-shrink: 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.info-strip::-webkit-scrollbar { display: none; }
.info-strip .iv { display: flex; align-items: center; gap: 3px; white-space: nowrap; }
.info-strip .ik { font-weight: 500; }
.info-strip .val { font-family: 'SF Mono','Cascadia Code','Consolas',monospace; color: var(--c-text2); }

/* ─── Input ─── */
.input-bar {
  background: var(--c-card);
  border-top: 1px solid var(--c-border);
  padding: 10px 20px 14px;
  display: flex; gap: 8px; align-items: center;
  flex-shrink: 0;
}
.input-bar input {
  flex: 1;
  padding: 9px 14px;
  border: 1px solid var(--c-border);
  border-radius: var(--r);
  font-size: 14px;
  background: var(--c-bg);
  color: var(--c-text);
  outline: none;
  transition: border-color 0.15s;
}
.input-bar input:focus { border-color: var(--c-accent); }
.input-bar input::placeholder { color: var(--c-text3); }
.send-btn {
  padding: 9px 18px;
  background: var(--c-accent);
  color: #fff;
  border: none;
  border-radius: var(--r);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 0.15s;
  flex-shrink: 0;
}
.send-btn:hover { opacity: 0.85; }
.send-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* ─── Modal ─── */
.modal-bg {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,0.5);
  z-index: 100;
  align-items: center; justify-content: center;
}
.modal-bg.show { display: flex; }
.modal-box {
  background: var(--c-card);
  border: 1px solid var(--c-border);
  border-radius: 10px;
  padding: 20px;
  width: 92%; max-width: 640px; max-height: 80vh;
  overflow-y: auto;
  box-shadow: var(--shadow);
}
.modal-box h3 { font-size: 15px; margin-bottom: 12px; }
.modal-box pre {
  background: var(--c-bg);
  border: 1px solid var(--c-border);
  padding: 12px; border-radius: 6px;
  font-size: 12px;
  font-family: 'SF Mono','Cascadia Code','Consolas',monospace;
  overflow-x: auto; max-height: 50vh;
  white-space: pre-wrap; word-break: break-all;
}
.modal-btns { display: flex; gap: 6px; margin-top: 12px; justify-content: flex-end; }

/* ─── Session Dropdown ─── */
.sess-dd { position: relative; }
.sess-panel {
  display: none; position: absolute;
  left: 0; top: calc(100% + 4px);
  background: var(--c-sidebar);
  border: 1px solid #30363d;
  border-radius: 8px;
  width: 240px; max-height: 280px;
  overflow-y: auto; z-index: 200;
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.sess-panel.show { display: block; }
.sess-item {
  padding: 8px 12px; cursor: pointer;
  border-bottom: 1px solid rgba(255,255,255,0.04);
  transition: background 0.1s;
}
.sess-item:hover { background: var(--c-sidebar-hover); }
.sess-item:last-child { border-bottom: none; }
.sess-item .sid { font-size: 11px; color: #6e7681; font-family: monospace; }
.sess-item .prev { font-size: 12px; color: #c9d1d9; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sess-item .mt { font-size: 11px; color: #6e7681; margin-top: 1px; }
.sess-empty { padding: 16px; text-align: center; color: #6e7681; font-size: 12px; }

/* ─── Mobile ─── */
@media (max-width: 768px) {
  .sidebar { display: none; }
  .main-toolbar { padding: 6px 12px; }
  .test-bar { padding: 6px 12px; }
  .chat { padding: 12px; gap: 12px; }
  .bubble-row { max-width: 100%; }
  .bubble { font-size: 13px; padding: 7px 12px; }
  .input-bar { padding: 8px 12px 12px; }
  .info-strip { padding: 4px 12px; font-size: 10px; }
}
</style>
</head>
<body>

<!-- ─── Sidebar ─── -->
<div class="sidebar">
  <div class="sidebar-header">
    <div class="sidebar-brand">
      <div class="icon">S</div>
      <div>
        <div class="name">Smart Support</div>
        <div class="ver">LangGraph Agent v1.0</div>
      </div>
    </div>
    <button class="new-chat-btn" onclick="newSession()">+ 新会话</button>
  </div>
  <div class="sidebar-section">会话</div>
  <div class="sidebar-nav" id="sessionList"></div>
  <div class="sidebar-section">快捷操作</div>
  <div class="sidebar-nav">
    <div class="nav-item" onclick="runFullFlow()"><span class="ico">▶</span> 自动演示</div>
    <div class="nav-item" onclick="exportSession()"><span class="ico">📋</span> 导出会话</div>
    <div class="nav-item" onclick="reloadKB()"><span class="ico">🔄</span> 重载知识库</div>
    <div class="nav-item" onclick="resetAll()"><span class="ico">🗑</span> 清除所有</div>
  </div>
  <div class="sidebar-footer">
    <div class="status-dot">在线</div>
    <button class="theme-switch" onclick="toggleTheme()" id="themeBtn">🌙</button>
  </div>
</div>

<!-- ─── Main ─── -->
<div class="main">
  <div class="main-toolbar">
    <button class="toolbar-btn" onclick="newSession()">+ 新会话</button>
    <button class="toolbar-btn" onclick="clearChat()">清空</button>
    <button class="toolbar-btn danger" onclick="resetAll()">重置</button>
    <div class="toolbar-sep"></div>
    <button class="toolbar-btn" onclick="reloadKB()">🔄 知识库</button>
    <button class="toolbar-btn" onclick="exportSession()">📋 导出</button>
  </div>

  <div class="test-bar">
    <span class="lbl">测试</span>
    <button class="chip" onclick="quickTest('产品怎么用？')">产品咨询</button>
    <button class="chip r" onclick="quickTest('我要投诉，产品质量太差了')">投诉</button>
    <button class="chip" onclick="quickTest('你好')">打招呼</button>
    <span class="lbl" style="margin-left:6px">结束</span>
    <button class="chip g" onclick="quickTest('谢谢，没问题了')">感谢并结束</button>
    <button class="chip r" onclick="quickTest('再见')">说再见</button>
    <span class="lbl" style="margin-left:6px">反馈</span>
    <button class="chip g" onclick="quickTest('满意')">满意</button>
    <button class="chip r" onclick="quickTest('不满意')">不满意</button>
    <span class="lbl" style="margin-left:6px">会话</span>
    <div class="sess-dd">
      <button class="chip" onclick="toggleSessionPanel()">📂 历史</button>
      <div class="sess-panel" id="sessPanel"></div>
    </div>
  </div>

  <div class="chat" id="chat"></div>

  <div class="info-strip">
    <div class="iv"><span class="ik">会话</span> <span class="val" id="iSess">-</span></div>
    <div class="iv"><span class="ik">意图</span> <span class="val" id="iIntent">-</span></div>
    <div class="iv"><span class="ik">重试</span> <span class="val" id="iRetry">0</span></div>
    <div class="iv"><span class="ik">情绪</span> <span id="iEmo">-</span> <span id="emBar" class="em-bar"></span></div>
    <div class="iv"><span class="ik">消息</span> <span class="val" id="iMsg">0</span></div>
    <div class="iv"><span class="ik">状态</span> <span class="val" id="iStat">Active</span></div>
  </div>

  <div class="input-bar">
    <input type="text" id="msgInput" placeholder="输入消息..." autocomplete="off" />
    <button class="send-btn" id="sendBtn" onclick="sendMsg()">发送</button>
  </div>
</div>

<!-- ─── Modal ─── -->
<div class="modal-bg" id="exportModal">
  <div class="modal-box">
    <h3>📋 会话导出</h3>
    <pre id="exportContent">加载中...</pre>
    <div class="modal-btns">
      <button class="toolbar-btn" onclick="copyExport()">复制</button>
      <button class="toolbar-btn" onclick="dlExport()">下载 JSON</button>
      <button class="toolbar-btn" onclick="closeModal()">关闭</button>
    </div>
  </div>
</div>

<script>
let curSess = null, busy = false, msgCnt = 0, botIdx = 0;
const QR = {
  def: ['产品怎么用？','我要投诉','价格是多少？','有保修吗？'],
  reply: ['能详细说说吗？','还有其他问题','谢谢，没问题了'],
  sat: ['满意','不满意'],
};

/* Theme */
function toggleTheme() {
  const h = document.documentElement, b = document.getElementById('themeBtn');
  if (h.getAttribute('data-theme') === 'dark') {
    h.removeAttribute('data-theme'); b.textContent = '🌙'; localStorage.setItem('th','light');
  } else {
    h.setAttribute('data-theme','dark'); b.textContent = '☀️'; localStorage.setItem('th','dark');
  }
}
if (localStorage.getItem('th') === 'dark') {
  document.documentElement.setAttribute('data-theme','dark');
  setTimeout(() => { const b = document.getElementById('themeBtn'); if(b) b.textContent = '☀️'; }, 0);
}

const chat = document.getElementById('chat');
const inp = document.getElementById('msgInput');
const sbtn = document.getElementById('sendBtn');

inp.addEventListener('keydown', e => { if (e.key === 'Enter' && !busy) sendMsg(); });

/* Add message */
function addMsg(role, text, type, anim) {
  msgCnt++;
  document.getElementById('iMsg').textContent = msgCnt;
  const row = document.createElement('div');
  row.className = 'bubble-row ' + role;
  const av = document.createElement('div');
  av.className = 'bubble-av';
  av.textContent = role === 'user' ? 'U' : 'A';
  const body = document.createElement('div');
  body.className = 'bubble-content';
  const bub = document.createElement('div');
  bub.className = 'bubble ' + (type || '');
  const meta = document.createElement('div');
  meta.className = 'bubble-meta';
  const ts = document.createElement('span');
  ts.textContent = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
  meta.appendChild(ts);
  if (role === 'bot' && text) {
    const rt = document.createElement('span');
    rt.textContent = /[\u4e00-\u9fff]/.test(text) ? Math.ceil(text.length/5)+'秒' : Math.ceil(text.split(/\s+/).filter(w=>w).length/2.7)+'s';
    meta.appendChild(rt);
    const cp = document.createElement('button');
    cp.className = 'copy-btn'; cp.textContent = '复制';
    cp.onclick = () => { navigator.clipboard.writeText(bub.textContent).then(()=>{cp.textContent='✓';setTimeout(()=>cp.textContent='复制',1500)}); };
    meta.appendChild(cp);
  }
  body.appendChild(bub);
  body.appendChild(meta);
  row.appendChild(av);
  row.appendChild(body);
  chat.appendChild(row);
  chat.scrollTop = chat.scrollHeight;
  rmQR();
  if (anim && role === 'bot') {
    const spd = /[\u4e00-\u9fff]/.test(text) ? 35 : 20;
    bub.classList.add('typing-cursor');
    let i = 0;
    (function t() {
      if (i < text.length) { bub.textContent = text.substring(0, i+1); i++; chat.scrollTop = chat.scrollHeight; setTimeout(t, spd); }
      else bub.classList.remove('typing-cursor');
    })();
  } else bub.textContent = text;
  if (role === 'bot' && type !== 'satisfaction' && type !== 'closing') {
    botIdx++;
    addStars(body, botIdx);
  }
  return row;
}

function addSys(t) {
  const d = document.createElement('div');
  d.className = 'sys'; d.textContent = t;
  chat.appendChild(d); chat.scrollTop = chat.scrollHeight;
}

function addTyping() {
  const row = document.createElement('div');
  row.className = 'bubble-row bot'; row.id = 'typing';
  row.innerHTML = '<div class="bubble-av">A</div><div class="bubble-content"><div class="bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div></div>';
  chat.appendChild(row); chat.scrollTop = chat.scrollHeight;
}
function rmTyping() { const e = document.getElementById('typing'); if (e) e.remove(); }

/* Quick replies */
function showQR(replies) {
  rmQR();
  const c = document.createElement('div');
  c.className = 'qr-row'; c.id = 'qr';
  for (const t of replies) {
    const b = document.createElement('button');
    b.className = 'qr-btn'; b.textContent = t;
    b.onclick = () => quickTest(t);
    c.appendChild(b);
  }
  chat.appendChild(c); chat.scrollTop = chat.scrollHeight;
}
function rmQR() { const e = document.getElementById('qr'); if (e) e.remove(); }
function ctxQR(lt) {
  if (lt === 'satisfaction') return QR.sat;
  if (lt === 'closing') return [];
  if (lt === 'reply') return QR.reply;
  return QR.def;
}

/* Stars */
function addStars(parent, idx) {
  const d = document.createElement('div');
  d.className = 'star-row';
  const l = document.createElement('span');
  l.className = 'lbl'; l.textContent = '有帮助？';
  d.appendChild(l);
  for (let i = 1; i <= 5; i++) {
    const b = document.createElement('button');
    b.className = 'star-btn'; b.textContent = '⭐'; b.title = i+'星';
    b.onclick = () => rate(idx, i, d);
    b.onmouseenter = () => d.querySelectorAll('.star-btn').forEach((x,j) => x.style.filter = j<i ? 'grayscale(0) opacity(1)' : 'grayscale(1) opacity(0.25)');
    b.onmouseleave = () => { if (!d.querySelector('.on')) d.querySelectorAll('.star-btn').forEach(x => x.style.filter = 'grayscale(1) opacity(0.25)'); };
    d.appendChild(b);
  }
  parent.appendChild(d);
}
function rate(idx, stars, d) {
  d.querySelectorAll('.star-btn').forEach((b,j) => { if (j<stars) b.classList.add('on'); b.onclick=null; b.style.cursor='default'; });
  const l = d.querySelector('.lbl'); if (l) l.remove();
  const th = document.createElement('div');
  th.className = 'star-thanks'; th.textContent = '感谢评价！'+stars+'⭐';
  d.replaceWith(th);
  if (curSess) fetch('/api/rating',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:curSess,message_index:idx,stars})}).catch(()=>{});
}

/* Send */
async function sendMsg(text) {
  if (busy) return;
  const msg = text || inp.value.trim();
  if (!msg) return;
  inp.value = '';
  addMsg('user', msg);
  busy = true; sbtn.disabled = true; addTyping();
  try {
    const sess = curSess || crypto.randomUUID();
    if (!curSess) { curSess = sess; document.getElementById('iSess').textContent = sess.slice(0,8)+'...'; }
    const resp = await fetch('/api/chat', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:msg,session_id:sess,stream:true}) });
    rmTyping();
    if (resp.headers.get('content-type')?.includes('text/event-stream')) {
      await handleStream(resp);
    } else {
      const data = await resp.json();
      if (data.error) { addMsg('bot','Error: '+data.error,'',false); }
      else {
        let lt = '';
        for (const r of data.replies) {
          const m = {satisfaction:'satisfaction',closing:'closing'};
          lt = m[r.type] || 'reply';
          addMsg('bot', r.content, lt, true);
        }
        const s = ctxQR(lt); if (s.length) setTimeout(()=>showQR(s), 800);
        if (data.intent) document.getElementById('iIntent').textContent = data.intent;
        if (data.retry_count !== undefined) document.getElementById('iRetry').textContent = data.retry_count;
        if (data.emotion) {
          const em = {neutral:'😐',angry:'😠',sad:'😢',anxious:'😰',happy:'😊'};
          document.getElementById('iEmo').textContent = (em[data.emotion]||'😐')+' '+data.emotion+(data.emotion_intensity?'('+data.emotion_intensity+'/5)':'');
          updEmBar(data.emotion, data.emotion_intensity);
        }
        document.getElementById('iStat').textContent = data.interrupted ? 'Escalated' : 'Active';
      }
    }
  } catch(e) { rmTyping(); addMsg('bot','连接错误: '+e.message,''); }
  busy = false; sbtn.disabled = false; inp.focus();
}

/* Stream */
async function handleStream(resp) {
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = '', full = '', lt = 'reply', meta = null;
  const div = addMsg('bot','', 'reply', false);
  const bub = div.querySelector('.bubble');
  bub.classList.add('typing-cursor');
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\n'); buf = lines.pop() || '';
      for (const ln of lines) {
        if (!ln.startsWith('data: ')) continue;
        try {
          const d = JSON.parse(ln.slice(6));
          if (d.done) { meta = d; bub.classList.remove('typing-cursor'); break; }
          else if (d.progress === 'analyzing') bub.textContent = '🤔 分析中...';
          else if (d.token !== undefined) { full += d.token; bub.textContent = full; chat.scrollTop = chat.scrollHeight; }
        } catch(e) {}
      }
    }
  } finally { reader.releaseLock(); }
  if (meta) {
    lt = meta.reply_type || 'reply';
    bub.className = 'bubble ' + lt;
    if (meta.intent) document.getElementById('iIntent').textContent = meta.intent;
    if (meta.emotion) {
      const em = {neutral:'😐',angry:'😠',sad:'😢',anxious:'😰',happy:'😊'};
      document.getElementById('iEmo').textContent = (em[meta.emotion]||'😐')+' '+meta.emotion+(meta.emotion_intensity?'('+meta.emotion_intensity+'/5)':'');
      updEmBar(meta.emotion, meta.emotion_intensity);
    }
    const s = ctxQR(lt); if (s.length) setTimeout(()=>showQR(s), 800);
  }
}

/* Session */
function newSession() {
  curSess = crypto.randomUUID();
  document.getElementById('iSess').textContent = curSess.slice(0,8)+'...';
  document.getElementById('iIntent').textContent = '-';
  document.getElementById('iRetry').textContent = '0';
  document.getElementById('iEmo').textContent = '-';
  document.getElementById('emBar').innerHTML = '';
  document.getElementById('iStat').textContent = 'Active';
  addSys('新会话已启动');
  setTimeout(() => {
    addMsg('bot', '👋 您好！我是智能客服助手。\n\n可以帮您：\n• 📦 产品咨询\n• 🔧 故障排查\n• 💰 价格与保修\n• 📞 投诉与建议\n\n请问有什么可以帮您的？', 'reply', true);
    setTimeout(() => showQR(['产品怎么用？','价格是多少？','我要投诉','有保修吗？']), 1200);
  }, 300);
  refreshSessionList();
}
function clearChat() { chat.innerHTML = ''; }
function resetAll() {
  curSess = null; msgCnt = 0; chat.innerHTML = '';
  document.getElementById('iSess').textContent = '-';
  document.getElementById('iIntent').textContent = '-';
  document.getElementById('iRetry').textContent = '0';
  document.getElementById('iMsg').textContent = '0';
  document.getElementById('iStat').textContent = 'Active';
  refreshSessionList();
}
function quickTest(t) { inp.value = t; sendMsg(t); }

/* Export */
async function exportSession() {
  if (!curSess) { addSys('没有活跃会话'); return; }
  document.getElementById('exportContent').textContent = '加载中...';
  document.getElementById('exportModal').classList.add('show');
  try {
    const r = await fetch('/api/export/'+curSess);
    const d = await r.json();
    if (d.error) document.getElementById('exportContent').textContent = '错误: '+d.error;
    else { window._exp = d; document.getElementById('exportContent').textContent = JSON.stringify(d,null,2); }
  } catch(e) { document.getElementById('exportContent').textContent = '网络错误: '+e.message; }
}
function closeModal() { document.getElementById('exportModal').classList.remove('show'); }
function copyExport() { navigator.clipboard.writeText(document.getElementById('exportContent').textContent).then(()=>addSys('已复制')).catch(()=>addSys('复制失败')); }
function dlExport() {
  if (!window._exp) return;
  const b = new Blob([JSON.stringify(window._exp,null,2)],{type:'application/json'});
  const u = URL.createObjectURL(b);
  const a = document.createElement('a'); a.href = u; a.download = 'session-'+(window._exp.session_id||'export')+'.json';
  a.click(); URL.revokeObjectURL(u);
}

/* Emotion bar */
function updEmBar(em, intensity) {
  const bar = document.getElementById('emBar');
  if (!bar || !intensity) { bar.innerHTML = ''; return; }
  let h = '';
  for (let i = 1; i <= 5; i++) h += '<i class="'+(i<=intensity?(intensity>=4?'on hi':'on'):'')+'"></i>';
  bar.innerHTML = h;
}

/* Reload KB */
async function reloadKB() {
  try {
    const r = await fetch('/api/rag/reload');
    const d = await r.json();
    if (d.error) addSys('重载失败: '+d.error);
    else addSys('✅ 知识库已重载: '+d.documents+' 文档, '+d.sections+' 章节');
  } catch(e) { addSys('网络错误: '+e.message); }
}

/* Full flow */
async function runFullFlow() {
  clearChat(); newSession();
  await new Promise(r => setTimeout(r, 500));
  const steps = [
    {msg:'产品怎么用？',label:'步骤1：咨询'},
    {msg:'谢谢，没问题了',label:'步骤2：结束'},
    {msg:'满意',label:'步骤3：反馈'},
  ];
  for (const s of steps) {
    addSys(s.label);
    await new Promise(r => setTimeout(r, 500));
    inp.value = s.msg;
    await sendMsg(s.msg);
    while (busy) await new Promise(r => setTimeout(r, 200));
    await new Promise(r => setTimeout(r, 1500));
  }
  addSys('演示完成！');
}

/* Session list (sidebar) */
async function refreshSessionList() {
  const el = document.getElementById('sessionList');
  try {
    const r = await fetch('/api/sessions');
    const d = await r.json();
    if (!d.sessions || !d.sessions.length) { el.innerHTML = '<div style="padding:8px 10px;font-size:12px;color:#6e7681">暂无会话</div>'; return; }
    let html = '';
    for (const s of d.sessions) {
      const active = curSess && s.session_id === curSess;
      html += '<div class="nav-item'+(active?' active':'')+'" onclick="switchSess(\''+s.session_id+'\')"><span class="ico">💬</span>'+s.session_id.slice(0,8)+'...</div>';
    }
    el.innerHTML = html;
  } catch(e) {}
}

/* Session dropdown (toolbar) */
async function toggleSessionPanel() {
  const p = document.getElementById('sessPanel');
  if (p.classList.contains('show')) { p.classList.remove('show'); return; }
  p.classList.add('show');
  p.innerHTML = '<div class="sess-empty">加载中...</div>';
  try {
    const r = await fetch('/api/sessions');
    const d = await r.json();
    if (!d.sessions || !d.sessions.length) { p.innerHTML = '<div class="sess-empty">暂无历史</div>'; return; }
    let h = '';
    for (const s of d.sessions) {
      const t = s.last_activity ? new Date(s.last_activity).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
      h += '<div class="sess-item" onclick="switchSess(\''+s.session_id+'\')"><div class="sid">'+s.session_id.slice(0,8)+'...</div><div class="prev">'+(s.preview||'无消息')+'</div><div class="mt">'+s.message_count+' 条 · '+t+'</div></div>';
    }
    p.innerHTML = h;
  } catch(e) { p.innerHTML = '<div class="sess-empty">网络错误</div>'; }
}
async function switchSess(sid) {
  document.getElementById('sessPanel').classList.remove('show');
  if (sid === curSess) return;
  try {
    const r = await fetch('/api/session/'+sid);
    const d = await r.json();
    clearChat(); msgCnt = 0; botIdx = 0; curSess = sid;
    document.getElementById('iSess').textContent = sid.slice(0,8)+'...';
    if (d.error) { addSys('加载失败: '+d.error); return; }
    for (const m of d.messages || []) {
      const role = m.role === 'user' ? 'user' : 'bot';
      let mt = '';
      if (role === 'bot') {
        if ((m.content||'').includes('满意')) mt = 'satisfaction';
        else if (/再见|goodbye|祝您/.test(m.content||'')) mt = 'closing';
      }
      addMsg(role, m.content, mt, false);
    }
    if (d.intent) document.getElementById('iIntent').textContent = d.intent;
    document.getElementById('iMsg').textContent = (d.messages||[]).length;
    addSys('已切换会话');
    chat.scrollTop = chat.scrollHeight;
    refreshSessionList();
  } catch(e) { addSys('切换失败: '+e.message); }
}
document.addEventListener('click', e => {
  const p = document.getElementById('sessPanel');
  if (p && !e.target.closest('.sess-dd')) p.classList.remove('show');
});

/* Init */
window.addEventListener('DOMContentLoaded', () => { if (!curSess) newSession(); refreshSessionList(); });
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
        elif self.path == '/api/sessions':
            # GET /api/sessions - list all sessions from memory DB with summary
            try:
                from agent.memory import _get_connection
                conn = _get_connection()

                # Get distinct session IDs with their conversation counts and last activity
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

                result = {'sessions': sessions, 'total': len(sessions)}
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
        # Rate limit all POST endpoints except /api/rating (fire-and-forget)
        if self.path != '/api/rating' and not self._check_rate_limit():
            return

        _request_counter["total"] += 1
        start_time = time.time()

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
        elif self.path == '/api/chat':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            user_message = data.get('message', '')
            session_id = data.get('session_id', str(uuid4()))
            stream = data.get('stream', False)

            if stream:
                # SSE streaming response
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'keep-alive')
                self.send_header('X-Accel-Buffering', 'no')
                self.end_headers()
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
    server = HTTPServer(('0.0.0.0', PORT), ChatHandler)
    print(f"[Server] Running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
