# -*- coding: utf-8 -*-
"""FastAPI HTTP layer for the customer-service agent.

This module deliberately reuses the proven business layer in ``app_original_sync``:
LangGraph orchestration, RAG, Redis, tracing and (critically) the existing
POST-SSE wire protocol are not rewritten during the web-framework migration.

Run a parity test first:
    uvicorn app_fastapi:app --host 0.0.0.0 --port 7862
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse, FileResponse
from pydantic import BaseModel, Field

# Importing this module does not start ThreadingHTTPServer because its entry
# point is protected by ``if __name__ == '__main__'``.
import app_original_sync as legacy
from agent.auth import AuthMiddleware
from agent.observability import TraceSession

APP_STARTED_AT = time.monotonic()

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "7862"))
MEMORY_DB = ROOT / "user_memory.db"


class SQLitePool:
    """Small bounded aiosqlite pool for HTTP read/write endpoints.

    Agent-internal persistence remains in its existing modules; this pool is
    for endpoints that used to repeatedly call sqlite3.connect() in handlers.
    """
    def __init__(self, database: Path, size: int = 4):
        self.database, self.size = database, size
        self._queue: asyncio.LifoQueue[aiosqlite.Connection] = asyncio.LifoQueue(maxsize=size)
        self._all: list[aiosqlite.Connection] = []

    async def start(self) -> None:
        for _ in range(self.size):
            conn = await aiosqlite.connect(self.database, timeout=10)
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.commit()
            self._all.append(conn)
            self._queue.put_nowait(conn)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._queue.get()
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await self._queue.put(conn)

    async def close(self) -> None:
        for conn in self._all:
            await conn.close()
        self._all.clear()


pool = SQLitePool(MEMORY_DB)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("APP_ENV", "development").lower() in {"prod", "production"}:
        raise RuntimeError(
            "app_fastapi is an experimental migration and is not approved for production; "
            "run the supported app.py server instead"
        )
    # One controlled startup sequence; legacy owns graph/trace/Redis setup.
    legacy.init()
    await pool.start()
    yield
    await pool.close()


app = FastAPI(title="LangGraph Customer Service Agent", version="2.2.0-fastapi", lifespan=lifespan)
# Built-in middleware handles regular buffered bodies correctly and deliberately
# bypasses StreamingResponse, preventing the prior SSE breakage.
app.add_middleware(GZipMiddleware, minimum_size=500)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: Optional[str] = None
    stream: bool = False


class RatingRequest(BaseModel):
    session_id: str = ""
    message_index: int = 0
    stars: int = Field(default=0, ge=0, le=5)


class ReactionRequest(BaseModel):
    session_id: str = ""
    message_id: str = ""
    emoji: str = "👍"
    active: bool = True


class FeedbackRequest(BaseModel):
    session_id: str = ""
    query: str = ""
    answer: str = ""
    rating: int = 0
    comment: str = ""


class TokenRequest(BaseModel):
    api_key: str = Field(min_length=1)
    subject: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(default="default", min_length=1, max_length=128)


def _session_id_for_request(request: Request) -> str:
    """Derive the only session a JWT principal may address."""
    if getattr(request.state, "auth_scheme", "") == "jwt":
        subject = request.state.auth_subject
        return f"user-{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:24]}"
    return legacy._session_id_for_ip(_client_ip(request))


def _owns_session(request: Request, session_id: str) -> bool:
    return getattr(request.state, "auth_scheme", "") != "jwt" or session_id == _session_id_for_request(request)


@app.middleware("http")
async def authenticate_request(request: Request, call_next):
    """Keep FastAPI deployment on the same JWT/API-key policy as app.py."""
    if not AuthMiddleware.is_public_endpoint(request.url.path):
        auth_context = type("AuthContext", (), {"headers": request.headers, "path": str(request.url)})()
        if not AuthMiddleware.check_api_key(auth_context):
            return JSONResponse(status_code=401, content={"error": "Unauthorized: Invalid or missing API key"})
        request.state.auth_subject = auth_context.auth_subject
        request.state.auth_tenant_id = auth_context.auth_tenant_id
        request.state.auth_scheme = auth_context.auth_scheme
    return await call_next(request)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _guard_chat(request: Request, data: ChatRequest) -> Optional[JSONResponse]:
    """Apply the same validation/security semantics as the stable handler."""
    text = data.message.strip()
    if not text:
        return JSONResponse(status_code=400, content={"error": "Message cannot be empty"})
    if len(text) > 4000:
        return JSONResponse(status_code=400, content={"error": "Message too long (max 4000 chars)"})

    prompt_result = legacy.prompt_scan(text)
    if not prompt_result.is_safe:
        return JSONResponse(status_code=400, content={
            "error": "输入包含不安全内容，已被拦截",
            "blocked_threats": prompt_result.threats,
        })

    ip = _client_ip(request)
    redis = legacy._redis
    if redis.available:
        limit = redis.check_rate_limit(ip, max_requests=60, window_seconds=60)
        if not limit["allowed"]:
            return JSONResponse(status_code=429, content={
                "error": "请求过于频繁，请稍后再试",
                "retry_after": int(limit["reset_at"] - time.time()) + 1,
            })
    return None


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # Keep exact shipping UI and its fetch/SSE contract.
    path = ROOT / "templates" / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page() -> HTMLResponse:
    return HTMLResponse(legacy.ANALYTICS_HTML)


@app.get("/static/{file_path:path}")
async def static_file(file_path: str):
    target = (ROOT / "static" / file_path).resolve()
    static_root = (ROOT / "static").resolve()
    if not str(target).startswith(str(static_root)) or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Fast liveness probe; readiness remains available at /api/health."""
    return {
        "status": "ok",
        "version": app.version,
        "uptime_seconds": int(time.monotonic() - APP_STARTED_AT),
    }


@app.get("/api/health")
async def health() -> dict[str, Any]:
    # Reuse the known-good health implementation until full endpoint parity lands.
    llm_ok = await asyncio.to_thread(legacy._check_llm_connectivity)
    db: dict[str, Any]
    try:
        async with pool.connection() as conn:
            async with conn.execute("SELECT COUNT(DISTINCT session_id) FROM conversation_history") as cur:
                conversations = (await cur.fetchone())[0]
            async with conn.execute("SELECT COUNT(*), COALESCE(AVG(stars), 0) FROM ratings") as cur:
                ratings = await cur.fetchone()
        db = {"conversations": conversations, "total_ratings": ratings[0], "avg_rating": round(ratings[1], 2) if ratings[0] else 0}
    except Exception as exc:
        db = {"error": str(exc)}
    return {"ok": True, "service": "LangGraph Customer Service Agent", "port": PORT,
            "llm": {"reachable": llm_ok}, "database": db}


@app.post("/api/chat")
async def chat(request: Request, payload: ChatRequest):
    rejected = _guard_chat(request, payload)
    if rejected:
        return rejected

    message = payload.message.strip()
    ip = _client_ip(request)
    session_id = _session_id_for_request(request)
    if payload.session_id and not _owns_session(request, payload.session_id):
        raise HTTPException(status_code=403, detail="Session does not belong to authenticated user")
    legacy.pii_scan(message)  # Preserve non-blocking audit behavior.
    legacy._redis.record_query(message)
    legacy._redis.mark_online(session_id, ttl=300)
    trace = TraceSession(request_id=str(uuid4()), user_id=session_id, input_text=message)
    trace.add_event("request_start", {"ip": ip, "session_id": session_id, "stream": payload.stream})

    if payload.stream:
        async def events() -> AsyncIterator[str]:
            """Bridge the proven synchronous generator without changing frames."""
            queue: asyncio.Queue[object] = asyncio.Queue()
            sentinel = object()
            loop = asyncio.get_running_loop()

            def produce() -> None:
                try:
                    for frame in legacy.run_agent_stream(session_id, message, trace_session=trace):
                        loop.call_soon_threadsafe(queue.put_nowait, frame)
                except Exception as exc:
                    loop.call_soon_threadsafe(queue.put_nowait, _sse({"error": str(exc)}))
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, sentinel)

            task = asyncio.create_task(asyncio.to_thread(produce))
            try:
                while True:
                    frame = await queue.get()
                    if frame is sentinel:
                        break
                    yield str(frame)
            finally:
                await task

        return StreamingResponse(events(), media_type="text/event-stream; charset=utf-8", headers={
            "Cache-Control": "no-cache", "Connection": "close", "X-Accel-Buffering": "no",
        })

    started = time.perf_counter()
    try:
        cached = legacy._redis.get_cached_response(message)
        if cached is not None:
            legacy._redis.record_cache_hit()
            result = {"replies": [{"type": legacy._classify_message(cached), "content": cached}],
                      "interrupted": False, "intent": "cached", "retry_count": 0,
                      "emotion": "neutral", "emotion_intensity": 1, "next_action": "Cached",
                      "session_id": session_id, "cached": True}
        else:
            legacy._redis.record_cache_miss()
            result = await asyncio.to_thread(legacy.run_agent, session_id, message, trace)
            reply_text = "\n".join(r.get("content", "") for r in result.get("replies", []) if isinstance(r, dict)).strip()
            if reply_text:
                legacy._redis.cache_response(message, reply_text, ttl=3600)
        legacy._redis.add_query_log(session_id, message)
        return JSONResponse(result, headers={"X-Response-Time": f"{(time.perf_counter()-started)*1000:.1f}ms"})
    except Exception as exc:
        # Retain legacy JSON shape for existing UI compatibility.
        return JSONResponse({"error": str(exc)}, headers={"X-Response-Time": f"{(time.perf_counter()-started)*1000:.1f}ms"})


@app.post("/api/rating")
async def rating(request: Request, data: RatingRequest) -> dict[str, bool]:
    session_id = data.session_id or _session_id_for_request(request)
    if not _owns_session(request, session_id):
        raise HTTPException(status_code=403, detail="Session does not belong to authenticated user")
    async with pool.connection() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS ratings (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, message_index INTEGER, stars INTEGER, rated_at TEXT)")
        await conn.execute("INSERT INTO ratings (session_id, message_index, stars, rated_at) VALUES (?, ?, ?, ?)",
                           (session_id, data.message_index, data.stars, datetime.now().isoformat()))
    return {"ok": True}


@app.post("/api/reaction")
async def reaction(request: Request, data: ReactionRequest) -> dict[str, bool]:
    session_id = data.session_id or _session_id_for_request(request)
    if not _owns_session(request, session_id):
        raise HTTPException(status_code=403, detail="Session does not belong to authenticated user")
    async with pool.connection() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS reactions (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, message_id TEXT, emoji TEXT, active INTEGER, reacted_at TEXT)")
        await conn.execute("INSERT INTO reactions (session_id, message_id, emoji, active, reacted_at) VALUES (?, ?, ?, ?, ?)",
                           (session_id, data.message_id, data.emoji, int(data.active), datetime.now().isoformat()))
    return {"ok": True}


@app.post("/api/feedback")
async def feedback(request: Request, data: FeedbackRequest):
    session_id = data.session_id or _session_id_for_request(request)
    if not _owns_session(request, session_id):
        raise HTTPException(status_code=403, detail="Session does not belong to authenticated user")
    try:
        from agent.eval_enhanced import save_feedback
        await asyncio.to_thread(save_feedback, session_id, data.query, data.answer, data.rating, data.comment)
        return {"ok": True}
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/api/session/{session_id}")
async def session(request: Request, session_id: str):
    if not _owns_session(request, session_id):
        raise HTTPException(status_code=403, detail="Session does not belong to authenticated user")
    return await asyncio.to_thread(_legacy_session, session_id)


def _legacy_session(session_id: str) -> dict[str, Any]:
    config = {"configurable": {"thread_id": session_id}}
    state = legacy._graph.get_state(config)
    values = getattr(state, "values", {}) or {}
    msgs = []
    for item in values.get("messages", []):
        role = "user" if isinstance(item, legacy.HumanMessage) else "assistant"
        msgs.append({"role": role, "content": item.content})
    return {"messages": msgs, "intent": values.get("intent", "unknown"),
            "emotion": values.get("emotion", "neutral"), "retry_count": values.get("retry_count", 0)}


@app.get("/api/export/{session_id}")
async def export_session(request: Request, session_id: str):
    if not _owns_session(request, session_id):
        raise HTTPException(status_code=403, detail="Session does not belong to authenticated user")
    data = await asyncio.to_thread(_legacy_session, session_id)
    data.update({"session_id": session_id, "exported_at": datetime.now().isoformat(), "message_count": len(data["messages"])})
    return data


@app.get("/api/auth/session")
async def auth_session(request: Request) -> dict[str, str]:
    """Return session_id for JWT-authenticated frontend sessions."""
    if getattr(request.state, "auth_scheme", "") != "jwt":
        return JSONResponse(status_code=400, content={"error": "JWT authentication is required"})
    sid = f"user-{hashlib.sha256(request.state.auth_subject.encode('utf-8')).hexdigest()[:24]}"
    return {"session_id": sid}


@app.post("/api/auth/token")
async def token_exchange(data: TokenRequest):
    """Exchange a configured legacy API key for a short-lived JWT access token."""
    if not AuthMiddleware._validate_key(data.api_key):
        raise HTTPException(status_code=401, detail="Invalid bootstrap API key")
    try:
        token = AuthMiddleware.create_access_token(data.subject.strip(), data.tenant_id.strip() or "default")
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    session_id = f"user-{hashlib.sha256(data.subject.strip().encode('utf-8')).hexdigest()[:24]}"
    return {"access_token": token, "token_type": "bearer", "session_id": session_id}


@app.get("/api/rag/reload")
async def reload_rag():
    try:
        from agent.rag import reload as reload_kb
        docs = await asyncio.to_thread(reload_kb)
        return {"reloaded": True, "documents": len(docs), "sections": sum(len(d["sections"]) for d in docs)}
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/api/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(legacy.metrics.get_prometheus_text(), media_type="text/plain; version=0.0.4")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app_fastapi:app", host="0.0.0.0", port=PORT, log_level="info")
