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
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>智联科技 · 智能客服</title>
<style>
  /* ═══════════════════════════════════════════════════════════
     Design System — CSS Variables
     ═══════════════════════════════════════════════════════════ */
  :root {
    --accent: #6366f1;
    --accent-hover: #4f46e5;
    --accent-glow: rgba(99,102,241,0.15);
    --accent-soft: rgba(99,102,241,0.08);
    --bg-app: #f8fafc;
    --bg-card: #ffffff;
    --bg-elevated: #ffffff;
    --bg-inset: #f1f5f9;
    --bg-overlay: rgba(15,23,42,0.5);
    --text-1: #0f172a;
    --text-2: #475569;
    --text-3: #94a3b8;
    --text-on-accent: #ffffff;
    --border: #e2e8f0;
    --border-light: #f1f5f9;
    --success: #10b981;
    --success-bg: #ecfdf5;
    --warning: #f59e0b;
    --warning-bg: #fffbeb;
    --danger: #ef4444;
    --danger-bg: #fef2f2;
    --shadow-sm: 0 1px 2px rgba(0,0,0,0.05);
    --shadow-md: 0 4px 12px rgba(0,0,0,0.08);
    --shadow-lg: 0 8px 24px rgba(0,0,0,0.12);
    --bot-bg: #ffffff;
    --bot-border: #e2e8f0;
    --scrollbar: #cbd5e1;
    --scrollbar-hover: #94a3b8;
    --r-sm: 8px;
    --r-md: 12px;
    --r-lg: 16px;
    --r-xl: 20px;
    --r-full: 9999px;
    --ease: cubic-bezier(0.4,0,0.2,1);
  }
  [data-theme="dark"] {
    --accent: #818cf8;
    --accent-hover: #6366f1;
    --accent-glow: rgba(129,140,248,0.2);
    --accent-soft: rgba(129,140,248,0.1);
    --bg-app: #0b1120;
    --bg-card: #131c31;
    --bg-elevated: #182240;
    --bg-inset: #0f1729;
    --text-1: #f1f5f9;
    --text-2: #94a3b8;
    --text-3: #64748b;
    --border: #1e293b;
    --border-light: #1e293b;
    --success: #34d399;
    --success-bg: #052e16;
    --warning: #fbbf24;
    --warning-bg: #422006;
    --danger: #f87171;
    --danger-bg: #450a0a;
    --shadow-sm: 0 1px 2px rgba(0,0,0,0.2);
    --shadow-md: 0 4px 12px rgba(0,0,0,0.3);
    --shadow-lg: 0 8px 24px rgba(0,0,0,0.4);
    --bot-bg: #182240;
    --bot-border: #1e293b;
    --scrollbar: #334155;
    --scrollbar-hover: #475569;
  }

  *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
  html { height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
    background: var(--bg-app);
    color: var(--text-1);
    height: 100%;
    display: flex;
    flex-direction: column;
    transition: background 0.3s var(--ease), color 0.3s var(--ease);
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
  }

  /* ── Header ────────────────────────────────────────── */
  .header {
    background: var(--bg-card);
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    height: 56px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-shrink: 0;
    transition: background 0.3s var(--ease);
    z-index: 10;
  }
  .header-left { display: flex; align-items: center; gap: 12px; }
  .logo {
    width: 32px; height: 32px;
    background: linear-gradient(135deg, var(--accent), #a78bfa);
    border-radius: var(--r-sm);
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; color: white; flex-shrink: 0;
  }
  .header-title { font-size: 15px; font-weight: 600; color: var(--text-1); letter-spacing: -0.01em; }
  .header-subtitle { font-size: 11px; color: var(--text-3); margin-top: 1px; }
  .header-right { display: flex; align-items: center; gap: 6px; }
  .status-pill {
    display: flex; align-items: center; gap: 6px;
    padding: 4px 10px;
    background: var(--success-bg);
    color: var(--success);
    border-radius: var(--r-full);
    font-size: 12px; font-weight: 500;
  }
  .status-pill .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--success);
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  .icon-btn {
    width: 32px; height: 32px;
    border-radius: var(--r-sm);
    border: 1px solid var(--border);
    background: var(--bg-card);
    color: var(--text-2);
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px;
    transition: all 0.15s var(--ease);
  }
  .icon-btn:hover {
    background: var(--bg-inset);
    color: var(--text-1);
    border-color: var(--accent);
  }

  /* ── Toolbar ───────────────────────────────────────── */
  .toolbar {
    background: var(--bg-card);
    padding: 8px 24px;
    display: flex; gap: 8px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    transition: background 0.3s var(--ease);
    overflow-x: auto;
    scrollbar-width: none;
  }
  .toolbar::-webkit-scrollbar { display: none; }
  .btn {
    padding: 6px 14px;
    border-radius: var(--r-full);
    border: 1px solid var(--border);
    background: var(--bg-card);
    color: var(--text-2);
    font-size: 12px; font-weight: 500;
    cursor: pointer;
    transition: all 0.15s var(--ease);
    white-space: nowrap;
    display: flex; align-items: center; gap: 5px;
  }
  .btn:hover {
    border-color: var(--accent);
    color: var(--accent);
    background: var(--accent-soft);
  }
  .btn.primary { background: var(--accent); color: var(--text-on-accent); border-color: var(--accent); }
  .btn.primary:hover { background: var(--accent-hover); }
  .btn.danger { color: var(--danger); border-color: var(--danger); }
  .btn.danger:hover { background: var(--danger-bg); }
  .btn .ico { font-size: 13px; }

  /* ── Test Strip ────────────────────────────────────── */
  .test-strip {
    background: var(--bg-inset);
    padding: 8px 24px;
    display: flex; gap: 6px; align-items: center;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    overflow-x: auto;
    scrollbar-width: none;
    transition: background 0.3s var(--ease);
  }
  .test-strip::-webkit-scrollbar { display: none; }
  .test-strip .group-label {
    font-size: 11px; color: var(--text-3);
    font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; margin-right: 2px; flex-shrink: 0;
  }
  .test-strip .divider { width: 1px; height: 16px; background: var(--border); margin: 0 4px; flex-shrink: 0; }
  .pill {
    padding: 4px 12px;
    border-radius: var(--r-full);
    border: 1px solid var(--border);
    background: var(--bg-card);
    color: var(--text-2);
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s var(--ease);
    white-space: nowrap;
  }
  .pill:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
  .pill.warn { border-color: var(--warning); color: #b45309; }
  .pill.warn:hover { background: var(--warning-bg); }
  .pill.danger { border-color: var(--danger); color: #b91c1c; }
  .pill.danger:hover { background: var(--danger-bg); }
  .pill.success { border-color: var(--success); color: #047857; }
  .pill.success:hover { background: var(--success-bg); }
  .pill.accent { border-color: var(--accent); color: var(--accent); }
  .pill.accent:hover { background: var(--accent-soft); }
  [data-theme="dark"] .pill.warn { color: var(--warning); }
  [data-theme="dark"] .pill.danger { color: var(--danger); }
  [data-theme="dark"] .pill.success { color: var(--success); }

  /* ── Chat Area ─────────────────────────────────────── */
  .chat-area {
    flex: 1; overflow-y: auto; overflow-x: hidden;
    padding: 24px;
    display: flex; flex-direction: column; gap: 20px;
    scroll-behavior: smooth;
  }
  .chat-area::-webkit-scrollbar { width: 5px; }
  .chat-area::-webkit-scrollbar-track { background: transparent; }
  .chat-area::-webkit-scrollbar-thumb { background: var(--scrollbar); border-radius: 3px; }
  .chat-area::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-hover); }

  .msg-row {
    display: flex; gap: 10px;
    max-width: 720px; width: 100%;
    margin: 0 auto;
    animation: msgIn 0.35s var(--ease);
  }
  @keyframes msgIn {
    from { opacity: 0; transform: translateY(12px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .msg-row.user { flex-direction: row-reverse; align-self: flex-end; }
  .msg-row.bot { align-self: flex-start; }

  .msg-avatar {
    width: 34px; height: 34px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 15px; flex-shrink: 0; margin-top: 2px;
  }
  .msg-row.user .msg-avatar { background: linear-gradient(135deg, var(--accent), #818cf8); color: white; }
  .msg-row.bot .msg-avatar { background: linear-gradient(135deg, #f472b6, #c084fc); color: white; }

  .msg-body { display: flex; flex-direction: column; gap: 4px; min-width: 0; }

  .bubble {
    padding: 10px 16px;
    border-radius: var(--r-lg);
    font-size: 14px; line-height: 1.65;
    white-space: pre-wrap; word-break: break-word;
    transition: background 0.3s var(--ease);
  }
  .msg-row.user .bubble { background: var(--accent); color: white; border-bottom-right-radius: var(--r-sm); }
  .msg-row.bot .bubble {
    background: var(--bot-bg); color: var(--text-1);
    border: 1px solid var(--bot-border);
    border-bottom-left-radius: var(--r-sm);
  }
  .msg-row.bot .bubble.satisfaction { border-color: var(--warning); background: var(--warning-bg); }
  .msg-row.bot .bubble.closing { border-color: var(--success); background: var(--success-bg); }

  .msg-meta { display: flex; align-items: center; gap: 8px; padding: 0 4px; }
  .msg-row.user .msg-meta { justify-content: flex-end; }
  .msg-time { font-size: 11px; color: var(--text-3); }
  .msg-readtime { font-size: 11px; color: var(--text-3); }
  .msg-copy {
    font-size: 11px; color: var(--text-3);
    background: none; border: none; cursor: pointer;
    padding: 1px 4px; border-radius: 4px;
    transition: all 0.15s; opacity: 0;
  }
  .msg-row:hover .msg-copy { opacity: 1; }
  .msg-copy:hover { background: var(--accent-soft); color: var(--accent); }

  .sys-msg {
    align-self: center;
    padding: 5px 14px;
    background: var(--bg-inset);
    border-radius: var(--r-full);
    font-size: 12px; color: var(--text-3);
    animation: msgIn 0.3s var(--ease);
    max-width: 720px;
  }

  .quick-replies {
    display: flex; gap: 6px; flex-wrap: wrap;
    max-width: 720px; width: 100%;
    margin: 0 auto; padding-left: 44px;
    animation: msgIn 0.3s var(--ease);
  }
  .qr-btn {
    padding: 5px 14px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--r-full);
    font-size: 13px; color: var(--text-2);
    cursor: pointer;
    transition: all 0.15s var(--ease);
  }
  .qr-btn:hover {
    border-color: var(--accent); color: var(--accent);
    background: var(--accent-soft);
    transform: translateY(-1px);
  }

  .typing-dots { display: flex; gap: 5px; padding: 14px 18px; align-items: center; }
  .typing-dots span {
    width: 7px; height: 7px;
    background: var(--text-3); border-radius: 50%;
    animation: dotBounce 1.4s infinite;
  }
  .typing-dots span:nth-child(2) { animation-delay: 0.2s; }
  .typing-dots span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes dotBounce {
    0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
    30% { transform: translateY(-6px); opacity: 1; }
  }

  .typing-cursor::after {
    content: '\25ca';
    animation: cursorBlink 0.8s infinite;
    color: var(--accent); font-weight: 300; margin-left: 1px;
  }
  @keyframes cursorBlink { 0%, 50% { opacity: 1; } 51%, 100% { opacity: 0; } }

  .emotion-bar { display: inline-flex; gap: 2px; vertical-align: middle; }
  .emotion-bar .seg {
    width: 6px; height: 12px; border-radius: 2px;
    background: var(--border);
    transition: background 0.3s var(--ease);
  }
  .emotion-bar .seg.active { background: var(--accent); }
  .emotion-bar .seg.high { background: var(--danger); }

  .star-row {
    display: flex; gap: 2px; align-items: center;
    margin-top: 6px; padding-left: 4px;
    animation: msgIn 0.3s var(--ease);
  }
  .star-row .label { font-size: 11px; color: var(--text-3); margin-right: 6px; }
  .star-btn {
    background: none; border: none; cursor: pointer;
    font-size: 16px; padding: 1px 3px;
    transition: transform 0.15s var(--ease);
    filter: grayscale(1) opacity(0.3);
  }
  .star-btn:hover { transform: scale(1.25); filter: grayscale(0) opacity(1); }
  .star-btn.on { filter: grayscale(0) opacity(1); }
  .star-thanks {
    font-size: 12px; color: var(--success);
    margin-top: 4px; padding-left: 4px;
    animation: msgIn 0.3s var(--ease);
  }

  /* ── Info Bar ──────────────────────────────────────── */
  .info-bar {
    background: var(--bg-card);
    border-top: 1px solid var(--border);
    padding: 6px 24px;
    font-size: 11px; color: var(--text-3);
    display: flex; gap: 16px;
    overflow-x: auto; scrollbar-width: none;
    flex-shrink: 0;
    transition: background 0.3s var(--ease);
  }
  .info-bar::-webkit-scrollbar { display: none; }
  .info-item { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
  .info-label { color: var(--text-3); font-weight: 500; }
  .info-value { color: var(--text-2); font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; font-size: 11px; }

  /* ── Input Area ────────────────────────────────────── */
  .input-area {
    background: var(--bg-card);
    border-top: 1px solid var(--border);
    padding: 12px 24px 16px;
    display: flex; gap: 10px; align-items: flex-end;
    flex-shrink: 0;
    transition: background 0.3s var(--ease);
  }
  .input-wrap { flex: 1; display: flex; flex-direction: column; gap: 4px; }
  .input-wrap input {
    width: 100%;
    padding: 10px 16px;
    border: 1px solid var(--border);
    border-radius: var(--r-full);
    font-size: 14px; outline: none;
    background: var(--bg-inset); color: var(--text-1);
    transition: all 0.2s var(--ease);
  }
  .input-wrap input:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-glow);
    background: var(--bg-card);
  }
  .input-wrap input::placeholder { color: var(--text-3); }
  .input-hint { font-size: 11px; color: var(--text-3); text-align: center; }
  .send-btn {
    padding: 10px 20px;
    background: var(--accent); color: white;
    border: none; border-radius: var(--r-full);
    font-size: 13px; font-weight: 600;
    cursor: pointer;
    transition: all 0.15s var(--ease);
    white-space: nowrap; flex-shrink: 0;
  }
  .send-btn:hover { background: var(--accent-hover); transform: translateY(-1px); }
  .send-btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

  /* ── Scroll Button ─────────────────────────────────── */
  .scroll-btn {
    position: fixed; bottom: 90px; right: 28px;
    width: 36px; height: 36px; border-radius: 50%;
    background: var(--bg-card); color: var(--text-2);
    border: 1px solid var(--border);
    cursor: pointer; font-size: 16px;
    display: none; align-items: center; justify-content: center;
    box-shadow: var(--shadow-md);
    transition: all 0.2s var(--ease); z-index: 50;
  }
  .scroll-btn:hover { color: var(--accent); border-color: var(--accent); transform: translateY(-2px); }
  .scroll-btn.show { display: flex; }

  /* ── Modal ─────────────────────────────────────────── */
  .modal-overlay {
    display: none; position: fixed; inset: 0;
    background: var(--bg-overlay); z-index: 100;
    align-items: center; justify-content: center;
    backdrop-filter: blur(4px);
  }
  .modal-overlay.show { display: flex; }
  .modal {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    padding: 24px;
    max-width: 700px; width: 92%; max-height: 80vh;
    overflow-y: auto; box-shadow: var(--shadow-lg);
  }
  .modal h3 {
    font-size: 16px; font-weight: 600; color: var(--text-1);
    margin-bottom: 14px; display: flex; align-items: center; gap: 8px;
  }
  .modal pre {
    background: var(--bg-inset);
    border: 1px solid var(--border);
    padding: 14px; border-radius: var(--r-sm);
    font-size: 12px;
    font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
    overflow-x: auto; max-height: 50vh;
    color: var(--text-1);
    white-space: pre-wrap; word-break: break-all; line-height: 1.5;
  }
  .modal-btns { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }

  /* ── Session Switcher ──────────────────────────────── */
  .session-dd { position: relative; display: inline-block; }
  .session-panel {
    display: none; position: absolute;
    right: 0; top: calc(100% + 6px);
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    box-shadow: var(--shadow-lg);
    max-height: 320px; overflow-y: auto;
    min-width: 300px; z-index: 200;
  }
  .session-panel.show { display: block; }
  .session-item {
    padding: 10px 14px; cursor: pointer;
    border-bottom: 1px solid var(--border-light);
    transition: background 0.1s; color: var(--text-1);
  }
  .session-item:hover { background: var(--accent-soft); }
  .session-item:last-child { border-bottom: none; }
  .session-item .sid {
    font-size: 11px; color: var(--text-3);
    font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
  }
  .session-item .preview {
    font-size: 13px; color: var(--text-2); margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 270px;
  }
  .session-item .meta { font-size: 11px; color: var(--text-3); margin-top: 2px; }
  .session-empty { padding: 20px; text-align: center; color: var(--text-3); font-size: 13px; }

  /* ── Responsive ────────────────────────────────────── */
  @media (max-width: 640px) {
    .header { padding: 0 16px; height: 50px; }
    .header-subtitle { display: none; }
    .toolbar { padding: 6px 16px; }
    .test-strip { padding: 6px 16px; }
    .chat-area { padding: 16px; gap: 16px; }
    .msg-row { max-width: 100%; }
    .msg-row.user, .msg-row.bot { align-self: stretch; }
    .msg-avatar { width: 28px; height: 28px; font-size: 13px; }
    .bubble { padding: 8px 14px; font-size: 13px; }
    .quick-replies { padding-left: 38px; }
    .input-area { padding: 10px 16px 14px; }
    .info-bar { padding: 5px 16px; gap: 12px; font-size: 10px; }
    .scroll-btn { right: 16px; bottom: 80px; }
  }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="logo">🤖</div>
    <div>
      <div class="header-title">智联科技 · 智能客服</div>
      <div class="header-subtitle">LangGraph Agent · 本地 LLM</div>
    </div>
  </div>
  <div class="header-right">
    <div class="status-pill"><span class="dot"></span>在线</div>
    <div class="session-dd">
      <button class="icon-btn" onclick="toggleSessionDropdown()" title="历史会话">📂</button>
      <div class="session-panel" id="sessionDropdown"></div>
    </div>
    <button class="icon-btn" onclick="toggleTheme()" id="themeBtn" title="切换主题">🌙</button>
  </div>
</div>

<div class="toolbar">
  <button class="btn primary" onclick="newSession()"><span class="ico">＋</span> 新会话</button>
  <button class="btn" onclick="clearChat()"><span class="ico">🗑</span> 清空</button>
  <button class="btn danger" onclick="resetAll()"><span class="ico">↻</span> 重置</button>
  <button class="btn" onclick="toggleSessionDropdown()"><span class="ico">📂</span> 历史</button>
  <button class="btn" onclick="reloadKB()"><span class="ico">🔄</span> 重载知识库</button>
</div>

<div class="test-strip">
  <span class="group-label">测试</span>
  <button class="pill accent" onclick="quickTest('产品怎么用？')">产品咨询</button>
  <button class="pill danger" onclick="quickTest('我要投诉，产品质量太差了')">投诉</button>
  <button class="pill" onclick="quickTest('你好')">打招呼</button>
  <div class="divider"></div>
  <span class="group-label">结束</span>
  <button class="pill success" onclick="quickTest('谢谢，没问题了')">感谢并结束</button>
  <button class="pill danger" onclick="quickTest('再见')">说再见</button>
  <div class="divider"></div>
  <span class="group-label">反馈</span>
  <button class="pill success" onclick="quickTest('满意')">满意</button>
  <button class="pill danger" onclick="quickTest('不满意')">不满意</button>
  <div class="divider"></div>
  <button class="pill accent" onclick="runFullFlow()">▶ 自动流程</button>
  <button class="pill accent" onclick="exportSession()">📋 导出</button>
</div>

<div class="chat-area" id="chatArea"></div>
<button class="scroll-btn" id="scrollBtn" onclick="scrollToBottom()">↓</button>

<div class="info-bar">
  <div class="info-item"><span class="info-label">会话</span> <span class="info-value" id="infoSession">-</span></div>
  <div class="info-item"><span class="info-label">意图</span> <span class="info-value" id="infoIntent">-</span></div>
  <div class="info-item"><span class="info-label">重试</span> <span class="info-value" id="infoRetries">0</span></div>
  <div class="info-item"><span class="info-label">情绪</span> <span id="infoEmotion">-</span> <span id="emotionBar" class="emotion-bar"></span></div>
  <div class="info-item"><span class="info-label">消息</span> <span class="info-value" id="infoMessages">0</span></div>
  <div class="info-item"><span class="info-label">状态</span> <span class="info-value" id="infoStatus">Active</span></div>
</div>

<div class="input-area">
  <div class="input-wrap">
    <input type="text" id="messageInput" placeholder="输入消息..." autocomplete="off" />
    <div class="input-hint">Enter 发送 · Ctrl+Enter 换行</div>
  </div>
  <button class="send-btn" id="sendBtn" onclick="sendMessage()">发送</button>
</div>

<div class="modal-overlay" id="exportModal">
  <div class="modal">
    <h3>📋 会话导出</h3>
    <pre id="exportContent">加载中...</pre>
    <div class="modal-btns">
      <button class="btn" onclick="copyExport()">📋 复制</button>
      <button class="btn" onclick="downloadExport()">💾 下载 JSON</button>
      <button class="btn primary" onclick="closeExportModal()">关闭</button>
    </div>
  </div>
</div>

<script>
let currentSession = null;
let isProcessing = false;
let messageCount = 0;
let botMessageIndex = 0;

const QUICK_REPLIES = {
  default: ['产品怎么用？', '我要投诉', '价格是多少？', '有保修吗？'],
  after_greeting: ['产品怎么用？', '价格是多少？', '有什么功能？'],
  after_reply: ['能详细说说吗？', '还有其他问题', '谢谢，没问题了'],
  satisfaction: ['满意', '不满意'],
};

function toggleTheme() {
  const html = document.documentElement;
  const btn = document.getElementById('themeBtn');
  if (html.getAttribute('data-theme') === 'dark') {
    html.removeAttribute('data-theme');
    btn.textContent = '🌙';
    localStorage.setItem('theme', 'light');
  } else {
    html.setAttribute('data-theme', 'dark');
    btn.textContent = '☀️';
    localStorage.setItem('theme', 'dark');
  }
}
(function() {
  const saved = localStorage.getItem('theme');
  if (saved === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    setTimeout(() => { const b = document.getElementById('themeBtn'); if (b) b.textContent = '☀️'; }, 0);
  }
})();

const chatArea = document.getElementById('chatArea');
const messageInput = document.getElementById('messageInput');
const sendBtn = document.getElementById('sendBtn');
const scrollBtn = document.getElementById('scrollBtn');

chatArea.addEventListener('scroll', () => {
  const near = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight < 100;
  scrollBtn.classList.toggle('show', !near);
});
function scrollToBottom() { chatArea.scrollTo({ top: chatArea.scrollHeight, behavior: 'smooth' }); }
messageInput.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !isProcessing) sendMessage(); });

function addMessage(role, content, type, animate) {
  messageCount++;
  document.getElementById('infoMessages').textContent = messageCount;
  const row = document.createElement('div');
  row.className = 'msg-row ' + role;
  const avatar = document.createElement('div');
  avatar.className = 'msg-avatar';
  avatar.textContent = role === 'user' ? '\u{1F464}' : '\u{1F916}';
  const body = document.createElement('div');
  body.className = 'msg-body';
  const bubble = document.createElement('div');
  bubble.className = 'bubble ' + (type || '');
  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  const ts = document.createElement('span');
  ts.className = 'msg-time';
  ts.textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  meta.appendChild(ts);

  if (role === 'bot' && content) {
    const hasCN = /[\u4e00-\u9fff]/.test(content);
    const rt = hasCN ? Math.ceil(content.length / 5) + '秒' : Math.ceil(content.split(/\s+/).filter(w=>w).length / 2.7) + 's';
    const rtS = document.createElement('span');
    rtS.className = 'msg-readtime';
    rtS.textContent = rt;
    meta.appendChild(rtS);
    const cp = document.createElement('button');
    cp.className = 'msg-copy';
    cp.textContent = '复制';
    cp.onclick = () => {
      navigator.clipboard.writeText(bubble.textContent)
        .then(() => { cp.textContent = '✓'; setTimeout(() => cp.textContent = '复制', 1500); }).catch(() => {});
    };
    meta.appendChild(cp);
  }

  body.appendChild(bubble);
  body.appendChild(meta);
  row.appendChild(avatar);
  row.appendChild(body);
  chatArea.appendChild(row);
  chatArea.scrollTop = chatArea.scrollHeight;
  removeQuickReplies();

  if (animate && role === 'bot') {
    const hasCN = /[\u4e00-\u9fff]/.test(content);
    typeWriter(bubble, content, hasCN ? 35 : 20);
  } else { bubble.textContent = content; }

  if (role === 'bot' && type !== 'satisfaction' && type !== 'closing') {
    botMessageIndex++;
    addStarRating(body, botMessageIndex);
  }
  return row;
}

function typeWriter(el, text, speed) {
  el.classList.add('typing-cursor');
  let i = 0;
  function type() {
    if (i < text.length) {
      el.textContent = text.substring(0, i + 1);
      i++;
      chatArea.scrollTop = chatArea.scrollHeight;
      setTimeout(type, speed);
    } else { el.classList.remove('typing-cursor'); }
  }
  type();
}

function addSystemMsg(text) {
  const d = document.createElement('div');
  d.className = 'sys-msg';
  d.textContent = text;
  chatArea.appendChild(d);
  chatArea.scrollTop = chatArea.scrollHeight;
}

function addTyping() {
  const row = document.createElement('div');
  row.className = 'msg-row bot';
  row.id = 'typingIndicator';
  row.innerHTML = '<div class="msg-avatar">\u{1F916}</div><div class="msg-body"><div class="bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div></div>';
  chatArea.appendChild(row);
  chatArea.scrollTop = chatArea.scrollHeight;
}
function removeTyping() { const e = document.getElementById('typingIndicator'); if (e) e.remove(); }

function showQuickReplies(replies) {
  removeQuickReplies();
  const c = document.createElement('div');
  c.className = 'quick-replies';
  c.id = 'qrContainer';
  for (const t of replies) {
    const b = document.createElement('button');
    b.className = 'qr-btn';
    b.textContent = t;
    b.onclick = () => quickTest(t);
    c.appendChild(b);
  }
  chatArea.appendChild(c);
  chatArea.scrollTop = chatArea.scrollHeight;
}
function removeQuickReplies() { const e = document.getElementById('qrContainer'); if (e) e.remove(); }
function getContextualQuickReplies(lastType) {
  if (lastType === 'satisfaction') return QUICK_REPLIES.satisfaction;
  if (lastType === 'closing') return [];
  if (lastType === 'reply') return QUICK_REPLIES.after_reply;
  return QUICK_REPLIES.default;
}

function addStarRating(parentEl, idx) {
  const d = document.createElement('div');
  d.className = 'star-row';
  d.id = 'star_' + idx;
  const lbl = document.createElement('span');
  lbl.className = 'label';
  lbl.textContent = '有帮助？';
  d.appendChild(lbl);
  for (let i = 1; i <= 5; i++) {
    const b = document.createElement('button');
    b.className = 'star-btn';
    b.textContent = '⭐';
    b.title = i + ' 星';
    b.onclick = () => submitRating(idx, i, d);
    b.onmouseenter = () => { d.querySelectorAll('.star-btn').forEach((x,j) => { x.style.filter = j < i ? 'grayscale(0) opacity(1)' : 'grayscale(1) opacity(0.3)'; }); };
    b.onmouseleave = () => { const r = d.querySelector('.on'); if (!r) d.querySelectorAll('.star-btn').forEach(x => x.style.filter = 'grayscale(1) opacity(0.3)'); };
    d.appendChild(b);
  }
  parentEl.appendChild(d);
}
function submitRating(idx, stars, d) {
  d.querySelectorAll('.star-btn').forEach((b, j) => { if (j < stars) b.classList.add('on'); b.onclick = null; b.style.cursor = 'default'; });
  const lbl = d.querySelector('.label'); if (lbl) lbl.remove();
  const th = document.createElement('div');
  th.className = 'star-thanks';
  th.textContent = '感谢评价！' + stars + ' ⭐';
  d.replaceWith(th);
  if (currentSession) {
    fetch('/api/rating', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({session_id:currentSession, message_index:idx, stars}) }).catch(()=>{});
  }
}

async function sendMessage(text) {
  if (isProcessing) return;
  const msg = text || messageInput.value.trim();
  if (!msg) return;
  messageInput.value = '';
  addMessage('user', msg);
  isProcessing = true;
  sendBtn.disabled = true;
  addTyping();

  try {
    const session = currentSession || crypto.randomUUID();
    if (!currentSession) {
      currentSession = session;
      document.getElementById('infoSession').textContent = session.slice(0, 8) + '...';
    }
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ message: msg, session_id: session, stream: true })
    });
    removeTyping();

    if (resp.headers.get('content-type') && resp.headers.get('content-type').includes('text/event-stream')) {
      await handleStreamResponse(resp);
    } else {
      const data = await resp.json();
      if (data.error) {
        addMessage('bot', 'Error: ' + data.error, '', false);
      } else {
        let lastType = '';
        for (const r of data.replies) {
          const tm = { satisfaction: 'satisfaction', closing: 'closing' };
          lastType = tm[r.type] || 'reply';
          addMessage('bot', r.content, lastType, true);
        }
        const sugg = getContextualQuickReplies(lastType);
        if (sugg && sugg.length) setTimeout(() => showQuickReplies(sugg), 800);
        if (data.intent) document.getElementById('infoIntent').textContent = data.intent;
        if (data.retry_count !== undefined) document.getElementById('infoRetries').textContent = data.retry_count;
        if (data.emotion) {
          const em = {neutral:'😐',angry:'😠',sad:'😢',anxious:'😰',happy:'😊'};
          document.getElementById('infoEmotion').textContent = (em[data.emotion]||'😐')+' '+data.emotion+(data.emotion_intensity?'('+data.emotion_intensity+'/5)':'');
          updateEmotionBar(data.emotion, data.emotion_intensity);
        }
        document.getElementById('infoStatus').textContent = data.interrupted ? 'Escalated' : 'Active';
      }
    }
  } catch (err) {
    removeTyping();
    addMessage('bot', '连接错误: ' + err.message, '');
  }
  isProcessing = false;
  sendBtn.disabled = false;
  messageInput.focus();
}

async function handleStreamResponse(resp) {
  const reader = resp.body.getReader();
  const dec = new TextDecoder('utf-8');
  let buf = '', full = '', lastType = 'reply', meta = null;
  const div = addMessage('bot', '', 'reply', false);
  const bubble = div.querySelector('.bubble');
  bubble.classList.add('typing-cursor');

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const d = JSON.parse(line.slice(6));
          if (d.done) { meta = d; bubble.classList.remove('typing-cursor'); break; }
          else if (d.progress === 'analyzing') bubble.textContent = '\u{1F914} 分析中...';
          else if (d.token !== undefined) { full += d.token; bubble.textContent = full; chatArea.scrollTop = chatArea.scrollHeight; }
        } catch (e) {}
      }
    }
  } finally { reader.releaseLock(); }

  if (meta) {
    lastType = meta.reply_type || 'reply';
    bubble.className = 'bubble ' + lastType;
    if (meta.intent) document.getElementById('infoIntent').textContent = meta.intent;
    if (meta.emotion) {
      const em = {neutral:'😐',angry:'😠',sad:'😢',anxious:'😰',happy:'😊'};
      document.getElementById('infoEmotion').textContent = (em[meta.emotion]||'😐')+' '+meta.emotion+(meta.emotion_intensity?'('+meta.emotion_intensity+'/5)':'');
      updateEmotionBar(meta.emotion, meta.emotion_intensity);
    }
    const sugg = getContextualQuickReplies(lastType);
    if (sugg && sugg.length) setTimeout(() => showQuickReplies(sugg), 800);
  }
}

function newSession() {
  currentSession = crypto.randomUUID();
  document.getElementById('infoSession').textContent = currentSession.slice(0, 8) + '...';
  document.getElementById('infoIntent').textContent = '-';
  document.getElementById('infoRetries').textContent = '0';
  document.getElementById('infoEmotion').textContent = '-';
  document.getElementById('emotionBar').innerHTML = '';
  document.getElementById('infoStatus').textContent = 'Active';
  addSystemMsg('新会话已启动');
  setTimeout(() => {
    addMessage('bot', '\u{1F44B} 您好！我是智联科技智能客服助手。\n\n我可以帮您：\n• \u{1F4E6} 产品咨询（智能音箱、智能家居、云服务）\n• \u{1F527} 故障排查与技术支援\n• \u{1F4B0} 价格与保修政策\n• \u{1F4DE} 投诉与建议\n\n请问有什么可以帮您的？', 'reply', true);
    setTimeout(() => showQuickReplies(['产品怎么用？', '价格是多少？', '我要投诉', '有保修吗？']), 1200);
  }, 300);
}
function clearChat() { chatArea.innerHTML = ''; }
function resetAll() {
  currentSession = null; messageCount = 0; chatArea.innerHTML = '';
  document.getElementById('infoSession').textContent = '-';
  document.getElementById('infoIntent').textContent = '-';
  document.getElementById('infoRetries').textContent = '0';
  document.getElementById('infoMessages').textContent = '0';
  document.getElementById('infoStatus').textContent = 'Active';
}
function quickTest(text) { messageInput.value = text; sendMessage(text); }

async function exportSession() {
  if (!currentSession) { addSystemMsg('没有活跃的会话可导出'); return; }
  document.getElementById('exportContent').textContent = '加载中...';
  document.getElementById('exportModal').classList.add('show');
  try {
    const r = await fetch('/api/export/' + currentSession);
    const d = await r.json();
    if (d.error) { document.getElementById('exportContent').textContent = '错误: ' + d.error; }
    else { window._exportData = d; document.getElementById('exportContent').textContent = JSON.stringify(d, null, 2); }
  } catch(e) { document.getElementById('exportContent').textContent = '网络错误: ' + e.message; }
}
function closeExportModal() { document.getElementById('exportModal').classList.remove('show'); }
function copyExport() {
  navigator.clipboard.writeText(document.getElementById('exportContent').textContent)
    .then(() => addSystemMsg('已复制到剪贴板')).catch(() => addSystemMsg('复制失败'));
}
function downloadExport() {
  if (!window._exportData) return;
  const blob = new Blob([JSON.stringify(window._exportData, null, 2)], {type:'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = 'session-' + (window._exportData.session_id||'export') + '.json';
  a.click(); URL.revokeObjectURL(url);
  addSystemMsg('已导出为 JSON 文件');
}

function updateEmotionBar(emotion, intensity) {
  const bar = document.getElementById('emotionBar');
  if (!bar || !intensity) { bar.innerHTML = ''; return; }
  let h = '';
  for (let i = 1; i <= 5; i++) {
    const cls = i <= intensity ? (intensity >= 4 ? 'seg active high' : 'seg active') : 'seg';
    h += '<span class="' + cls + '"></span>';
  }
  bar.innerHTML = h;
}

async function reloadKB() {
  try {
    const r = await fetch('/api/rag/reload');
    const d = await r.json();
    if (d.error) addSystemMsg('知识库重载失败: ' + d.error);
    else addSystemMsg('✅ 知识库已重载: ' + d.documents + ' 个文档, ' + d.sections + ' 个章节');
  } catch(e) { addSystemMsg('网络错误: ' + e.message); }
}

async function runFullFlow() {
  clearChat(); newSession();
  await new Promise(r => setTimeout(r, 500));
  const steps = [
    { msg: '产品怎么用？', label: '步骤1：咨询产品用法' },
    { msg: '谢谢，没问题了', label: '步骤2：结束对话' },
    { msg: '满意', label: '步骤3：满意反馈' },
  ];
  for (const s of steps) {
    addSystemMsg(s.label);
    await new Promise(r => setTimeout(r, 500));
    messageInput.value = s.msg;
    await sendMessage(s.msg);
    while (isProcessing) await new Promise(r => setTimeout(r, 200));
    await new Promise(r => setTimeout(r, 1500));
  }
  addSystemMsg('完整流程演示完成！');
}

async function toggleSessionDropdown() {
  const dd = document.getElementById('sessionDropdown');
  if (dd.classList.contains('show')) { dd.classList.remove('show'); return; }
  dd.classList.add('show');
  dd.innerHTML = '<div class="session-empty">加载中...</div>';
  try {
    const r = await fetch('/api/sessions');
    const d = await r.json();
    if (d.error) { dd.innerHTML = '<div class="session-empty">加载失败</div>'; return; }
    if (!d.sessions || !d.sessions.length) { dd.innerHTML = '<div class="session-empty">暂无历史会话</div>'; return; }
    let html = '';
    for (const s of d.sessions) {
      const active = currentSession && s.session_id === currentSession;
      const t = s.last_activity ? new Date(s.last_activity).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
      html += '<div class="session-item" onclick="switchSession(\'' + s.session_id + '\')">' +
        '<div class="sid">' + (active ? '▶ ' : '') + s.session_id.slice(0,8) + '...</div>' +
        '<div class="preview">' + (s.preview||'无消息') + '</div>' +
        '<div class="meta">' + s.message_count + ' 条消息 · ' + t + '</div></div>';
    }
    dd.innerHTML = html;
  } catch(e) { dd.innerHTML = '<div class="session-empty">网络错误</div>'; }
}
async function switchSession(sid) {
  document.getElementById('sessionDropdown').classList.remove('show');
  if (sid === currentSession) return;
  try {
    const r = await fetch('/api/session/' + sid);
    const d = await r.json();
    clearChat(); messageCount = 0; botMessageIndex = 0; currentSession = sid;
    document.getElementById('infoSession').textContent = sid.slice(0,8) + '...';
    if (d.error) { addSystemMsg('加载失败: ' + d.error); return; }
    for (const m of d.messages || []) {
      const role = m.role === 'user' ? 'user' : 'bot';
      let mt = '';
      if (role === 'bot') {
        if ((m.content||'').includes('满意')) mt = 'satisfaction';
        else if (/再见|goodbye|祝您/.test(m.content||'')) mt = 'closing';
      }
      addMessage(role, m.content, mt, false);
    }
    if (d.intent) document.getElementById('infoIntent').textContent = d.intent;
    if (d.retry_count !== undefined) document.getElementById('infoRetries').textContent = d.retry_count;
    document.getElementById('infoMessages').textContent = (d.messages||[]).length;
    addSystemMsg('已切换到会话 ' + sid.slice(0,8) + '...');
    scrollToBottom();
  } catch(e) { addSystemMsg('切换失败: ' + e.message); }
}
document.addEventListener('click', (e) => {
  const dd = document.getElementById('sessionDropdown');
  if (dd && !e.target.closest('.session-dd')) dd.classList.remove('show');
});

window.addEventListener('DOMContentLoaded', () => { if (!currentSession) newSession(); });
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
