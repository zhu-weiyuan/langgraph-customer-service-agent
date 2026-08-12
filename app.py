# -*- coding: utf-8 -*-
print("[APP.LOADED] app.py loaded from", __file__)
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

# Windows UTF-8 — leave pytest capture streams untouched.
if sys.platform == 'win32' and 'pytest' not in sys.modules:
    try:
        # Use line buffering to ensure immediate output
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    except (AttributeError, ValueError):
        pass

from langchain_core.messages import HumanMessage, AIMessage
from agent.graph import build_graph
from agent.security.pii_redactor import redact as pii_redact, scan_and_log as pii_scan
from agent.security.prompt_guard import scan_input as prompt_scan, reinforce_system_prompt
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import os

def _timeline_langchain_messages(session_id: str, limit: int = 100):
    """Load persisted conversation messages and remove adjacent checkpoint duplicates."""
    from agent.memory import get_conversation_messages
    rows = get_conversation_messages(session_id, limit=limit)
    result = []
    seen = set()
    previous = None
    for row in rows:
        role = row.get("role", "user")
        content = row.get("content", "")
        key = (role, content)
        if key == previous or key in seen:
            continue
        seen.add(key)
        previous = key
        result.append(HumanMessage(content=content) if role == "user"
                      else AIMessage(content=content))
    return result


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

# Load templates from JSON-encoded strings (avoids HTML parser issues with </tag> in <script>)
from agent._html_strings import INDEX as _RAW_CHAT_HTML, ANALYTICS as ANALYTICS_HTML
from agent.logging_config import setup_logging, logger
from agent.metrics import MetricsCollector
from agent.observability import AlertService
metrics = MetricsCollector()
alert_service = AlertService(metrics)
# 告警规则：平均延迟超过 5s 时触发
alert_service.add_rule("high_latency", "request_latency", threshold=5000)
from agent.auth import AuthMiddleware
from agent.redis_cache import get_redis

_redis = get_redis()

# Fix HTML parser issue: </g in regex breaks <script> parsing
# Replace /</g with /\x3c/g in the served HTML
import re as _re
# Fix HTML parser issue: any </ + letter in <script> breaks parsing
# Replace ALL closing tags inside <script> with escaped versions
CHAT_HTML = _RAW_CHAT_HTML

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
_trace_service = None

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
        with ur.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def init():
    """Initialize the agent graph.

    Set USE_POSTGRES=1 for persistent checkpointing via PostgreSQL,
    or USE_SQLITE=1 for SQLite (default: in-memory MemorySaver).
    """
    global _graph, _trace_service
    use_postgres = os.environ.get('USE_POSTGRES', '0') == '1'
    use_sqlite = os.environ.get('USE_SQLITE', '0') == '1'
    db_path = os.environ.get('CHECKPOINT_DB', 'checkpoints.db')
    _graph = build_graph(use_postgres=use_postgres, use_sqlite=use_sqlite,
                         db_path=db_path)
    mode = 'postgres' if use_postgres else 'sqlite' if use_sqlite else 'memory'
    print(f"[Server] Agent initialized (checkpointer={mode})")

    # Initialize observability trace service
    from agent.observability import TraceService
    _trace_service = TraceService(db_path="agent/trace.db")
    print("[Server] ✅ TraceService initialized (agent/trace.db)")

    # LLM connectivity check (non-blocking warning)
    llm_ok = _check_llm_connectivity()
    if llm_ok:
        print("[Server] ✅ LLM API reachable")
    else:
        print("[Server] ⚠️  LLM API unreachable — agent will run in degraded mode")
        print("[Server]    Ensure llama.cpp is running on port 8080")


def stream_llm_reply(messages, system_prompt, max_tokens=384):
    """Stream LLM reply tokens via llama.cpp streaming API.

    异常处理：
    - 客户端断开（BrokenPipeError/ConnectionResetError）→ 静默退出，停止生成 Token
    - 超时 → 发送错误消息给前端
    - 网络断流 → 重试一次后返回完整回复作为 fallback
    - 熔断器记录：429 限流 → record_vendor_429()

    Yields individual token strings as they arrive from the LLM.
    Falls back to non-streaming if streaming fails.
    """
    import urllib.request as _ur
    from agent.llm_client import get_llm_client

    client = get_llm_client()
    api_url = client.api_url
    api_key = client.api_key

    payload = {
        "model": client.model,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
    }

    # Ensure we use the chat completions endpoint
    if not api_url.endswith('/chat/completions'):
        api_url = api_url.rstrip('/') + '/chat/completions'
    
    data = json.dumps(payload).encode("utf-8")
    req = _ur.Request(
        api_url,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )

    # 获取限流器（用于熔断器记录）
    from agent.rate_limiter import get_rate_limiter
    rate_limiter = get_rate_limiter()
    
    try:
        print(f"[Stream] Connecting to {api_url}...")
        with _ur.urlopen(req, timeout=180) as resp:
            print(f"[Stream] Connected, status={resp.status}")
            # 连接成功，记录到熔断器
            rate_limiter.record_vendor_success()
            
            buf = b""
            token_count = 0
            while True:
                try:
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
                                print(f"[Stream] Received [DONE] after {token_count} tokens")
                                return
                            try:
                                obj = json.loads(json_str)
                                delta = obj.get("choices", [{}])[0].get("delta", {})
                                token = delta.get("content", "")
                                if token:
                                    token_count += 1
                                    yield token
                            except (json.JSONDecodeError, KeyError, IndexError):
                                pass
                except (BrokenPipeError, ConnectionResetError) as e:
                    # 客户端断开连接，停止生成 Token（避免浪费钱）
                    print(f"[Stream] Client disconnected: {e}")
                    return
                except TimeoutError:
                    # 返回文本让 run_agent_stream 包装为 SSE
                    yield "【响应超时，请稍后重试】"
                    return
            print(f"[Stream] Finished with {token_count} tokens")
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "Too Many Requests" in error_msg:
            rate_limiter.record_vendor_429()
            yield "【服务繁忙，请稍后重试】"
            return
        # 其他错误：降级为非流式调用，逐字切块渐进显示
        print(f"[Stream] Streaming failed, fallback to non-streaming: {e}")
        rate_limiter.record_vendor_success()
        from agent.nodes import _call_llm
        fallback = _call_llm(messages, system_prompt, max_tokens)
        if fallback:
            for i in range(0, len(fallback), 3):
                yield fallback[i:i+3]


def run_agent_stream(session_id, user_message, trace_session=None):
    """Run the agent and stream tokens via SSE.

    Args:
        session_id: Session identifier
        user_message: User input message
        trace_session: Optional TraceSession for observability recording

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

    # Trace: record start of intent identification
    t0 = time.time()
    if trace_session:
        trace_session.add_event("intent_identification", {"status": "started"})

    # Step 1: Identify intent (non-streaming)
    state = dict(input_data)

    # Send progress event: analyzing
    yield 'data: ' + json.dumps({"progress": "analyzing"}, ensure_ascii=False) + '\n\n'

    intent_result = identify_intent(state)
    state.update(intent_result)

    # Trace: record intent result
    if trace_session:
        intent_duration = (time.time() - t0) * 1000
        trace_session.add_event("intent_identification", {
            "status": "completed",
            "intent": state.get('intent', 'unknown'),
            "emotion": state.get('emotion', 'neutral'),
            "intensity": state.get('emotion_intensity', 1),
            "ending": state.get('ending', False)
        }, duration_ms=intent_duration)

    intent = state.get('intent', 'consult')
    emotion = state.get('emotion', 'neutral')
    intensity = state.get('emotion_intensity', 1)

    # Step 2: Build reply context using shared helper (same logic as generate_reply node)
    t1 = time.time()
    ctx = build_reply_context(
        messages=state.get('messages', []),
        intent=intent,
        user_query=user_message,
        session_id=session_id,
        emotion=emotion,
        emotion_intensity=intensity,
        retry_count=0,
    )
    context_duration = (time.time() - t1) * 1000

    # Trace: record RAG retrieval result
    if trace_session:
        rag_info = ctx.get('rag_info')
        trace_session.add_event("rag_retrieval", {
            "duration_ms": round(context_duration, 2),
            "has_context": bool(ctx.get('rag_context', '')),
            "rounds": rag_info.get('rounds', 0) if rag_info else 0,
            "sufficient": rag_info.get('sufficient', False) if rag_info else False,
            "queries_tried": len(rag_info.get('queries_tried', [])) if rag_info else 0
        }, duration_ms=context_duration)

    # Stream tokens
    rag_info = ctx.get('rag_info')
    full_reply = ""
    print(f"[Stream] Starting to stream tokens...")
    token_count = 0
    for token in stream_llm_reply(ctx['context_messages'], ctx['system_prompt'], max_tokens=384):
        full_reply += token
        token_count += 1
        token_json = json.dumps({"token": token}, ensure_ascii=False)
        yield "data: " + token_json + "\n\n"
    print(f"[Stream] Finished streaming {token_count} tokens, total length: {len(full_reply)}")

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

    # Trace: record LLM generation result
    if trace_session:
        trace_session.add_event("llm_generation", {
            "status": "completed",
            "token_count": token_count,
            "reply_length": len(full_reply),
            "reply_type": _classify_message(full_reply)
        })
        # Save the complete trace
        trace_session.finalize()
        if _trace_service:
            try:
                _trace_service.save_trace(trace_session)
            except Exception as e:
                print(f"[Trace] Failed to save: {e}")

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


def run_agent(session_id, user_message, trace_session=None):
    """Run the agent for a user message.

    Args:
        session_id: Session identifier
        user_message: User input message
        trace_session: Optional TraceSession for observability recording
    """
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

    # Trace: record graph execution start
    t0 = time.time()
    if trace_session:
        trace_session.add_event("graph_execution", {"status": "started"})

    try:
        for event in _graph.stream(input_data, config=config, stream_mode="values"):
            if event and event.get('messages'):
                new_msgs = event['messages'][existing_count:]
                all_new_messages.extend(new_msgs)
    except Exception as e:
        # Trace: record graph execution completion with error info
        if trace_session:
            duration = (time.time() - t0) * 1000
            trace_session.add_event("graph_execution", {
                "status": "completed",
                "interrupted": False,
                "duration_ms": round(duration, 2)
            }, duration_ms=duration)
        if "interrupt" in str(e).lower():
            interrupted = True

    # Trace: record graph execution completion
    if trace_session:
        duration = (time.time() - t0) * 1000
        trace_session.add_event("graph_execution", {
            "status": "completed",
            "interrupted": interrupted,
            "duration_ms": round(duration, 2)
        }, duration_ms=duration)

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

    # Trace: save the complete trace before returning
    if trace_session:
        trace_session.add_event("response", {
            "reply_count": len(replies),
            "intent": intent,
            "next_action": next_action
        })
        trace_session.finalize()
        if _trace_service:
            try:
                _trace_service.save_trace(trace_session)
            except Exception as e:
                print(f"[Trace] Failed to save: {e}")

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
    @staticmethod
    def _degraded_response(message: str, session_id: str, request_id: str) -> dict:
        return {"fallback": True, "request_id": request_id,
                "session_id": session_id,
                "error": "服务暂时不可用，请稍后再试"}

    protocol_version = 'HTTP/1.1'
    
    def log_message(self, format, *args):
        """Suppress default HTTP logging."""
        pass
    
    def _generate_request_id(self) -> str:
        """Generate a unique request ID for tracking (Phase A: Request Governance)."""
        import uuid
        return str(uuid.uuid4())
    
    def _send_response_headers_and_set_trace(self, start_time: float, status_code: int = 200):
        """Unified response header handling with X-Response-Time (Phase A).
        
        Note: X-Request-ID is now sent separately at each call site for better traceability.
        """
        duration_ms = (time.time() - start_time) * 1000
        self.send_response(status_code)
        self.send_header('X-Response-Time', f'{duration_ms:.1f}ms')
    def _check_rate_limit(self):
        """分层限流检查。

        四层架构：
        1. 用户级：滑动窗口（60 req/60s）
        2. 供应商级：令牌桶 + 熔断器
        3. 模型级：按模型隔离的令牌桶
        4. 并发限制：最多10个并发请求

        Returns True if allowed, False otherwise.
        """
        from agent.rate_limiter import get_rate_limiter, RateLimitError
        
        client_ip = self.client_address[0] if hasattr(self, 'client_address') else 'unknown'
        session_id = getattr(self, '_session_id', client_ip)  # 优先用session_id，否则用IP
        
        limiter = get_rate_limiter()
        
        try:
            # 同步限流：使用滑动窗口层检查 IP + session
            from agent.rate_limiter import LocalConservativeLimiter as _LocalLimiter
            _local = _LocalLimiter()  # 内置线程安全的滑动窗口，初始即用
            layer_keys = {"ip": self.client_address[0] if hasattr(self, 'client_address') else 'unknown',
                          "session": session_id}
            _local.acquire(layer_keys)
            return True
        except RateLimitError as e:
            self.send_response(429)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            body = json.dumps({
                "error": str(e),
                "retry_after": 60,
            }, ensure_ascii=False)
            self.wfile.write(body.encode('utf-8'))
            return False

    def do_GET(self):
        # Phase A: Generate request_id at the very top
        self._request_id = self._generate_request_id()
        start_time = time.time()
        
        # Public endpoints (no auth required)
        if not AuthMiddleware.is_public_endpoint(self.path) and not AuthMiddleware.check_api_key(self):
            self._send_response_headers_and_set_trace(start_time, 401)
            self.send_header('X-Request-ID', self._request_id)  # Phase A: error path
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized: Invalid or missing API key"}')
            return

        # Metrics endpoint (public for monitoring) — Prometheus format
        if self.path == '/api/metrics':
            # 运行告警检查（非阻塞）
            try:
                alerts = alert_service.check_and_alert()
                if alerts:
                    print(f"[Alerts triggered] {alerts}")
            except Exception as e:
                print(f"[Alert check error] {e}")
            
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(metrics.get_prometheus_text().encode())
        
        elif self.path == '/api/health':
            self._send_health()
            return

        # Rate limit API endpoints only, not static/HTML
        if self.path not in ('/', '/index.html') and self.path.startswith('/api/'):
            if not self._check_rate_limit():
                return

        if self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(CHAT_HTML.encode('utf-8'))
        elif self.path.startswith('/static/'):
            # Serve static files (JS, CSS)
            file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.path.lstrip('/'))
            if os.path.isfile(file_path):
                self.send_response(200)
                if file_path.endswith('.js'):
                    self.send_header('Content-Type', 'application/javascript; charset=utf-8')
                elif file_path.endswith('.css'):
                    self.send_header('Content-Type', 'text/css; charset=utf-8')
                else:
                    self.send_header('Content-Type', 'application/octet-stream')
                self.end_headers()
                with open(file_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, f'Not found: {self.path}')
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
            print(f"[DEBUG] /api/analytics called, __file__={__file__}")
            raise RuntimeError("DEBUG: This should appear if code is loaded!")
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
        elif self.path.startswith('/api/trace'):
            # GET /api/trace - list recent traces
            # GET /api/trace/<request_id> - get specific trace
            try:
                if _trace_service:
                    parts = self.path.split('/')
                    if len(parts) >= 4 and parts[3]:
                        # Get specific trace by ID
                        request_id = parts[3]
                        result = _trace_service.get_trace_by_id(request_id)
                    else:
                        # List recent traces (default last 20)
                        limit = 20
                        if '?' in self.path:
                            from urllib.parse import parse_qs, urlparse
                            params = parse_qs(urlparse(self.path).query)
                            limit = int(params.get('limit', ['20'])[0])
                        result = _trace_service.get_recent_traces(limit=limit)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                else:
                    self.send_response(503)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Trace service not initialized"}).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
        elif self.path == '/api/trace/stats':
            # GET /api/trace/stats - trace statistics
            try:
                if _trace_service:
                    result = _trace_service.get_stats()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                else:
                    self.send_response(503)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Trace service not initialized"}).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
        elif self.path == '/analytics':
            # GET /analytics - Analytics Dashboard UI (HTML page with charts)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(ANALYTICS_HTML.encode('utf-8'))
        elif self.path == '/api/redis/health':
            # GET /api/redis/health - Redis connection health check
            try:
                result = _redis.health_check()
                self._json_response(200, result)
            except Exception as e:
                self._json_response(500, {"error": str(e)})
        elif self.path == '/api/redis/stats':
            # GET /api/redis/stats - Redis cache hit/miss statistics
            try:
                stats = _redis.cache_stats()
                self._json_response(200, stats)
            except Exception as e:
                self._json_response(500, {"error": str(e)})
        elif self.path == '/api/redis/hot-questions':
            # GET /api/redis/hot-questions - Top N hot questions from sorted set
            try:
                n = int(self._get_query_param('n', '10'))
                questions = _redis.get_hot_questions(n)
                self._json_response(200, {"hot_questions": questions})
            except Exception as e:
                self._json_response(500, {"error": str(e)})
        elif self.path.startswith('/api/redis/session/'):
            # GET /api/redis/session/<user_id> - Get user session state
            user_id = self.path.split('/')[-1]
            try:
                session_data = _redis.get_user_session(user_id)
                if session_data is None:
                    self._json_response(404, {"error": f"Session not found for user: {user_id}"})
                else:
                    self._json_response(200, {"user_id": user_id, "session": session_data})
            except Exception as e:
                self._json_response(500, {"error": str(e)})
        elif self.path.startswith('/api/redis/log/'):
            # GET /api/redis/log/<user_id> - Get user query history
            user_id = self.path.split('/')[-1]
            try:
                logs = _redis.get_query_log(user_id)
                self._json_response(200, {"user_id": user_id, "query_log": logs})
            except Exception as e:
                self._json_response(500, {"error": str(e)})
        elif self.path == '/api/redis/online-users':
            # GET /api/redis/online-users - List online users
            try:
                users = _redis.get_online_users()
                self._json_response(200, {"online_users": list(users), "count": len(users)})
            except Exception as e:
                self._json_response(500, {"error": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def _json_response(self, status_code: int, data: dict):
        """Send a JSON response with proper headers."""
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _get_query_param(self, name: str, default: str = '') -> str:
        """Extract a query parameter from the URL."""
        if '?' in self.path:
            params = dict(p.split('=', 1) for p in self.path.split('?')[1].split('&') if '=' in p)
            return params.get(name, default)
        return default

    def _send_health(self):
        """Health check endpoint — returns system status, LLM connectivity, DB stats."""
        try:
            # LLM connectivity
            llm_reachable = False
            llm_url = "http://127.0.0.1:8080"
            try:
                from agent.llm_client import get_llm_client
                client = get_llm_client()
                llm_url = client.base_url
                models_url = llm_url + "/models"
                import urllib.request as ur
                req = ur.Request(models_url, headers={"Authorization": f"Bearer {client.api_key}"})
                resp = ur.urlopen(req, timeout=3)
                llm_reachable = resp.status == 200
            except Exception:
                pass

            # DB stats
            db_stats = {}
            try:
                from agent.memory import _get_connection
                conn = _get_connection()
                conversations = conn.execute("SELECT COUNT(DISTINCT session_id) FROM conversation_history").fetchone()[0]
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
                    "url": llm_url,
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
        # Phase A: Generate request_id at the very top
        self._request_id = self._generate_request_id()
        start_time = time.time()
        
        # Public endpoints (no auth required)
        if not AuthMiddleware.is_public_endpoint(self.path) and not AuthMiddleware.check_api_key(self):
            self._send_response_headers_and_set_trace(start_time, 401)
            self.send_header('X-Request-ID', self._request_id)  # Phase A: error path
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized: Invalid or missing API key"}')
            return

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
            self._send_response_headers_and_set_trace(start_time, 500)
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
        elif self.path == '/api/feedback':
            # POST /api/feedback - save detailed user feedback
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            try:
                from agent.eval_enhanced import save_feedback
                save_feedback(
                    session_id=data.get('session_id', ''),
                    query=data.get('query', ''),
                    answer=data.get('answer', ''),
                    rating=data.get('rating', 0),
                    comment=data.get('comment', '')
                )
                result = {"ok": True}
            except Exception as e:
                result = {"error": str(e)}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
        elif self.path == '/api/feedback/stats':
            # GET /api/feedback/stats - get feedback statistics
            try:
                from agent.eval_enhanced import get_feedback_stats
                result = get_feedback_stats()
            except Exception as e:
                result = {"error": str(e)}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
        elif self.path == '/api/eval/run':
            # GET /api/eval/run - run evaluation (background)
            try:
                from agent.eval_enhanced import run_ragas_evaluation
                result = run_ragas_evaluation()
            except Exception as e:
                result = {"error": str(e)}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
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

            # Observability: create trace session for this request
            from agent.observability import TraceSession
            request_id = str(uuid4())
            user_ip = self.client_address[0]
            trace_session = TraceSession(
                request_id=request_id,
                user_id=session_id,
                input_text=user_message
            )
            # Record initial event
            from datetime import datetime
            trace_session.add_event("request_start", {
                "ip": user_ip,
                "session_id": session_id,
                "stream": stream
            })

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

            # ── Redis: Record hot question + mark user online ──
            _redis.record_query(user_message)
            _redis.mark_online(session_id, ttl=300)

            # ── Redis: Check rate limit (sliding window) ──
            if _redis.available:
                rl = _redis.check_rate_limit(user_ip, max_requests=60, window_seconds=60)
                if not rl["allowed"]:
                    self._send_response_headers_and_set_trace(start_time, 429)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "error": "请求过于频繁，请稍后再试",
                        "retry_after": int(rl["reset_at"] - time.time()) + 1,
                    }, ensure_ascii=False).encode('utf-8'))
                    return

            if stream:
                # SSE streaming response (Phase A: add X-Request-ID)
                # 启用 TCP_NODELAY 禁用 Nagle 算法，确保每次 write/flush 立即发送到网络
                try:
                    import socket as _sock
                    self.connection.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_NODELAY, 1)
                except Exception:
                    pass
                self._send_response_headers_and_set_trace(start_time, 200)
                self.send_header('X-Request-ID', self._request_id)  # Phase A: stream path
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
                status_code = 200
                error_occurred = False
                # 使用 connection.sendall 直接写入 socket，绕过 Python 的 BufferedWriter
                _send = lambda data: self.connection.sendall(data.encode('utf-8'))
                try:
                    for chunk in run_agent_stream(session_id, user_message, trace_session=trace_session):
                        _send(chunk)
                except Exception as e:
                    status_code = 500
                    error_occurred = True
                    import traceback
                    traceback.print_exc()
                    # 使用 _degraded_response 统一降级
                    err_data = self._degraded_response(user_message, session_id, self._request_id)
                    # 同时发送 error 帧 + done 帧，保证前端能显示错误
                    err = json.dumps({'error': err_data['error']}, ensure_ascii=False)
                    _send(f"data: {err}\n\n")
                    done_meta = json.dumps({'done': True, 'error': err_data['error'], 'fallback': True, 'session_id': session_id}, ensure_ascii=False)
                    _send(f"data: {done_meta}\n\n")
                finally:
                    duration_ms = (time.time() - start_time) * 1000
                    # 记录指标：请求计数 + 延迟直方图 + 错误计数
                    labels = {'endpoint': self.path}
                    metrics.increment_counter('http_requests_total', labels)
                    metrics.observe_histogram('request_latency_ms', duration_ms, labels)
                    if status_code >= 400:
                        metrics.increment_counter('http_errors_total', labels)
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
                start_time = time.time()  # 重新计时（避免重复计算流式部分的时间）
                try:
                    # ── Redis: Check LLM response cache ──
                    cached = _redis.get_cached_response(user_message)
                    if cached is not None:
                        _redis.record_cache_hit()
                        result = {"reply": cached, "cached": True}
                        print(f"[Cache HIT] {user_message[:30]}...")
                    else:
                        _redis.record_cache_miss()
                        result = run_agent(session_id, user_message, trace_session=trace_session)
                        # Cache the response (only if no error)
                        reply_text = result.get("reply", "")
                        if reply_text and "error" not in result:
                            _redis.cache_response(user_message, reply_text, ttl=3600)

                    # ── Redis: Record query log for this user ──
                    _redis.add_query_log(session_id, user_message)

                    response = json.dumps(result, ensure_ascii=False)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    # 统一降级响应
                    err_data = self._degraded_response(user_message, session_id, self._request_id)
                    response = json.dumps(err_data, ensure_ascii=False)
                finally:
                    duration_ms = (time.time() - start_time) * 1000
                    # 记录指标：请求计数 + 延迟直方图 + 错误计数
                    labels = {'endpoint': self.path}
                    metrics.increment_counter('http_requests_total', labels)
                    metrics.observe_histogram('request_latency_ms', duration_ms, labels)
                    if 'error' in response:
                        metrics.increment_counter('http_errors_total', labels)

                # Standard JSON response (Phase A: use unified header helper)
                self._send_response_headers_and_set_trace(start_time, 200)
                self.send_header('X-Request-ID', self._request_id)  # Phase A: JSON path
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(response.encode('utf-8'))

        # ── Redis API Endpoints ────────────────────────────────
        elif self.path == '/api/redis/health':
            if self.command != 'GET':
                self.send_response(405)
                self.end_headers()
                return
            health = _redis.health_check()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(health, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/redis/stats':
            if self.command != 'GET':
                self.send_response(405)
                self.end_headers()
                return
            stats = {
                "cache": _redis.cache_stats(),
                "online_users": _redis.get_online_count(),
                "available": _redis.available,
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(stats, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/api/redis/hot-questions':
            if self.command != 'GET':
                self.send_response(405)
                self.end_headers()
                return
            top_n = int(self.path.split('?')[-1].split('top=')[1]) if '?top=' in self.path else 10
            questions = _redis.get_hot_questions(top_n)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"hot_questions": questions}, ensure_ascii=False).encode('utf-8'))

        elif self.path.startswith('/api/redis/session/'):
            user_id = self.path.split('/')[-1]
            if self.command == 'GET':
                session = _redis.get_user_session(user_id)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(session or {}, ensure_ascii=False).encode('utf-8'))
            elif self.command == 'POST':
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                try:
                    data = json.loads(body.decode('utf-8'))
                    _redis.set_user_session(user_id, data)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok"}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode('utf-8'))
            else:
                self.send_response(405)
                self.end_headers()

        elif self.path.startswith('/api/redis/log/'):
            user_id = self.path.split('/')[-1]
            if self.command != 'GET':
                self.send_response(405)
                self.end_headers()
                return
            log = _redis.get_query_log(user_id)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"query_log": log}, ensure_ascii=False).encode('utf-8'))

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")


def main():
    print("[Main] Starting init...")
    init()
    print("[Main] Init complete, starting server...")
    server = ThreadingHTTPServer(('0.0.0.0', PORT), ChatHandler)
    print(f"[Server] Running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
