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
import os
import time
import platform
from uuid import uuid4
from datetime import datetime
from collections import defaultdict

# Windows UTF-8 — skip under pytest (CaptureFixture has no .buffer)
if sys.platform == 'win32' and '__pytest' not in sys.modules:
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

from langchain_core.messages import HumanMessage, AIMessage
from agent.graph import build_graph
from agent.security.pii_redactor import redact as pii_redact, scan_and_log as pii_scan
from agent.security.prompt_guard import scan_input as prompt_scan, reinforce_system_prompt
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import os

# ── Template Loader ─────────────────────────────────────────────
def _load_template(name):
    """Load an HTML template from the templates/ directory."""
    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
    path = os.path.join(template_dir, name)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"[Server] ⚠️  Template not found: {path}")
        return f"<h1>Template not found: {name}</h1>"

# Load templates at import time (cached in memory)
CHAT_HTML = _load_template('index.html')
ANALYTICS_HTML = _load_template('analytics.html')

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
        print(f"[Streaming 错误] {e}")
        # Fallback: non-streaming call
        from agent.nodes import _call_llm
        fallback = _call_llm(messages, system_prompt, max_tokens)
        yield fallback


def run_agent_stream(session_id, user_message):
    """Run the agent and stream tokens via SSE.

    Returns a generator of SSE-formatted strings.
    """
    # Security: scan for prompt injection (same check as non-streaming path)
    from agent.security.prompt_guard import scan_input as _prompt_scan
    from agent.security.pii_redactor import scan_and_log as _pii_scan
    _prompt_result = _prompt_scan(user_message)
    if not _prompt_result.is_safe:
        print(f"[Security] Stream: Prompt injection blocked: threats={_prompt_result.threats}")
        yield 'data: ' + json.dumps({"error": "输入包含不安全内容，已被拦截", "blocked_threats": _prompt_result.threats}, ensure_ascii=False) + '\n\n'
        return
    _pii_scan(user_message)

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
    from agent.nodes import identify_intent, build_reply_context

    # Step 1: Identify intent (non-streaming)
    state = dict(input_data)

    # Send progress event: analyzing
    yield 'data: ' + json.dumps({"progress": "analyzing"}, ensure_ascii=False) + '\n\n'

    intent_result = identify_intent(state)
    state.update(intent_result)

    intent = state.get('intent', 'consult')
    emotion = state.get('emotion', 'neutral')
    intensity = state.get('emotion_intensity', 1)

    # Step 2: Build reply context using shared helper (same logic as generate_reply node)
    ctx = build_reply_context(
        messages=state.get('messages', []),
        intent=intent,
        user_query=user_message,
        session_id=session_id,
        emotion=emotion,
        emotion_intensity=intensity,
        retry_count=0,
    )

    # Stream tokens
    rag_info = ctx.get('rag_info')
    full_reply = ""
    for token in stream_llm_reply(ctx['context_messages'], ctx['system_prompt'], max_tokens=384):
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
                    "CREATE TABLE IF NOT EXISTS ratings ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
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
                    "CREATE TABLE IF NOT EXISTS reactions ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
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

            # Security: scan for prompt injection attempts (LangGraph best practice)
            prompt_result = prompt_scan(user_message)
            if not prompt_result.is_safe:
                print(f"[Security] Prompt injection blocked from {self.client_address[0]}: "
                      f"threats={prompt_result.threats}")
                self.send_response(400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": "输入包含不安全内容，已被拦截",
                    "blocked_threats": prompt_result.threats,
                }, ensure_ascii=False).encode('utf-8'))
                return

            # Security: scan and log PII (non-blocking — still process the request)
            pii_detected = pii_scan(user_message)

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
