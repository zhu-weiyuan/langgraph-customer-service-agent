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
      RATE_LIMIT_REQUESTS  — max requests per window (default: 60)
      RATE_LIMIT_WINDOW    — window size in seconds (default: 60)
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
        print(f"[Streaming 错误] {e}")
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
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LangGraph 智能客服 Agent</title>
<style>
  :root {
    --bg-primary: #f0f2f5;
    --bg-toolbar: white;
    --bg-test-cases: #f9fafb;
    --bg-input-area: white;
    --bg-info-bar: #f8fafc;
    --text-primary: #1f2937;
    --text-secondary: #6b7280;
    --text-muted: #9ca3af;
    --border-color: #e5e7eb;
    --bot-bubble-bg: white;
    --bot-bubble-shadow: rgba(0,0,0,0.1);
    --system-msg-bg: #f3f4f6;
    --system-msg-text: #6b7280;
    --satisfaction-bg: #fef3c7;
    --closing-bg: #dcfce7;
    --quick-reply-bg: white;
    --quick-reply-border: #d1d5db;
    --quick-reply-hover: #f3f4f6;
    --scrollbar-thumb: #d1d5db;
    --scrollbar-thumb-hover: #9ca3af;
  }

  [data-theme="dark"] {
    --bg-primary: #0f172a;
    --bg-toolbar: #1e293b;
    --bg-test-cases: #1e293b;
    --bg-input-area: #1e293b;
    --bg-info-bar: #1e293b;
    --text-primary: #e2e8f0;
    --text-secondary: #94a3b8;
    --text-muted: #64748b;
    --border-color: #334155;
    --bot-bubble-bg: #1e293b;
    --bot-bubble-shadow: rgba(0,0,0,0.3);
    --system-msg-bg: #334155;
    --system-msg-text: #94a3b8;
    --satisfaction-bg: #422006;
    --closing-bg: #14532d;
    --quick-reply-bg: #1e293b;
    --quick-reply-border: #475569;
    --quick-reply-hover: #334155;
    --scrollbar-thumb: #475569;
    --scrollbar-thumb-hover: #64748b;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg-primary); height: 100vh; display: flex; flex-direction: column; transition: background 0.3s; color: var(--text-primary); }
  .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
  .header h1 { font-size: 18px; font-weight: 600; }
  .status { font-size: 13px; opacity: 0.9; display: flex; align-items: center; gap: 8px; }
  .status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #4ade80; }
  .theme-toggle { background: rgba(255,255,255,0.2); border: none; color: white; cursor: pointer; font-size: 16px; padding: 4px 8px; border-radius: 6px; transition: background 0.15s; }
  .theme-toggle:hover { background: rgba(255,255,255,0.3); }
  .toolbar { background: var(--bg-toolbar); padding: 10px 24px; display: flex; gap: 10px; border-bottom: 1px solid var(--border-color); transition: background 0.3s; }
  .toolbar button { padding: 8px 16px; border: 1px solid var(--border-color); background: var(--bg-toolbar); border-radius: 6px; cursor: pointer; font-size: 13px; color: var(--text-primary); transition: all 0.15s; }
  .toolbar button:hover { filter: brightness(0.97); }
  .toolbar button.primary { background: #667eea; color: white; border-color: #667eea; }
  .toolbar button.danger { background: #ef4444; color: white; border-color: #ef4444; }
  .test-cases { background: var(--bg-test-cases); padding: 10px 24px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; border-bottom: 1px solid var(--border-color); transition: background 0.3s; }
  .test-cases .label { font-size: 12px; color: var(--text-secondary); margin-right: 4px; font-weight: 600; }
  .tc-btn { padding: 5px 12px; background: var(--bg-toolbar); border: 1px solid var(--border-color); border-radius: 16px; font-size: 12px; cursor: pointer; transition: all 0.15s; color: var(--text-primary); }
  .tc-btn:hover { filter: brightness(0.97); }
  .tc-btn.green { border-color: #86efac; color: #166534; background: #f0fdf4; }
  .tc-btn.red { border-color: #fca5a5; color: #991b1b; background: #fef2f2; }
  .tc-btn.blue { border-color: #93c5fd; color: #1e40af; background: #eff6ff; }
  [data-theme="dark"] .tc-btn.green { background: #052e16; color: #86efac; }
  [data-theme="dark"] .tc-btn.red { background: #450a0a; color: #fca5a5; }
  [data-theme="dark"] .tc-btn.blue { background: #172554; color: #93c5fd; }
  .chat-container { flex: 1; overflow-y: auto; padding: 20px 24px; display: flex; flex-direction: column; gap: 16px; }
  .message { display: flex; gap: 12px; max-width: 75%; animation: fadeIn 0.3s ease; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  .message.user { align-self: flex-end; flex-direction: row-reverse; }
  .message.bot { align-self: flex-start; }
  .avatar { width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0; }
  .message.user .avatar { background: #667eea; }
  .message.bot .avatar { background: linear-gradient(135deg, #f093fb, #f5576c); }
  .bubble { padding: 12px 16px; border-radius: 16px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
  .message.user .bubble { background: #667eea; color: white; border-bottom-right-radius: 4px; }
  .message.bot .bubble { background: var(--bot-bubble-bg); color: var(--text-primary); border-bottom-left-radius: 4px; box-shadow: 0 1px 3px var(--bot-bubble-shadow); transition: background 0.3s; }
  .message.bot .bubble.satisfaction { background: var(--satisfaction-bg); border: 1px solid #f59e0b; }
  .message.bot .bubble.closing { background: var(--closing-bg); border: 1px solid #22c55e; }
  .system-msg { align-self: center; padding: 6px 16px; background: var(--system-msg-bg); border-radius: 12px; font-size: 12px; color: var(--system-msg-text); transition: background 0.3s; }

  /* Quick reply suggestions */
  .quick-replies { display: flex; gap: 8px; flex-wrap: wrap; align-self: flex-start; margin-left: 48px; animation: fadeIn 0.3s ease; }
  .quick-reply-btn { padding: 6px 14px; background: var(--quick-reply-bg); border: 1px solid var(--quick-reply-border); border-radius: 16px; font-size: 12px; cursor: pointer; transition: all 0.15s; color: var(--text-secondary); }
  .quick-reply-btn:hover { background: var(--quick-reply-hover); border-color: #667eea; color: #667eea; }

  .input-area { background: var(--bg-input-area); padding: 16px 24px; border-top: 1px solid var(--border-color); display: flex; gap: 12px; transition: background 0.3s; }
  .input-area input { flex: 1; padding: 12px 16px; border: 1px solid var(--border-color); border-radius: 24px; font-size: 14px; outline: none; transition: border-color 0.15s; background: var(--bg-toolbar); color: var(--text-primary); }
  .input-area input:focus { border-color: #667eea; box-shadow: 0 0 0 3px rgba(102,126,234,0.1); }
  .input-area button { padding: 12px 24px; background: linear-gradient(135deg, #667eea, #764ba2); color: white; border: none; border-radius: 24px; font-size: 14px; cursor: pointer; }
  .input-area button:hover { opacity: 0.9; }
  .input-area button:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Keyboard shortcut hint */
  .shortcut-hint { font-size: 10px; color: var(--text-muted); text-align: center; padding: 2px 0; }
  .info-bar { background: var(--bg-info-bar); padding: 8px 24px; font-size: 12px; color: var(--text-secondary); display: flex; gap: 20px; border-top: 1px solid var(--border-color); overflow-x: auto; transition: background 0.3s; }
  .info-bar span { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
  .info-bar .label { color: var(--text-muted); }
  .typing-indicator { display: flex; gap: 4px; padding: 12px 16px; align-items: center; }
  .typing-indicator .dot { width: 8px; height: 8px; background: var(--text-muted); border-radius: 50%; animation: bounce 1.4s infinite; }
  .typing-indicator .dot:nth-child(2) { animation-delay: 0.2s; }
  .typing-indicator .dot:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce { 0%, 60%, 100% { transform: translateY(0); } 30% { transform: translateY(-8px); } }

  /* Typing cursor for character-by-character animation */
  .typing-cursor::after {
    content: '▊';
    animation: blink 0.8s infinite;
    color: #667eea;
    font-weight: 300;
  }
  @keyframes blink { 0%, 50% { opacity: 1; } 51%, 100% { opacity: 0; } }

  /* Memory indicator */
  .memory-badge { display: inline-block; font-size: 10px; padding: 2px 6px; background: #e0e7ff; color: #4338ca; border-radius: 8px; margin-left: 8px; }

  /* Mobile responsive */
  @media (max-width: 640px) {
    .header { padding: 12px 16px; }
    .header h1 { font-size: 15px; }
    .toolbar { padding: 8px 16px; gap: 6px; flex-wrap: wrap; }
    .test-cases { padding: 8px 16px; }
    .chat-container { padding: 16px;
      gap: 12px; }
    .message { max-width: 88%; }
    .quick-replies { margin-left: 42px; }
    .input-area { padding: 12px 16px; }
    .info-bar { padding: 6px 16px; font-size: 11px; gap: 12px; }
    .avatar { width: 30px; height: 30px; font-size: 15px; }
    .bubble { padding: 10px 14px; font-size: 13px; }
  }

  /* Emotion intensity bar */
  .emotion-bar { display: inline-flex; gap: 2px; vertical-align: middle; }
  .emotion-bar .seg { width: 8px; height: 14px; border-radius: 2px; background: #d1d5db; transition: background 0.3s; }
  .emotion-bar .seg.active { background: #667eea; }
  .emotion-bar .seg.high { background: #ef4444; }

  /* Session export modal */
  .export-modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 100; align-items: center; justify-content: center; }
  .export-modal.show { display: flex; }
  .export-modal-content { background: var(--bg-toolbar); border-radius: 12px; padding: 24px; max-width: 700px; width: 90%; max-height: 80vh; overflow-y: auto; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
  .export-modal-content h3 { margin-bottom: 12px; color: var(--text-primary); }
  .export-modal-content pre { background: var(--system-msg-bg); padding: 16px; border-radius: 8px; font-size: 12px; overflow-x: auto; max-height: 50vh; color: var(--text-primary); white-space: pre-wrap; word-break: break-all; }
  .export-modal-content .btn-row { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }
  .export-modal-content button { padding: 8px 16px; border-radius: 6px; border: 1px solid var(--border-color); cursor: pointer; background: var(--bg-toolbar); color: var(--text-primary); }
  .export-modal-content button.primary { background: #667eea; color: white; border-color: #667eea; }

  /* Message timestamp */
  .msg-time { font-size: 10px; color: var(--text-muted); margin-top: 4px; display: block; }

  /* Read time estimation */
  .read-time { font-size: 10px; color: var(--text-muted); margin-left: 8px; }

  /* Copy button on bot messages */
  .copy-btn { font-size: 10px; color: var(--text-muted); background: none; border: none; cursor: pointer; padding: 2px 6px; border-radius: 4px; transition: all 0.15s; margin-left: 4px; }
  .copy-btn:hover { background: var(--quick-reply-hover); color: #667eea; }

  /* Inline star rating */
  .star-rating { display: flex; gap: 2px; align-items: center; margin-top: 8px; padding-left: 4px; animation: fadeIn 0.3s ease; }
  .star-rating .label { font-size: 10px; color: var(--text-muted); margin-right: 6px; }
  .star-btn { background: none; border: none; cursor: pointer; font-size: 18px; padding: 2px 4px; transition: transform 0.15s, filter 0.15s; filter: grayscale(1) opacity(0.4); }
  .star-btn:hover { transform: scale(1.3); filter: grayscale(0) opacity(1); }
  .star-btn.rated { filter: grayscale(0) opacity(1); }
  .star-rating.thanks { font-size: 12px; color: #22c55e; margin-top: 4px; padding-left: 4px; animation: fadeIn 0.3s ease; }

  /* Scrollbar styling */
  .chat-container::-webkit-scrollbar { width: 6px; }
  .chat-container::-webkit-scrollbar-track { background: transparent; }
  .chat-container::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb); border-radius: 3px; }
  .chat-container::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-thumb-hover); }

  /* Scroll-to-bottom floating button */
  .scroll-bottom-btn {
    position: fixed;
    bottom: 80px;
    right: 24px;
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: #667eea;
    color: white;
    border: none;
    cursor: pointer;
    font-size: 16px;
    display: none;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    transition: opacity 0.2s, transform 0.2s;
    z-index: 50;
  }
  .scroll-bottom-btn:hover { transform: scale(1.1); }
  .scroll-bottom-btn.show { display: flex; }
</style>
</head>
<body>

<div class="header">
  <h1>LangGraph 智能客服 Agent</h1>
  <div class="status"><span class="dot"></span>在线 (本地 LLM) <button class="theme-toggle" onclick="toggleTheme()" title="切换深色/浅色模式">🌙</button></div>
</div>

<div class="toolbar">
  <button class="primary" onclick="newSession()">新会话</button>
  <button onclick="clearChat()">清空聊天</button>
  <button class="danger" onclick="resetAll()">重置全部</button>
  <button onclick="reloadKB()" title="Hot reload knowledge base">🔄 重载知识库</button>
</div>

<div class="test-cases">
  <span class="label">测试：</span>
  <button class="tc-btn blue" onclick="quickTest('产品怎么用？')">产品咨询</button>
  <button class="tc-btn red" onclick="quickTest('我要投诉，产品质量太差了')">投诉</button>
  <button class="tc-btn" onclick="quickTest('你好')">打招呼</button>
  <span style="width:1px;height:20px;background:#d1d5db;margin:0 4px"></span>
  <span class="label">结束：</span>
  <button class="tc-btn green" onclick="quickTest('谢谢，没问题了')">感谢并结束</button>
  <button class="tc-btn red" onclick="quickTest('再见')">说再见</button>
  <span style="width:1px;height:20px;background:#d1d5db;margin:0 4px"></span>
  <span class="label">反馈：</span>
  <button class="tc-btn green" onclick="quickTest('满意')">满意</button>
  <button class="tc-btn red" onclick="quickTest('不满意')">不满意</button>
  <span style="width:1px;height:20px;background:#d1d5db;margin:0 4px"></span>
  <button class="tc-btn blue" onclick="runFullFlow()">自动完整流程</button>
  <button class="tc-btn blue" onclick="exportSession()">导出会话</button>
</div>

<div class="chat-container" id="chatContainer"></div>
<button class="scroll-bottom-btn" id="scrollBottomBtn" onclick="scrollToBottom()" title="Scroll to bottom">↓</button>

<div class="info-bar">
  <span><span class="label">Session:</span> <span id="infoSession">-</span></span>
  <span><span class="label">Intent:</span> <span id="infoIntent">-</span></span>
  <span><span class="label">Retries:</span> <span id="infoRetries">0</span></span>
  <span><span class="label">Emotion:</span> <span id="infoEmotion">-</span> <span id="emotionBar" class="emotion-bar"></span></span>
  <span><span class="label">Messages:</span> <span id="infoMessages">0</span></span>
  <span><span class="label">Status:</span> <span id="infoStatus">Active</span></span>
</div>

<div class="shortcut-hint">按 Enter 发送 · Ctrl+Enter 换行</div>
<div class="input-area">
  <input type="text" id="messageInput" placeholder="输入消息... (Enter 发送)" autocomplete="off" />
  <button id="sendBtn" onclick="sendMessage()">发送</button>
</div>

<!-- Export Modal -->
<div class="export-modal" id="exportModal">
  <div class="export-modal-content">
    <h3>📋 会话导出</h3>
    <pre id="exportContent">加载中...</pre>
    <div class="btn-row">
      <button onclick="copyExport()">📋 复制</button>
      <button onclick="downloadExport()">💾 下载 JSON</button>
      <button class="primary" onclick="closeExportModal()">关闭</button>
    </div>
  </div>
</div>

<script>
let currentSession = null;
let isProcessing = false;
let messageCount = 0;
let botMessageIndex = 0;

// Quick reply suggestions based on context
const QUICK_REPLIES = {
  default: ['产品怎么用？', '我要投诉', '价格是多少？', '有保修吗？'],
  after_greeting: ['产品怎么用？', '价格是多少？', '有什么功能？'],
  after_reply: ['能详细说说吗？', '还有其他问题', '谢谢，没问题了'],
  satisfaction: ['满意', '不满意'],
};

// Theme management
function toggleTheme() {
  const html = document.documentElement;
  const btn = document.querySelector('.theme-toggle');
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

// Restore theme on load
(function() {
  const saved = localStorage.getItem('theme');
  if (saved === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    setTimeout(() => {
      const btn = document.querySelector('.theme-toggle');
      if (btn) btn.textContent = '☀️';
    }, 0);
  }
})();

const chatContainer = document.getElementById('chatContainer');
const messageInput = document.getElementById('messageInput');
const sendBtn = document.getElementById('sendBtn');
const scrollBottomBtn = document.getElementById('scrollBottomBtn');

// Show/hide scroll-to-bottom button based on scroll position
chatContainer.addEventListener('scroll', () => {
  const isNearBottom = chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight < 100;
  scrollBottomBtn.classList.toggle('show', !isNearBottom);
});

function scrollToBottom() {
  chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });
}

messageInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !isProcessing) sendMessage();
});

function addMessage(role, content, type, animate) {
  messageCount++;
  document.getElementById('infoMessages').textContent = messageCount;

  const div = document.createElement('div');
  div.className = `message ${role}`;
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '\u{1F464}' : '\u{1F916}';

  // Bubble wrapper (contains bubble + metadata)
  const wrapper = document.createElement('div');
  wrapper.style.display = 'flex';
  wrapper.style.flexDirection = 'column';
  wrapper.style.alignSelf = role === 'user' ? 'flex-end' : 'flex-start';

  const bubble = document.createElement('div');
  bubble.className = `bubble ${type || ''}`;

  // Timestamp + read time row
  const metaRow = document.createElement('div');
  metaRow.style.display = 'flex';
  metaRow.style.alignItems = 'center';
  metaRow.style.gap = '4px';
  if (role === 'user') {
    metaRow.style.justifyContent = 'flex-end';
  }

  const timeSpan = document.createElement('span');
  timeSpan.className = 'msg-time';
  timeSpan.textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  metaRow.appendChild(timeSpan);

  if (role === 'bot') {
    // Estimate read time: Chinese ~5 chars/sec, English ~160 words/min ≈ 2.7 words/sec
    const hasChinese = content && /[\u4e00-\u9fff]/.test(content);
    let readTime;
    if (content && hasChinese) {
      const charCount = content.length;
      const seconds = Math.ceil(charCount / 5); // ~5 Chinese chars per second
      readTime = `${seconds}秒阅读`;
    } else if (content) {
      const wordCount = content.split(/\s+/).filter(w => w).length;
      const seconds = Math.ceil(wordCount / 2.7);
      readTime = `${seconds}s read`;
    }
    if (readTime) {
      const rtSpan = document.createElement('span');
      rtSpan.className = 'read-time';
      rtSpan.textContent = readTime;
      metaRow.appendChild(rtSpan);
    }

    // Copy button for bot messages
    const copyBtn = document.createElement('button');
    copyBtn.className = 'copy-btn';
    copyBtn.textContent = '📋';
    copyBtn.title = '复制内容';
    copyBtn.onclick = () => {
      navigator.clipboard.writeText(bubble.textContent)
        .then(() => { copyBtn.textContent = '✅'; setTimeout(() => copyBtn.textContent = '📋', 1500); })
        .catch(() => {});
    };
    metaRow.appendChild(copyBtn);
  }

  wrapper.appendChild(bubble);
  wrapper.appendChild(metaRow);
  div.appendChild(avatar);
  div.appendChild(wrapper);
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;

  // Remove existing quick replies when new message added
  removeQuickReplies();

  if (animate && role === 'bot') {
    // Slower speed for Chinese text (40ms/char) vs English (25ms/char)
    const hasChinese = /[\u4e00-\u9fff]/.test(content);
    typeWriter(bubble, content, hasChinese ? 35 : 20);
  } else {
    bubble.textContent = content;
  }

  // Add star rating for bot replies (not satisfaction/closing messages)
  if (role === 'bot' && type !== 'satisfaction' && type !== 'closing') {
    botMessageIndex++;
    addStarRating(wrapper, botMessageIndex);
  }

  return div;
}

function typeWriter(element, text, speed) {
  element.classList.add('typing-cursor');
  let i = 0;
  const len = text.length;
  function type() {
    if (i < len) {
      element.textContent = text.substring(0, i + 1);
      i++;
      chatContainer.scrollTop = chatContainer.scrollHeight;
      setTimeout(type, speed);
    } else {
      element.classList.remove('typing-cursor');
    }
  }
  type();
}

function addSystemMsg(text) {
  const div = document.createElement('div');
  div.className = 'system-msg';
  div.textContent = text;
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function addTyping() {
  const div = document.createElement('div');
  div.className = 'message bot';
  div.id = 'typingIndicator';
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = '\u{1F916}';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = '<div class="typing-indicator"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>';
  div.appendChild(avatar);
  div.appendChild(bubble);
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function removeTyping() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

// Quick reply suggestions
function showQuickReplies(replies) {
  removeQuickReplies();
  const container = document.createElement('div');
  container.className = 'quick-replies';
  container.id = 'quickRepliesContainer';
  for (const text of replies) {
    const btn = document.createElement('button');
    btn.className = 'quick-reply-btn';
    btn.textContent = text;
    btn.onclick = () => quickTest(text);
    container.appendChild(btn);
  }
  chatContainer.appendChild(container);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function removeQuickReplies() {
  const el = document.getElementById('quickRepliesContainer');
  if (el) el.remove();
}

// Get contextual quick replies based on last bot message type
function getContextualQuickReplies(lastReplyType) {
  if (lastReplyType === 'satisfaction') return QUICK_REPLIES.satisfaction;
  if (lastReplyType === 'closing') return [];
  if (lastReplyType === 'reply') return QUICK_REPLIES.after_reply;
  return QUICK_REPLIES.default;
}

// Inline star rating — add ⭐ buttons after bot messages
function addStarRating(wrapperEl, msgIndex) {
  const ratingDiv = document.createElement('div');
  ratingDiv.className = 'star-rating';
  ratingDiv.id = `starRating_${msgIndex}`;

  const label = document.createElement('span');
  label.className = 'label';
  label.textContent = 'Helpful?';
  ratingDiv.appendChild(label);

  for (let i = 1; i <= 5; i++) {
    const btn = document.createElement('button');
    btn.className = 'star-btn';
    btn.textContent = '⭐';
    btn.title = `${i} star${i > 1 ? 's' : ''}`;
    btn.onclick = () => submitRating(msgIndex, i, ratingDiv);
    // Hover highlight
    btn.onmouseenter = () => {
      const allBtns = ratingDiv.querySelectorAll('.star-btn');
      allBtns.forEach((b, idx) => {
        b.style.filter = idx < i ? 'grayscale(0) opacity(1)' : 'grayscale(1) opacity(0.4)';
      });
    };
    btn.onmouseleave = () => {
      const allBtns = ratingDiv.querySelectorAll('.star-btn');
      const rated = ratingDiv.querySelector('.rated');
      if (!rated) {
        allBtns.forEach(b => b.style.filter = 'grayscale(1) opacity(0.4)');
      }
    };
    ratingDiv.appendChild(btn);
  }

  wrapperEl.appendChild(ratingDiv);
}

function submitRating(msgIndex, stars, ratingDiv) {
  // Mark stars as rated
  const allBtns = ratingDiv.querySelectorAll('.star-btn');
  allBtns.forEach((b, idx) => {
    if (idx < stars) b.classList.add('rated');
    b.onclick = null;
    b.style.cursor = 'default';
  });

  // Remove label
  const label = ratingDiv.querySelector('.label');
  if (label) label.remove();

  // Show thanks message
  const thanks = document.createElement('div');
  thanks.className = 'star-rating thanks';
  thanks.textContent = `Thanks! You rated ${stars} ⭐`;
  ratingDiv.replaceWith(thanks);

  // Send rating to server for logging (fire-and-forget)
  if (currentSession) {
    fetch('/api/rating', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: currentSession,
        message_index: msgIndex,
        stars: stars
      })
    }).catch(() => {});
  }
}

async function sendMessage(text) {
  if (isProcessing) return;
  const message = text || messageInput.value.trim();
  if (!message) return;
  messageInput.value = '';
  addMessage('user', message);

  isProcessing = true;
  sendBtn.disabled = true;
  addTyping();

  try {
    const session = currentSession || crypto.randomUUID();
    if (!currentSession) {
      currentSession = session;
      document.getElementById('infoSession').textContent = session.slice(0, 8) + '...';
    }

    // Try SSE streaming first; fall back to standard JSON
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: session, stream: true })
    });

    removeTyping();

    if (response.headers.get('content-type') && response.headers.get('content-type').includes('text/event-stream')) {
      // SSE streaming mode
      await handleStreamResponse(response);
    } else {
      // Fallback to standard JSON
      const data = await response.json();
      if (data.error) {
        addMessage('bot', 'Error: ' + data.error, '', false);
      } else {
        let lastReplyType = '';
        for (const reply of data.replies) {
          const typeMap = { satisfaction: 'satisfaction', closing: 'closing' };
          lastReplyType = typeMap[reply.type] || 'reply';
          addMessage('bot', reply.content, lastReplyType, true);
        }

        // Show contextual quick replies after bot responds
        const suggestions = getContextualQuickReplies(lastReplyType);
        if (suggestions && suggestions.length > 0) {
          setTimeout(() => showQuickReplies(suggestions), 800);
        }

        if (data.intent) document.getElementById('infoIntent').textContent = data.intent;
        if (data.retry_count !== undefined) document.getElementById('infoRetries').textContent = data.retry_count;
        if (data.emotion) {
          const emojiMap = { neutral: '😐', angry: '😠', sad: '😢', anxious: '😰', happy: '😊' };
          const emoji = emojiMap[data.emotion] || '😐';
          document.getElementById('infoEmotion').textContent = emoji + ' ' + data.emotion + (data.emotion_intensity ? '(' + data.emotion_intensity + '/5)' : '');
          updateEmotionBar(data.emotion, data.emotion_intensity);
        }
        document.getElementById('infoStatus').textContent = data.interrupted ? 'Escalated' : 'Active';
      }
    }
  } catch (err) {
    removeTyping();
    addMessage('bot', 'Connection error: ' + err.message, '');
  }

  isProcessing = false;
  sendBtn.disabled = false;
  messageInput.focus();
}

// SSE streaming handler
async function handleStreamResponse(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let fullReply = '';
  let lastReplyType = 'reply';
  let metadata = null;

  // Create a streaming bubble (no content yet, so no read time)
  const streamDiv = addMessage('bot', '', 'reply', false);
  const bubbleEl = streamDiv.querySelector('.bubble');
  bubbleEl.classList.add('typing-cursor');

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Process complete SSE lines
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const dataStr = line.slice(6);
        try {
          const data = JSON.parse(dataStr);
          if (data.done) {
            // Final metadata event
            metadata = data;
            bubbleEl.classList.remove('typing-cursor');
            break;
          } else if (data.progress === 'analyzing') {
            // Show analyzing indicator in bubble
            bubbleEl.textContent = '🤔 分析中...';
          } else if (data.token !== undefined) {
            fullReply += data.token;
            bubbleEl.textContent = fullReply;
            chatContainer.scrollTop = chatContainer.scrollHeight;
          }
        } catch (e) {
          // Skip malformed JSON
        }
      }
    }
  } finally {
    reader.releaseLock();
  }

  // Update metadata from final event
  if (metadata) {
    lastReplyType = metadata.reply_type || 'reply';
    bubbleEl.className = `bubble ${lastReplyType}`;

    if (metadata.intent) document.getElementById('infoIntent').textContent = metadata.intent;
    if (metadata.emotion) {
      const emojiMap = { neutral: '😐', angry: '😠', sad: '😢', anxious: '😰', happy: '😊' };
      const emoji = emojiMap[metadata.emotion] || '😐';
      document.getElementById('infoEmotion').textContent = emoji + ' ' + metadata.emotion + (metadata.emotion_intensity ? '(' + metadata.emotion_intensity + '/5)' : '');
      updateEmotionBar(metadata.emotion, metadata.emotion_intensity);
    }

    // Show quick replies after streaming completes
    const suggestions = getContextualQuickReplies(lastReplyType);
    if (suggestions && suggestions.length > 0) {
      setTimeout(() => showQuickReplies(suggestions), 800);
    }
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

  // Show welcome message with suggested topics
  setTimeout(() => {
    addMessage('bot', '👋 您好！我是智联科技智能客服助手。\n\n我可以帮您：\n• 📦 产品咨询（智能音箱、智能家居、云服务）\n• 🔧 故障排查与技术支援\n• 💰 价格与保修政策\n• 📞 投诉与建议\n\n请问有什么可以帮您的？', 'reply', true);
    setTimeout(() => showQuickReplies(['产品怎么用？', '价格是多少？', '我要投诉', '有保修吗？']), 1200);
  }, 300);
}

function clearChat() { chatContainer.innerHTML = ''; }

function resetAll() {
  currentSession = null;
  messageCount = 0;
  chatContainer.innerHTML = '';
  document.getElementById('infoSession').textContent = '-';
  document.getElementById('infoIntent').textContent = '-';
  document.getElementById('infoRetries').textContent = '0';
  document.getElementById('infoMessages').textContent = '0';
  document.getElementById('infoStatus').textContent = 'Active';
}

function quickTest(text) { messageInput.value = text; sendMessage(text); }

// Session export
async function exportSession() {
  if (!currentSession) { addSystemMsg('没有活跃的会话可导出'); return; }
  document.getElementById('exportContent').textContent = '加载中...';
  document.getElementById('exportModal').classList.add('show');
  try {
    const resp = await fetch(`/api/export/${currentSession}`);
    const data = await resp.json();
    if (data.error) {
      document.getElementById('exportContent').textContent = '错误: ' + data.error;
    } else {
      window._exportData = data;
      document.getElementById('exportContent').textContent = JSON.stringify(data, null, 2);
    }
  } catch(e) {
    document.getElementById('exportContent').textContent = '网络错误: ' + e.message;
  }
}

function closeExportModal() { document.getElementById('exportModal').classList.remove('show'); }

function copyExport() {
  const text = document.getElementById('exportContent').textContent;
  navigator.clipboard.writeText(text).then(() => addSystemMsg('已复制到剪贴板')).catch(() => addSystemMsg('复制失败'));
}

function downloadExport() {
  if (!window._exportData) return;
  const blob = new Blob([JSON.stringify(window._exportData, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `session-${window._exportData.session_id || 'export'}.json`;
  a.click(); URL.revokeObjectURL(url);
  addSystemMsg('会话已导出为 JSON 文件');
}

// Update emotion bar with visual intensity indicator
function updateEmotionBar(emotion, intensity) {
  const bar = document.getElementById('emotionBar');
  if (!bar || !intensity) { bar.innerHTML = ''; return; }
  let html = '';
  for (let i = 1; i <= 5; i++) {
    const cls = i <= intensity ? (intensity >= 4 ? 'seg active high' : 'seg active') : 'seg';
    html += `<span class="${cls}"></span>`;
  }
  bar.innerHTML = html;
}

// Hot reload knowledge base
async function reloadKB() {
  try {
    const resp = await fetch('/api/rag/reload');
    const data = await resp.json();
    if (data.error) {
      addSystemMsg(`知识库重载失败: ${data.error}`);
    } else {
      addSystemMsg(`✅ 知识库已重载: ${data.documents} 个文档, ${data.sections} 个章节`);
    }
  } catch(e) {
    addSystemMsg(`网络错误: ${e.message}`);
  }
}

async function runFullFlow() {
  clearChat();
  newSession();
  await new Promise(r => setTimeout(r, 500));

  const steps = [
    { msg: '产品怎么用？', label: '步骤1：咨询产品用法' },
    { msg: '谢谢，没问题了', label: '步骤2：结束对话 → 满意度检查' },
    { msg: '满意', label: '步骤3：满意 → 结束语' },
  ];

  for (const step of steps) {
    addSystemMsg(step.label);
    await new Promise(r => setTimeout(r, 500));
    messageInput.value = step.msg;
    await sendMessage(step.msg);
    // Wait until processing finishes plus a short buffer
    while (isProcessing) { await new Promise(r => setTimeout(r, 200)); }
    await new Promise(r => setTimeout(r, 1500));
  }

  addSystemMsg('完整流程演示完成！');
}
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
        """Health check endpoint — returns system status, LLM connectivity, DB stats."""
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
            print(f"[Rating] session={session_id}, msg={msg_index}, stars={stars}⭐")

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
