# -*- coding: utf-8 -*-
"""
app_fastapi.py 鈥?鐢熶骇鍏ュ彛锛圥2 闆嗘垚鐗堬級銆?

鏇夸唬鏃?app_original_sync.py锛堣鏂囦欢淇濈暀涓嶅姩锛屼粎浣滃綊妗ｅ弬鑰冿級锛?
- 涓氬姟鎵ц璧?agent/runner.py锛坓raph.invoke + PostgreSQL PostgresSaver锛夈€?
- 闄愭祦/骞跺彂闂?棰勭畻/涓婁笅鏂囪秴闄?鈫?agent/rate_limiter.py + agent/llm_gateway.py 寮傚父璇箟銆?
- 瑙傛祴鎺ョ嚎锛歛gent/logging_setup + agent/otel_setup + agent/metrics +
  agent/observability锛圱raceSession 璇锋眰鍐呭垱寤猴紝finally finalize_and_save锛?
  AlertService 30s 鍚庡彴浠诲姟锛夈€?
- Feedback and ratings are stored in PostgreSQL through the runtime DB layer.
- prompt 绠＄悊绔細/api/admin/prompts*锛坅gent/prompt_registry 鐘舵€佹満锛夈€?

鍚姩:
    python app_fastapi.py                       # uvicorn 澶?worker锛?WORKERS锛岄粯璁?2锛?
    uvicorn app_fastapi:app --port 7860         # 鍗?worker 璋冭瘯
"""
from __future__ import annotations

# 鈹€鈹€ .env 鍔犺浇锛堝繀椤诲湪璇诲彇浠讳綍 env/config 甯搁噺銆乮mport agent 妯″潡涔嬪墠锛?
#    python-dotenv 鏈畨瑁呮椂闈欓粯璺宠繃锛岃涓轰笌鏃х増涓€鑷达級鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response, StreamingResponse)
from pydantic import BaseModel, Field

# 鈹€鈹€ agent 灞傦紙鍏ㄩ儴 import 瀹夊叏锛氫笁鏂逛緷璧栧唴閮ㄥ畧鍗級鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
from agent import runner
from agent.auth import AuthMiddleware
from agent.http_helpers import (FEEDBACK_DDL, FEEDBACK_INSERT, RATINGS_DDL,
                                RATINGS_INSERT, REACTIONS_DDL, REACTIONS_INSERT,
                                admin_auth_status, admin_prompt_action,
                                idempotency_key_from_headers, query_analytics,
                                query_session_detail, query_sessions, sse_format)
from agent.llm_gateway import BudgetExceededError, ContextOverflowError
from agent.logging_setup import (bind_request_context, clear_request_context,
                                 setup_logging)
from agent.metrics import metrics, record_http_request, record_rate_limit_event
from agent.observability import TraceSession, alert_service, get_trace_service
from agent.otel_setup import setup_otel
from agent.rate_limiter import RateLimitExceeded, get_rate_limiter
from agent.runtime_db import connect as pg_connect, init_runtime_schema

# 鈹€鈹€ 鍙€変笁鏂逛緷璧栵紙瀹堝崼锛夆攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
try:
    from agent.security.prompt_guard import scan_input as _prompt_scan
except Exception:  # pragma: no cover
    _prompt_scan = None

try:
    from agent.security.pii_redactor import scan_and_log as _pii_scan
except Exception:  # pragma: no cover
    _pii_scan = None

try:
    from agent.feedback_store import FeedbackStore
    feedback_store: Optional[Any] = FeedbackStore()
except Exception:  # pragma: no cover
    feedback_store = None

try:
    from agent.prompt_registry import PromptRegistry, seed_default_prompts
    prompt_registry: Optional[Any] = PromptRegistry()
except Exception:  # pragma: no cover
    prompt_registry = None

    def seed_default_prompts(_reg: Any) -> Dict[str, int]:  # type: ignore
        return {}

logger = logging.getLogger("app_fastapi")

APP_STARTED_AT = time.monotonic()
ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "7860"))
TRACE_DB = "postgresql"
SHUTDOWN_DRAIN_SECONDS = float(os.getenv("SHUTDOWN_TIMEOUT_SECONDS", "30"))
CONCURRENCY_WAIT_SECONDS = float(os.getenv("CONCURRENCY_WAIT_SECONDS", "10"))

# 鈹€鈹€ 杩愯鎬侊紙姣?worker 杩涚▼鐙珛锛夆攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
_inflight = 0                     # 瑙傛祴涓棿浠剁淮鎶わ紝shutdown drain 鐢?
_redis_available = False
_alert_task: Optional[asyncio.Task] = None
_memory_backfill_task: Optional[asyncio.Task] = None


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# PostgreSQL short-transaction helpers (live requests never write SQLite)
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

def _sync_db(fn: Callable[[Any], Any]) -> Any:
    """Run a short PostgreSQL transaction outside the event loop."""
    conn = pg_connect()
    try:
        result = fn(conn)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def db_read(fn: Callable[[Any], Any]) -> Any:
    return await asyncio.to_thread(_sync_db, fn)


async def db_write(ddl: str, dml: str, params: tuple) -> None:
    def _write(conn) -> None:
        if ddl.strip():
            conn.execute(ddl)
        conn.execute(dml, params)
    await asyncio.to_thread(_sync_db, _write)


# lifespan
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

async def _alert_loop() -> None:
    """AlertService 鍚庡彴宸℃锛氭瘡 30s 璇勪及涓€娆℃粦鍔ㄧ獥鍙ｈ鍒欍€?"""
    while True:
        await asyncio.sleep(30)
        try:
            for fired in alert_service.check_and_alert():
                logger.warning("ALERT fired: %s", fired)
        except Exception:
            logger.error("alert loop iteration failed", exc_info=True)


def _register_alert_rules() -> None:
    alert_service.add_rule("high_http_error_rate", "http_error", threshold=10,
                           agg="count", window_seconds=300)
    alert_service.add_rule("high_latency_p_avg_ms", "http_latency_ms",
                           threshold=5000, agg="avg", window_seconds=300,
                           min_samples=5)
    alert_service.add_rule("rate_limit_burst", "rate_limited", threshold=30,
                           agg="count", window_seconds=300)
    alert_service.add_rule("limiter_degraded", "limiter_degraded", threshold=0,
                           agg="count", window_seconds=300, cooldown_seconds=300)
    alert_service.add_rule("chat_errors", "chat_error", threshold=5,
                           agg="count", window_seconds=300)


async def _check_redis() -> bool:
    """Check Redis availability without making readiness fail closed."""
    try:
        import redis.asyncio as aioredis  # type: ignore
    except Exception:
        logger.warning("redis package not installed 鈥?rate limiting will run "
                       "fail-closed on the local conservative limiter")
        return False
    try:
        client = aioredis.from_url(
            os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            socket_connect_timeout=2)
        try:
            await asyncio.wait_for(client.ping(), timeout=2)
            return True
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()
    except Exception as exc:
        logger.warning("redis unreachable (%s) 鈥?rate limiting degrades "
                       "fail-closed to local 50%% limits", exc)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis_available, _alert_task, _memory_backfill_task
    import os, time  # must come first to avoid UnboundLocalError

    # 1. logging + otel
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    setup_otel(app)

    # 2. Check Redis before choosing the cross-worker schema-init lock.
    # Docker deployments override REDIS_URL to the Redis service hostname.
    _redis_available = await _check_redis()

    # 3. PostgreSQL + pgvector schema (distributed lock to avoid concurrent DDL deadlocks)
    if _redis_available:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
        lock = _redis_client.lock("schema_init_lock", timeout=30)
        got_lock = await lock.acquire(blocking=True, blocking_timeout=60)
        if got_lock:
            try:
                await asyncio.to_thread(init_runtime_schema)
            finally:
                await lock.release()
        await _redis_client.aclose()
    else:
        # No Redis: use a simple file-based lock that works on Windows too
        lock_path = os.path.join(os.environ.get("TEMP", "/tmp"), "langgraph_schema_init.lock")
        got_lock = False
        for _ in range(600):  # wait up to 60s
            try:
                # O_CREAT|O_EXCL is atomic on Windows too
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                got_lock = True
                break
            except FileExistsError:
                await asyncio.sleep(0.1)
        if got_lock:
            try:
                await asyncio.to_thread(init_runtime_schema)
            finally:
                try:
                    os.unlink(lock_path)
                except Exception:
                    pass
        # else: another worker initialized, skip

    # Restore shared observability counters after the schema exists. This is
    # deliberately best-effort and never blocks chat startup on metrics.
    await asyncio.to_thread(metrics.load_persistent_rag_metrics)

    # 2. graph / checkpointer 棰勭儹锛坙anggraph 缂哄腑鏃堕檷绾у惎鍔紝chat 杩斿洖 503锛?
    graph_ok = await runner.prewarm()
    logger.info("graph prewarm: %s", "ok" if graph_ok else "unavailable")

    # 鍚戦噺绱㈠紩鍚姩棰勫缓(鍚庡彴,涓嶉樆濉炲惎鍔?:閬垮厤棣栦釜鐢ㄦ埛璇锋眰鎵挎媴鍏ㄩ噺
    # embedding 鍐峰惎鍔ㄣ€俁AG_BACKEND=pgvector 鏃跺悜閲忓湪搴撻噷,鏃犻渶棰勫缓銆?
    _index_task = None
    if os.getenv("RAG_BACKEND", "").strip().lower() != "pgvector":
        async def _prebuild_vector_index():
            try:
                from agent import vector_rag
                await asyncio.to_thread(vector_rag.build_index)
                logger.info("vector index prebuild: ok")
            except Exception as exc:  # 棰勫缓澶辫触涓嶅奖鍝嶆湇鍔?鎳掑姞杞藉厹搴?
                logger.warning("vector index prebuild failed: %s", exc)
        _index_task = asyncio.create_task(_prebuild_vector_index())

    # 4. redis 鎺㈡祴锛堝畧鍗紝涓嶉樆鏂級
    # Redis availability was checked before schema initialization.

    # 5. trace service锛堥娆¤皟鐢ㄥ喅瀹?db 璺緞锛? prompt registry seed
    get_trace_service()
    if prompt_registry is not None:
        try:
            seeded = seed_default_prompts(prompt_registry)
            if seeded:
                logger.info("prompt registry seeded: %s", seeded)
        except Exception:
            logger.warning("prompt registry seed failed", exc_info=True)

    # 6. Memory embedding backfill (background, non-blocking)
    _backfill_limit = int(os.getenv("MEMORY_EMBED_BACKFILL_LIMIT", "200") or "0")
    if _backfill_limit > 0:
        async def _backfill_memory_embeddings():
            try:
                from agent.user_memory import get_memory_store
                res = await asyncio.to_thread(
                    get_memory_store().backfill_missing_embeddings,
                    None, "default", _backfill_limit)
                logger.info("memory embedding backfill: %s", res)
            except Exception as exc:
                logger.warning("memory embedding backfill failed: %s", exc)
        _memory_backfill_task = asyncio.create_task(_backfill_memory_embeddings())

    # 7. Alert rules + background loop
    _register_alert_rules()
    _alert_task = asyncio.create_task(_alert_loop())

    logger.info("app started (port=%s, redis=%s, graph=%s, database=postgresql)",
                PORT, _redis_available, graph_ok)
    try:
        yield
    finally:
        # shutdown: 鍋滃悗鍙颁换鍔?鈫?drain 鍦ㄩ€旇姹傦紙鈮?0s锛夆啋 鍏抽棴璧勬簮
        if _alert_task is not None:
            _alert_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _alert_task
        if _memory_backfill_task is not None:
            _memory_backfill_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _memory_backfill_task
        deadline = time.monotonic() + SHUTDOWN_DRAIN_SECONDS
        while _inflight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if _inflight > 0:
            logger.warning("shutdown drain timeout with %d in-flight requests",
                           _inflight)
        await runner.shutdown()
        logger.info("app shutdown complete")


app = FastAPI(title="LangGraph Customer Service Agent",
              version="3.0.0", lifespan=lifespan)

# CORS锛堥粯璁ゅ悓婧愰儴缃诧紱璺ㄥ煙鐢?CORS_ALLOW_ORIGINS=閫楀彿鍒嗛殧瑕嗙洊锛?
_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
                 if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# 涓棿浠讹細瑙傛祴锛坢etrics + request_id + alert锛変笌 auth
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲


# Browser login fallback cookie. The Vue app still sends X-User-Id when it can,
# but some local browser shells can lose JS storage on refresh. A first-party
# cookie lets the backend recover the stable PostgreSQL/pgvector user id instead
# of falling back to anon-<ip>.
AUTH_USER_COOKIE = "aster_user_id"
AUTH_NAME_COOKIE = "aster_username"
AUTH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30


def _safe_cookie_value(value: str, limit: int = 128) -> str:
    return (value or "").strip()[:limit]


def _set_auth_cookies(response: Response, user_id: str, username: str = "") -> None:
    uid = _safe_cookie_value(user_id)
    name = _safe_cookie_value(username or uid)
    if not uid:
        return
    cookie_kwargs = {
        "max_age": AUTH_COOKIE_MAX_AGE,
        "path": "/",
        "samesite": "lax",
        "secure": False,
        "httponly": False,
    }
    response.set_cookie(AUTH_USER_COOKIE, uid, **cookie_kwargs)
    response.set_cookie(AUTH_NAME_COOKIE, name, **cookie_kwargs)


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(AUTH_USER_COOKIE, path="/", samesite="lax")
    response.delete_cookie(AUTH_NAME_COOKIE, path="/", samesite="lax")


@app.middleware("http")
async def observe_request(request: Request, call_next):
    """璁?http 鎸囨爣 + 缁戝畾 request_id 鏃ュ織涓婁笅鏂?+ 鍠?alert 婊戝姩绐楀彛 + 鍦ㄩ€旇鏁般€?"""
    global _inflight
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    request.state.request_id = request_id
    tokens = bind_request_context(request_id=request_id)
    start = time.perf_counter()
    _inflight += 1
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        _inflight -= 1
        duration = time.perf_counter() - start
        path = request.url.path
        try:
            record_http_request(request.method, path, status, duration)
            alert_service.record("http_request", 1)
            alert_service.record("http_latency_ms", duration * 1000)
            if status >= 500:
                alert_service.record("http_error", 1)
        except Exception:
            logger.debug("metrics recording failed", exc_info=True)
        clear_request_context(tokens)


@app.middleware("http")
async def authenticate_request(request: Request, call_next):
    """娌跨敤 AuthMiddleware锛圝WT / API key / query 鍙傛暟锛夊绾︼紝骞惰В鍑?user_id銆?

    **鍏抽敭鏂板 request.state.user_id 鈥斺€?闀挎湡璁板繂涓婚敭**锛岀嫭绔嬩簬 session_id锛?
      - Bearer <jwt> 鏈夋晥 鈫?user_id = claims.sub锛堣法浼氳瘽绋冲畾锛夈€?
      - 鍚﹀垯 X-User-Id 澶达紙鍓嶇鍖垮悕鏍囪瘑锛夈€?
      - 閮芥病鏈?鈫?鍖垮悕鍥為€€ anon-<ip 鍝堝笇>锛屼繚璇佹棫鍖垮悕娴佺▼涓嶇牬鍧忋€?
    """
    request.state.auth_scheme = ""
    request.state.auth_subject = ""
    request.state.auth_tenant_id = "default"
    request.state.user_id = ""

    # 鍏堝皾璇曡В JWT锛堝嵆浣跨鐐规槸鍏紑鐨勶紝涔熻鎷垮埌 user_id 鐢ㄤ簬璁板繂褰掑睘锛夈€?
    auth_header = request.headers.get("Authorization", "") or ""
    jwt_claims = None
    if auth_header.startswith("Bearer "):
        jwt_claims = AuthMiddleware._decode_jwt(auth_header[7:].strip())

    if not AuthMiddleware.is_public_endpoint(request.url.path):
        ctx = type("AuthContext", (), {
            "headers": request.headers, "path": str(request.url)})()
        if not AuthMiddleware.check_api_key(ctx):
            return JSONResponse(status_code=401, content={
                "error": "Unauthorized: Invalid or missing API key"})
        request.state.auth_subject = ctx.auth_subject
        request.state.auth_tenant_id = ctx.auth_tenant_id
        request.state.auth_scheme = ctx.auth_scheme

    # Resolve the stable user id for PostgreSQL/pgvector ownership:
    # valid JWT > X-User-Id > login cookie > anonymous IP hash.
    if jwt_claims and jwt_claims.get("sub"):
        request.state.user_id = str(jwt_claims["sub"])[:128]
        request.state.auth_tenant_id = str(
            jwt_claims.get("tenant_id", request.state.auth_tenant_id) or "default")
        if not request.state.auth_scheme:
            request.state.auth_scheme = "jwt"
            request.state.auth_subject = request.state.user_id
    else:
        header_uid = (request.headers.get("X-User-Id", "") or "").strip()
        cookie_uid = (request.cookies.get(AUTH_USER_COOKIE, "") or "").strip()
        if header_uid:
            request.state.user_id = header_uid[:128]
        elif cookie_uid:
            request.state.user_id = cookie_uid[:128]
            if not request.state.auth_scheme:
                request.state.auth_scheme = "cookie"
            if not request.state.auth_subject:
                request.state.auth_subject = request.state.user_id
        else:
            ip = _client_ip(request)
            request.state.user_id = f"anon-{hashlib.sha1(ip.encode()).hexdigest()[:16]}"
    return await call_next(request)


def _user_id_for_request(request: Request) -> str:
    """闀挎湡璁板繂涓婚敭锛堜腑闂翠欢宸插啓鍏?request.state.user_id锛夈€?"""
    return getattr(request.state, "user_id", "") or "anon-unknown"


def _tenant_for_request(request: Request) -> str:
    return getattr(request.state, "auth_tenant_id", "default") or "default"


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    record_rate_limit_event(exc.layer)
    alert_service.record("rate_limited", 1)
    return JSONResponse(
        status_code=429,
        content={"error": "璇锋眰杩囦簬棰戠箒锛岃绋嶅悗鍐嶈瘯", "layer": exc.layer,
                 "retry_after": round(exc.retry_after, 1)},
        headers={"Retry-After": str(max(1, int(exc.retry_after + 0.999)))})


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# 璇锋眰妯″瀷
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

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
    emoji: str = "馃憤"
    active: bool = True


class FeedbackRequest(BaseModel):
    session_id: str = ""
    query: str = ""
    answer: str = ""
    rating: int = 0
    comment: str = ""


class ApproveRequest(BaseModel):
    version: int
    percent: int = 10
    name: str = "system_prompt"


class PromptActionRequest(BaseModel):
    name: str = "system_prompt"


class TokenRequest(BaseModel):
    api_key: str = Field(min_length=1)
    subject: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(default="default", min_length=1, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: Optional[str] = Field(default=None, max_length=256)
    tenant_id: str = Field(default="default", min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: Optional[str] = Field(default=None, max_length=256)
    display_name: Optional[str] = Field(default=None, max_length=128)
    tenant_id: str = Field(default="default", min_length=1, max_length=128)


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# 浼氳瘽褰掑睘
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _session_id_for_request(request: Request) -> str:
    if getattr(request.state, "auth_scheme", "") == "jwt":
        subject = request.state.auth_subject
        return f"user-{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:24]}"
    ip = _client_ip(request)
    return f"ip-{hashlib.sha1(ip.encode('utf-8')).hexdigest()[:16]}"


def _owns_session(request: Request, session_id: str, *,
                  allow_unregistered: bool = False) -> bool:
    """Check whether a request can access a session.

    JWT users may create a previously unregistered client session only through
    ``/api/chat``. Read/export/feedback endpoints must reject an unknown
    session id instead of treating it as public.
    """
    if getattr(request.state, "auth_scheme", "") != "jwt":
        return True
    if session_id == _session_id_for_request(request):
        return True
    try:
        from agent import memory
        owner = memory.get_session_owner(session_id)
    except Exception:
        # JWT ownership lookup failures must fail closed.
        return False
    if owner is None:
        return allow_unregistered
    return owner == _user_id_for_request(request)


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# 闈欐€侀〉闈?
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Show the archive notice for the retired 7860 web UI.

    Port 7860 remains the FastAPI backend/API. The active Vue development UI
    runs separately at http://localhost:5173/ and proxies /api requests here.
    The former 7860 page is preserved under archive/ and is no longer served
    as the default application shell.
    """
    notice = ROOT / "archive" / "legacy-frontend-7860.html"
    return HTMLResponse(notice.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page() -> HTMLResponse:
    path = ROOT / "templates" / "analytics.html"
    if path.is_file():
        return HTMLResponse(path.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/assets/{file_path:path}")
async def vue_assets(file_path: str):
    """Serve Vue frontend built assets (*.js, *.css)."""
    target = (ROOT / "frontend" / "dist" / "assets" / file_path).resolve()
    assets_root = (ROOT / "frontend" / "dist" / "assets").resolve()
    if not str(target).startswith(str(assets_root)) or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/static/{file_path:path}")
async def static_file(file_path: str):
    target = (ROOT / "static" / file_path).resolve()
    static_root = (ROOT / "static").resolve()
    if not str(target).startswith(str(static_root)) or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target)


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# 鍋ュ悍 / 灏辩华 / 鎸囨爣
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

@app.get("/healthz")
async def healthz() -> dict:
    """绾瓨娲绘帰閽堬紙docker healthcheck / k8s livenessProbe锛夈€?"""
    return {"status": "ok", "version": app.version,
            "uptime_seconds": int(time.monotonic() - APP_STARTED_AT)}


@app.get("/readyz")
@app.get("/api/ready")
async def ready() -> JSONResponse:
    """灏辩华鎺㈤拡锛歅ostgreSQL + pgvector + graph + 鍏抽敭閰嶇疆銆?"""
    checks: Dict[str, Any] = {}
    ok = True

    def _probe(conn) -> bool:
        row = conn.execute(
            "SELECT current_database() AS db, EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector') AS vector"
        ).fetchone()
        if not row or not row["vector"]:
            raise RuntimeError("pgvector extension is not available")
        return True

    try:
        await db_read(_probe)
        checks["postgresql"] = {"ok": True, "pgvector": True}
    except Exception as exc:
        ok = False
        checks["postgresql"] = {"ok": False, "error": str(exc)}

    checks["redis"] = {"ok": _redis_available,
                       "degraded_mode": not _redis_available}
    checks["graph"] = {"ok": runner._graph is not None}
    if runner._graph is None:
        ok = False
    checks["config"] = {
        "ok": bool(os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_KEY")),
        "openai_base_url": bool(os.getenv("OPENAI_BASE_URL")),
        "jwt_configured": bool(os.getenv("JWT_SECRET", "").strip()),
        "api_keys_configured": bool(os.getenv("API_KEYS", "").strip()),
    }
    return JSONResponse(status_code=200 if ok else 503,
                        content={"ready": ok, "checks": checks})


async def _llm_reachable(timeout: float = 3.0) -> bool:
    """LLM 杩為€氭€ф帰娴嬶紙httpx 瀹堝崼锛?s锛屽け璐ヤ笉鑷村懡锛夈€?"""
    try:
        import httpx  # type: ignore
    except Exception:
        return False
    base = os.getenv("OPENAI_BASE_URL", "http://localhost:8080").rstrip("/")
    if base.endswith("/v1"):
        models_url = base + "/models"
    else:
        models_url = base + "/v1/models"
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.get(models_url, headers={
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', 'sk-local')}"})
            return resp.status_code < 500
    except Exception:
        return False


@app.get("/api/health")
async def health() -> dict:
    llm_ok = await _llm_reachable()
    db: Dict[str, Any]
    try:
        stats = await db_read(query_analytics)
        db = {"conversations": stats["total_conversations"],
              "total_ratings": stats["ratings"]["total"],
              "avg_rating": stats["ratings"]["average"]}
    except Exception as exc:
        db = {"error": str(exc)}
    limiter_stats: Dict[str, Any] = {}
    with contextlib.suppress(Exception):
        limiter_stats = get_rate_limiter().get_stats()
    return {"ok": True, "service": "LangGraph Customer Service Agent",
            "version": app.version, "port": PORT,
            "llm": {"reachable": llm_ok},
            "redis": {"available": _redis_available},
            "rate_limiter": limiter_stats,
            "database": db}


_METRICS_DASHBOARD_HTML = '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LangGraph | Observability Dashboard</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1220;--panel:#121c2e;--line:#263752;--text:#e8eef8;--muted:#91a4be;--cyan:#67e8f9;--green:#34d399;--red:#fb7185}*{box-sizing:border-box}body{margin:0;min-height:100vh;color:var(--text);font-family:Inter,system-ui,"Microsoft YaHei",sans-serif;background:radial-gradient(circle at 8% 0%,#1b4268 0,transparent 35%),linear-gradient(135deg,#0b1220,#101a30)}main{max-width:1180px;margin:auto;padding:34px 22px 48px}header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:26px}h1{margin:0 0 8px;font-size:clamp(27px,4vw,42px);letter-spacing:-.04em}.subtitle,.muted,.hint{color:var(--muted)}.subtitle{margin:0;font-size:14px}.right{display:flex;gap:9px;flex-wrap:wrap;justify-content:flex-end}.badge{display:inline-block;border:1px solid #38bdf866;border-radius:999px;padding:6px 11px;font-size:12px;font-weight:700;color:#bae6fd;background:#0c4a6e99}.badge.ok{color:#a7f3d0;border-color:#10b98166;background:#064e3b99}.badge.bad{color:#fecdd3;border-color:#fb718566;background:#88133799}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}.card,.panel{background:linear-gradient(145deg,#17263eee,#101a2bee);border:1px solid var(--line);border-radius:18px;box-shadow:0 18px 55px #02061755}.card{padding:18px;min-height:124px}.title{color:#b8c6da;font-size:13px}.value{margin:12px 0 5px;font-size:30px;font-weight:800}.cyan .value{color:var(--cyan)}.green .value{color:var(--green)}.columns{display:grid;grid-template-columns:1.1fr .9fr;gap:18px;margin-bottom:18px}.panel{padding:20px}.panel h2{margin:0 0 15px;font-size:17px}.services{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.service{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:13px;background:#0b1424aa;border:1px solid #22334f;border-radius:12px;font-size:13px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:11px 8px;border-bottom:1px solid #22334f}th{color:#9fb0c8;font-size:12px}code,pre{font-family:Consolas,"SFMono-Regular",monospace}code{color:#a5f3fc}details{border-top:1px solid var(--line);padding-top:14px;margin-top:17px}summary{cursor:pointer;color:#bde9f3;font-size:13px}pre{white-space:pre-wrap;word-break:break-word;max-height:430px;overflow:auto;color:#aabbd3;font-size:11px;line-height:1.55;background:#08111f;border-radius:12px;padding:14px}.footer{display:flex;justify-content:space-between;gap:12px;margin-top:17px;color:var(--muted);font-size:12px}a{color:var(--cyan);text-decoration:none}@media(max-width:820px){.grid{grid-template-columns:repeat(2,1fr)}.columns{grid-template-columns:1fr}header{flex-direction:column}.right{justify-content:flex-start}}@media(max-width:520px){main{padding:24px 14px 36px}.grid,.services{grid-template-columns:1fr}.value{font-size:27px}}\n</style></head><body><main>\n<header><div><h1>LangGraph | Observability Dashboard</h1><p class="subtitle">&#35266;&#27979;&#38754;&#26495; | PostgreSQL / pgvector | Redis | Local LLM</p></div><div class="right"><span id="status" class="badge">Loading</span><span class="badge">Auto refresh 15s</span></div></header>\n<section class="grid"><article class="card cyan"><div class="title">HTTP Requests</div><div id="requests" class="value">-</div><div id="errors" class="hint">Loading</div></article><article class="card green"><div class="title">Average Latency</div><div id="latency" class="value">-</div><div id="latencyHint" class="hint">Loading</div></article><article class="card cyan"><div class="title">LLM Calls</div><div id="llm" class="value">-</div><div id="llmHint" class="hint">Loading</div></article><article class="card green"><div class="title">Tokens / Cost</div><div id="tokens" class="value">-</div><div id="cost" class="hint">Loading</div></article></section>\n<div class="columns"><section class="panel"><h2>&#26381;&#21153;&#29366;&#24577; | Service Status</h2><div id="services" class="services"><div class="muted">Loading service status...</div></div><div class="footer"><span id="uptime">Uptime -</span><span id="families">Metric families -</span></div></section><section class="panel"><h2>&#25968;&#25454;&#19982;&#36136;&#37327; | Data Quality</h2><div class="services"><div class="service"><span>&#23545;&#35805;&#25968; | Conversations</span><strong id="conversations">-</strong></div><div class="service"><span>&#35780;&#20998;&#25968; | Ratings</span><strong id="ratings">-</strong></div><div class="service"><span>&#24179;&#22343;&#35780;&#20998; | Avg rating</span><strong id="avgRating">-</strong></div><div class="service"><span>RAG &#21629;&#20013;&#29575; | Hit ratio</span><strong id="rag">-</strong></div><div class="service"><span>&#38480;&#27969;&#20107;&#20214; | Rate limits</span><strong id="limits">-</strong></div><div class="service"><span>&#21453;&#39304;&#20107;&#20214; | Feedback</span><strong id="feedback">-</strong></div></div></section></div>\n<section class="panel"><h2>LLM Gateway | Routing / Reliability / Cache</h2><div class="services"><div class="service"><span>Fallbacks</span><strong id="fallbacks">-</strong></div><div class="service"><span>Routes</span><strong id="routes">-</strong></div><div class="service"><span>Average TTFT</span><strong id="ttft">-</strong></div><div class="service"><span>Cache hit rate</span><strong id="cache">-</strong></div><div class="service"><span>Circuit breaker open</span><strong id="breaker">-</strong></div><div class="service"><span>Attributed cost</span><strong id="attribution">-</strong></div></div></section>\n<section class="panel"><h2>&#25509;&#21475;&#35831;&#27714;&#25490;&#34892; | Top Endpoints</h2><table><thead><tr><th>Endpoint</th><th>Requests</th></tr></thead><tbody id="endpoints"><tr><td colspan="2" class="muted">Loading...</td></tr></tbody></table><details><summary>&#26597;&#30475;&#21407;&#22987; Prometheus &#25351;&#26631; | Raw metrics</summary><pre id="raw">Loading...</pre></details></section>\n<div class="footer"><span id="updated">-</span><span><a href="/api/metrics?format=prometheus">Prometheus text</a> | <a href="/api/health">Health</a> | <a href="/api/ready">Readiness</a></span></div>\n</main><script>\nconst $=id=>document.getElementById(id),fmt=n=>Number(n||0).toLocaleString("zh-CN",{maximumFractionDigits:2});\nfunction parse(t){const a=[];for(const line of t.split(/\\r?\\n/)){if(!line||line[0]==="#")continue;const m=line.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\\{([^}]*)\\})?\\s+([-+]?\\d+(?:\\.\\d+)?(?:[eE][-+]?\\d+)?|NaN|[+-]?Inf)/);if(!m)continue;const l={};for(const x of (m[2]||"").matchAll(/([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\\\.|[^"])*)"/g))l[x[1]]=x[2];const v=Number(m[3]);if(Number.isFinite(v))a.push({name:m[1],labels:l,value:v})}return a}\nconst sum=(a,n,p=()=>true)=>a.filter(x=>x.name===n&&p(x.labels)).reduce((v,x)=>v+x.value,0),badge=ok=>`<span class="badge ${ok?"ok":"bad"}">${ok?"\\u6b63\\u5e38":"\\u5f02\\u5e38"}</span>`;\nasync function refresh(){try{const [m,h,r]=await Promise.all([fetch("/api/metrics?format=prometheus"),fetch("/api/health"),fetch("/api/ready")]);const text=await m.text(),health=await h.json(),ready=await r.json(),a=parse(text);const req=sum(a,"http_requests_total"),err=sum(a,"http_requests_total",l=>Number(l.status)>=500),count=sum(a,"http_request_duration_seconds_count"),duration=sum(a,"http_request_duration_seconds_sum"),llm=sum(a,"llm_requests_total"),llmErr=sum(a,"llm_requests_total",l=>l.outcome!=="success"),input=sum(a,"llm_tokens_total",l=>l.direction==="input"),output=sum(a,"llm_tokens_total",l=>l.direction==="output"),cost=sum(a,"llm_cost_yuan_total"),attribution=sum(a,"llm_cost_attribution_yuan_total"),fallbacks=sum(a,"llm_fallback_total"),routes=sum(a,"llm_route_total"),ttftCount=sum(a,"llm_ttft_seconds_count"),ttftSum=sum(a,"llm_ttft_seconds_sum"),cacheHits=sum(a,"cache_events_total",l=>l.result==="hit"),cacheMisses=sum(a,"cache_events_total",l=>l.result==="miss"),breakerOpen=sum(a,"circuit_breaker_state",l=>true),ragQueries=sum(a,"rag_queries_total"),ragHits=sum(a,"rag_hits_total"),ragSample=a.find(x=>x.name==="rag_hit_ratio"),rag=ragSample?.value||0,limits=sum(a,"rate_limit_events_total"),feedback=sum(a,"feedback_events_total"),eps={};a.filter(x=>x.name==="http_requests_total").forEach(x=>eps[x.labels.endpoint||"unknown"]=(eps[x.labels.endpoint||"unknown"]||0)+x.value);const db=health.database||{},llmOk=!!health.llm?.reachable,dbOk=!db.error,redisOk=!!health.redis?.available,graphOk=!!ready.checks?.graph?.ok,all=llmOk&&dbOk&&redisOk&&graphOk;\n$("status").className="badge "+(all?"ok":"bad");$("status").textContent=all?"\\u7cfb\\u7edf\\u6b63\\u5e38":"\\u90e8\\u5206\\u5f02\\u5e38";$("requests").textContent=fmt(req);$("errors").textContent=`\\u9519\\u8bef ${fmt(err)} | \\u9519\\u8bef\\u7387 ${req?(err/req*100).toFixed(2):"0.00"}%`;$("latency").textContent=`${fmt(count?duration/count*1000:0)} ms`;$("latencyHint").textContent=`${fmt(count)} requests`;$("llm").textContent=fmt(llm);$("llmHint").textContent=`\\u5931\\u8d25 ${fmt(llmErr)} calls`;$("tokens").textContent=fmt(input+output);$("cost").textContent=`in ${fmt(input)} | out ${fmt(output)} | CNY ${Number(cost||0).toFixed(4)}`;$("fallbacks").textContent=fmt(fallbacks);$("routes").textContent=fmt(routes);$("ttft").textContent=ttftCount?`${fmt(ttftSum/ttftCount*1000)} ms`:"-";const cacheTotal=cacheHits+cacheMisses;$("cache").textContent=cacheTotal?`${(cacheHits/cacheTotal*100).toFixed(1)}% (${fmt(cacheHits)}/${fmt(cacheTotal)})`:"-";$("breaker").textContent=breakerOpen?"OPEN":"CLOSED";$("attribution").textContent=`CNY ${Number(attribution||0).toFixed(4)}`;$("services").innerHTML=`<div class="service"><span>FastAPI / Graph</span>${badge(graphOk)}</div><div class="service"><span>Local LLM</span>${badge(llmOk)}</div><div class="service"><span>PostgreSQL + pgvector</span>${badge(dbOk)}</div><div class="service"><span>Redis</span>${badge(redisOk)}</div>`;$("uptime").textContent=`Uptime ${fmt(health.uptime_seconds)}s`;$("families").textContent=`Metric families ${new Set(a.map(x=>x.name)).size}`;$("conversations").textContent=fmt(db.conversations);$("ratings").textContent=fmt(db.total_ratings);$("avgRating").textContent=String(db.avg_rating??"-");$("rag").textContent=ragQueries?`${(rag*100).toFixed(1)}% (${fmt(ragHits)}/${fmt(ragQueries)})`:"No data";$("limits").textContent=fmt(limits);$("feedback").textContent=fmt(feedback);$("endpoints").innerHTML=Object.entries(eps).sort((x,y)=>y[1]-x[1]).slice(0,12).map(([e,v])=>`<tr><td><code>${e.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}</code></td><td>${fmt(v)}</td></tr>`).join("")||\'<tr><td colspan="2" class="muted">No request data</td></tr>\';$("raw").textContent=text;$("updated").textContent=`Updated ${new Date().toLocaleString("zh-CN")} | Aggregates only; no messages or secrets`;\n}catch(e){$("status").className="badge bad";$("status").textContent="Read failed";$("updated").textContent="Metrics read failed: "+e.message}}\nrefresh();setInterval(refresh,15000);\n</script></body></html>'

@app.get("/api/metrics")
async def metrics_endpoint(request: Request) -> Response:
    text, content_type = metrics.render()
    requested_format = request.query_params.get("format", "").lower()
    accept = request.headers.get("accept", "").lower()
    accepts_html = "text/html" in accept
    if requested_format in {"prometheus", "text", "plain"}:
        return PlainTextResponse(text, media_type=content_type)
    if requested_format in {"html", "dashboard"} or accepts_html:
        return HTMLResponse(_METRICS_DASHBOARD_HTML)
    return PlainTextResponse(text, media_type=content_type)


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# /api/chat
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

def _guard_chat(message: str) -> Optional[JSONResponse]:
    if not message:
        return JSONResponse(status_code=400,
                            content={"error": "Message cannot be empty"})
    if len(message) > 4000:
        return JSONResponse(status_code=400,
                            content={"error": "Message too long (max 4000 chars)"})
    if _prompt_scan is not None:
        result = _prompt_scan(message)
        if not result.is_safe:
            return JSONResponse(status_code=400, content={
                "error": "杈撳叆鍖呭惈涓嶅畨鍏ㄥ唴瀹癸紝宸茶鎷︽埅",
                "blocked_threats": result.threats})
    return None


def _record_implicit_signals(session_id: str, message: str) -> None:
    """姣忚疆鐢ㄦ埛娑堟伅锛氳繛缁拷闂娴嬶紙P4 闅愬紡淇″彿锛宼o_thread 鍐呰皟鐢級銆?"""
    if feedback_store is not None:
        with contextlib.suppress(Exception):
            feedback_store.record_repeat_question(session_id, message)


async def _record_escalation(session_id: str, message: str,
                             result: Dict[str, Any]) -> None:
    if feedback_store is None:
        return
    last_reply = ""
    for r in result.get("replies", []):
        last_reply = r.get("content", last_reply)
    with contextlib.suppress(Exception):
        await asyncio.to_thread(
            feedback_store.record_escalation, session_id,
            query=message, answer=last_reply,
            reason=f"retries={result.get('retry_count', 0)}")


async def _run_with_overflow_retry(session_id: str, message: str,
                                   trace: TraceSession,
                                   idem_key: Optional[str],
                                   user_id: str = "",
                                    tenant_id: str = "default") -> Dict[str, Any]:
    """ContextOverflowError 鈫?瑙﹀彂涓€娆″帇缂╅噸璇曪紱浠嶆孩鍑哄垯涓婃姏缁欑鐐硅浆 413銆?"""
    try:
        return await runner.run(session_id, message, trace_session=trace,
                                idempotency_key=idem_key, user_id=user_id, tenant_id=tenant_id)
    except ContextOverflowError:
        trace.add_event("context_overflow", {"action": "compact_and_retry"})
        with contextlib.suppress(Exception):
            from agent.context_compaction import get_compactor
            get_compactor()  # 棰勭儹鍘嬬缉鍣紱graph 鑺傜偣鍦ㄩ噸璇曟椂鎵ц瀹為檯鍘嬬缉
        return await runner.run(session_id, message, trace_session=trace,
                                idempotency_key=idem_key, user_id=user_id,
                                tenant_id=tenant_id)


@app.post("/api/chat")
async def chat(request: Request, payload: ChatRequest):
    message = payload.message.strip()
    rejected = _guard_chat(message)
    if rejected is not None:
        return rejected

    session_id = _session_id_for_request(request)
    memory_user_id = _user_id_for_request(request)  # 闀挎湡璁板繂涓婚敭
    tenant_id = _tenant_for_request(request)
    if payload.session_id:
        owns_requested_session = _owns_session(
            request, payload.session_id, allow_unregistered=True)
        if not owns_requested_session:
            raise HTTPException(status_code=403,
                                detail="Session does not belong to authenticated user")
        session_id = payload.session_id

    if _pii_scan is not None:  # 闈為樆鏂璁?
        with contextlib.suppress(Exception):
            _pii_scan(message)

    # 鍒嗗眰闄愭祦锛圧ateLimitExceeded 鈫?鍏ㄥ眬 429 handler锛?
    limiter = get_rate_limiter()
    user_id = getattr(request.state, "auth_subject", "") or session_id
    await limiter.acquire(user_id=user_id, ip=_client_ip(request),
                          session_id=session_id)

    idem_key = idempotency_key_from_headers(request.headers)
    trace = TraceSession(request_id=getattr(request.state, "request_id", "")
                         or uuid4().hex,
                         user_id=user_id, session_id=session_id,
                         input_text=message)
    trace.add_event("request_start", {"session_id": session_id,
                                      "stream": payload.stream,
                                      "idempotency_key": bool(idem_key)})
    await asyncio.to_thread(_record_implicit_signals, session_id, message)

    if payload.stream:
        return StreamingResponse(
            _chat_sse(request, session_id, message, trace, idem_key,
                      memory_user_id, tenant_id),
            media_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"})

    started = time.perf_counter()
    try:
        async with limiter.concurrency(timeout=CONCURRENCY_WAIT_SECONDS):
            result = await _run_with_overflow_retry(session_id, message,
                                                    trace, idem_key,
                                                    memory_user_id, tenant_id)
        if result.get("interrupted"):
            result["reply_type"] = "escalated"
            await _record_escalation(session_id, message, result)
        return JSONResponse(result, headers={
            "X-Response-Time": f"{(time.perf_counter() - started) * 1000:.1f}ms"})
    except BudgetExceededError as exc:
        retry_after = max(1, int(getattr(exc, "retry_after", 60) or 60))
        return JSONResponse(status_code=429,
                            content={"error": "budget exhausted; please retry later",
                                     "reason": "budget_exceeded"},
                            headers={"Retry-After": str(retry_after)})
    except ContextOverflowError:
        return JSONResponse(status_code=413,
                            content={"error": "瀵硅瘽涓婁笅鏂囪繃闀匡紝璇峰紑鍚柊浼氳瘽"})
    except asyncio.TimeoutError:
        alert_service.record("chat_error", 1)
        return JSONResponse(status_code=504,
                            content={"error": "璇锋眰澶勭悊瓒呮椂锛岃绋嶅悗鍐嶈瘯"})
    except RateLimitExceeded:
        raise
    except Exception as exc:
        alert_service.record("chat_error", 1)
        logger.error("chat failed: %s", exc, exc_info=True)
        return JSONResponse(status_code=500,
                            content={"error": "service temporarily unavailable; please retry later"})
    finally:
        await get_trace_service(TRACE_DB).finalize_and_save(trace)


async def _chat_sse(request: Request, session_id: str, message: str,
                    trace: TraceSession,
                    idem_key: Optional[str],
                    user_id: str = "",
                     tenant_id: str = "default") -> AsyncIterator[str]:
    """SSE 浜嬩欢娴侊細甯?= {"progress"} / {"token"} / {"done":true,...} / {"error"}銆?

    姣忓抚鍙戦€佸墠妫€鏌?request.is_disconnected()锛屾柇杩炵珛鍗冲彇娑?runner 浠诲姟
    锛坅sync generator aclose 鍦?finally 閲屽叧闂簳灞?astream锛夈€?
    """
    limiter = get_rate_limiter()
    gen = None
    try:
        async with limiter.concurrency(timeout=CONCURRENCY_WAIT_SECONDS):
            gen = runner.run_stream(session_id, message, trace_session=trace,
                                    idempotency_key=idem_key, user_id=user_id,
                                    tenant_id=tenant_id)
            async for frame in gen:
                if await request.is_disconnected():
                    logger.info("SSE client disconnected; cancelling run "
                                "(session=%s)", session_id)
                    break
                if frame.get("done") and frame.get("interrupted"):
                    frame["reply_type"] = "escalated"
                    await _record_escalation(session_id, message,
                                             {"replies": [],
                                              "retry_count": 0})
                yield sse_format(frame)
    except RateLimitExceeded as exc:
        record_rate_limit_event(exc.layer)
        yield sse_format({"error": "request rate limited; please retry later",
                          "retry_after": round(exc.retry_after, 1)})
    except BudgetExceededError:
        yield sse_format({"error": "budget exhausted; please retry later"})
    except ContextOverflowError:
        yield sse_format({"error": "context too long; please start a new session"})
    except asyncio.TimeoutError:
        alert_service.record("chat_error", 1)
        yield sse_format({"error": "request timed out; please retry later"})
    except Exception as exc:
        alert_service.record("chat_error", 1)
        logger.error("SSE chat failed: %s", exc, exc_info=True)
        yield sse_format({"error": "service temporarily unavailable; please retry later"})
    finally:
        if gen is not None:
            with contextlib.suppress(Exception):
                await gen.aclose()
        await get_trace_service(TRACE_DB).finalize_and_save(trace)


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# Feedback endpoints (PostgreSQL + feedback_store)
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

@app.post("/api/rating")
async def rating(request: Request, data: RatingRequest) -> dict:
    session_id = data.session_id or _session_id_for_request(request)
    if not _owns_session(request, session_id):
        raise HTTPException(status_code=403,
                            detail="Session does not belong to authenticated user")
    await db_write(RATINGS_DDL, RATINGS_INSERT,
                   (session_id, data.message_index, data.stars,
                    datetime.now().isoformat()))
    if feedback_store is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(feedback_store.record_rating, session_id,
                                    data.stars,
                                    request_id=f"msg-{data.message_index}")
    metrics.record_feedback("rating")
    return {"ok": True}


@app.post("/api/reaction")
async def reaction(request: Request, data: ReactionRequest) -> dict:
    session_id = data.session_id or _session_id_for_request(request)
    if not _owns_session(request, session_id):
        raise HTTPException(status_code=403,
                            detail="Session does not belong to authenticated user")
    await db_write(REACTIONS_DDL, REACTIONS_INSERT,
                   (session_id, data.message_id, data.emoji, int(data.active),
                    datetime.now().isoformat()))
    if feedback_store is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(feedback_store.record_reaction, session_id,
                                    data.emoji, data.active,
                                    request_id=data.message_id)
    metrics.record_feedback("reaction")
    return {"ok": True}


@app.post("/api/feedback")
async def feedback(request: Request, data: FeedbackRequest) -> dict:
    session_id = data.session_id or _session_id_for_request(request)
    if not _owns_session(request, session_id):
        raise HTTPException(status_code=403,
                            detail="Session does not belong to authenticated user")
    await db_write(FEEDBACK_DDL, FEEDBACK_INSERT,
                   (session_id, data.query, data.answer, data.rating,
                    data.comment, datetime.now().isoformat()))
    if feedback_store is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(feedback_store.record_feedback, session_id,
                                    data.query, data.answer, data.rating,
                                    data.comment)
    metrics.record_feedback("feedback_form")
    return {"ok": True}


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# Sessions / analytics (PostgreSQL)
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

@app.get("/api/sessions")
async def sessions(request: Request, search: str = "") -> dict:
    """杩斿洖**褰撳墠 user_id 鐨勫巻鍙蹭細璇濆垪琛?*锛堟爣棰?鏃堕棿/娑堟伅鏁帮級銆?

    褰㈢姸锛歿"user_id", "sessions": [{session_id, title, created_at,
    last_active, message_count}, ...]}銆俿earch 涓虹┖鏃跺垪鍑鸿鐢ㄦ埛鍏ㄩ儴浼氳瘽锛?
    JWT 鏈厤缃殑鍖垮悕鐢ㄦ埛鎸?anon-<ip> user_id 褰掑睘銆?
    """
    from agent import memory
    user_id = _user_id_for_request(request)
    try:
        rows = await asyncio.to_thread(memory.list_user_sessions, user_id, 100)
        if search:
            s = search.strip()
            rows = [r for r in rows if s in (r.get("title") or "")]
        return {"user_id": user_id, "sessions": rows}
    except Exception as exc:
        logger.error("sessions query failed: %s", exc)
        return {"user_id": user_id, "sessions": [], "error": str(exc)}


def _session_owner(session_id: str) -> Optional[str]:
    from agent import memory
    return memory.get_session_owner(session_id)


@app.get("/api/session/{session_id}")
async def session_detail(request: Request, session_id: str) -> dict:
    # 褰掑睘鏍￠獙锛氫紭鍏堟寜 user_id锛坰essions 琛級锛屽洖閫€鏃?session 褰掑睘閫昏緫銆?
    owner = await asyncio.to_thread(_session_owner, session_id)
    if owner is not None and owner != _user_id_for_request(request):
        raise HTTPException(status_code=403,
                            detail="Session does not belong to authenticated user")
    if owner is None and not _owns_session(request, session_id):
        raise HTTPException(status_code=403,
                            detail="Session does not belong to authenticated user")
    try:
        return await db_read(lambda conn: query_session_detail(conn, session_id))
    except Exception as exc:
        return {"session_id": session_id, "messages": [], "error": str(exc)}


@app.get("/api/analytics")
async def analytics() -> dict:
    try:
        return await db_read(query_analytics)
    except Exception as exc:
        logger.error("analytics query failed: %s", exc)
        return {"total_conversations": 0, "avg_reply_length": 0,
                "ratings": {"total": 0, "average": 0},
                "tickets": {"total": 0, "by_priority": {}},
                "intents": {}, "emotions": {}, "error": str(exc)}


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# /api/memory 鈥?褰撳墠鐢ㄦ埛鐨勯暱鏈熻蹇嗭紙鍙煡鐪?鍒犻櫎鍗曟潯锛岀敤鎴峰彲缂栬緫锛?
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

@app.get("/api/memory")
async def memory_list(request: Request) -> dict:
    """鍒楀嚭褰撳墠 user_id 鐨勯暱鏈熻蹇嗭紙user_id/tenant 鍦ㄥ瓨鍌ㄥ眰 SQL 纭繃婊わ級銆?

    褰㈢姸锛歿"user_id", "memories": [{id, content, kind, importance, created_at}]}
    """
    try:
        from agent.user_memory import get_memory_store
    except Exception:
        return {"user_id": _user_id_for_request(request), "memories": [],
                "error": "memory store unavailable"}
    user_id = _user_id_for_request(request)
    tenant = _tenant_for_request(request)
    try:
        items = await asyncio.to_thread(
            get_memory_store().list_memories, user_id, tenant)
        return {"user_id": user_id, "memories": items}
    except Exception as exc:
        logger.error("memory list failed: %s", exc)
        return {"user_id": user_id, "memories": [], "error": str(exc)}


@app.post("/api/memory/backfill")
async def memory_backfill(request: Request, limit: int = 100) -> dict:
    """Fill missing pgvector embeddings for the current user's memories."""
    try:
        from agent.user_memory import get_memory_store
    except Exception:
        raise HTTPException(status_code=503, detail="memory store unavailable")
    user_id = _user_id_for_request(request)
    tenant = _tenant_for_request(request)
    res = await asyncio.to_thread(
        get_memory_store().backfill_missing_embeddings,
        user_id, tenant, limit)
    return {"ok": True, "user_id": user_id, **res}


@app.delete("/api/memory/{memory_id}")
async def memory_delete(request: Request, memory_id: str) -> dict:
    """鍒犻櫎褰撳墠鐢ㄦ埛鐨勪竴鏉￠暱鏈熻蹇嗭紙瀛樺偍灞傛寜 user_id 纭牎楠岋紝鍙兘鍒犺嚜宸辩殑锛夈€?"""
    try:
        from agent.user_memory import get_memory_store
    except Exception:
        raise HTTPException(status_code=503, detail="memory store unavailable")
    user_id = _user_id_for_request(request)
    tenant = _tenant_for_request(request)
    ok = await asyncio.to_thread(
        get_memory_store().delete_memory, user_id, memory_id, tenant)
    if not ok:
        raise HTTPException(status_code=404,
                            detail="memory not found or not owned by user")
    return {"ok": True, "deleted": memory_id}


@app.get("/api/export/{session_id}")
async def export_session(request: Request, session_id: str) -> dict:
    if not _owns_session(request, session_id):
        raise HTTPException(status_code=403,
                            detail="Session does not belong to authenticated user")
    data = await db_read(lambda conn: query_session_detail(conn, session_id))
    data["exported_at"] = datetime.now().isoformat()
    return data


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# auth token 绔偣锛堟部鐢ㄦ棫濂戠害锛?
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

@app.get("/api/auth/session")
async def auth_session(request: Request):
    if getattr(request.state, "auth_scheme", "") != "jwt":
        return JSONResponse(status_code=400,
                            content={"error": "JWT authentication is required"})
    return {"session_id": _session_id_for_request(request)}


@app.post("/api/auth/token")
async def token_exchange(data: TokenRequest):
    if not AuthMiddleware._validate_key(data.api_key):
        raise HTTPException(status_code=401, detail="Invalid bootstrap API key")
    try:
        token = AuthMiddleware.create_access_token(
            data.subject.strip(), data.tenant_id.strip() or "default")
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    sid = f"user-{hashlib.sha256(data.subject.strip().encode('utf-8')).hexdigest()[:24]}"
    return {"access_token": token, "token_type": "bearer", "session_id": sid}


# 鈹€鈹€ 杞婚噺鐢ㄦ埛鐧诲綍 / 娉ㄥ唽 / me锛坲ser_id = username锛汮WT 鎵胯浇韬唤锛夆攢鈹€鈹€鈹€鈹€鈹€

def _issue_user_token(user_id: str, tenant_id: str) -> Optional[str]:
    """绛惧彂鐢ㄦ埛 JWT锛汮WT_SECRET 鏈厤缃椂杩斿洖 None锛堝墠绔彲鐢?X-User-Id 鍏滃簳锛夈€?"""
    try:
        from agent.auth import create_access_token
        return create_access_token(user_id, tenant=tenant_id)
    except ValueError:
        return None


@app.post("/api/auth/register")
async def auth_register(data: RegisterRequest):
    """?????username ? user_id???????? created=False?"""
    from agent import memory
    user_id = data.username.strip()
    tenant_id = data.tenant_id.strip() or "default"
    res = await asyncio.to_thread(
        memory.create_user, user_id, data.password,
        data.display_name, tenant_id)
    token = _issue_user_token(user_id, tenant_id)
    payload = {"ok": True, "user_id": user_id, "created": res["created"],
               "access_token": token, "token_type": "bearer"}
    response = JSONResponse(payload)
    _set_auth_cookies(response, user_id, data.display_name or user_id)
    return response

@app.post("/api/auth/login")
async def auth_login(data: LoginRequest):
    """杞婚噺鐧诲綍锛歶sername 鍗?user_id銆?

    - users 琛ㄦ湁 password_hash 鈫?鏍￠獙瀵嗙爜锛?
    - 鏃?password_hash 鎴栫敤鎴蜂笉瀛樺湪 鈫?鍏嶅瘑/棣栨鐧诲綍鍗虫敞鍐屻€?
    鎴愬姛杩斿洖 JWT + user_id锛圝WT_SECRET 鏈厤缃椂 access_token=None锛?
    鍓嶇鏀圭敤 X-User-Id 澶存壙杞借韩浠斤紝闀挎湡璁板繂浠嶆寜 user_id 褰掑睘锛夈€?
    """
    from agent import memory
    user_id = data.username.strip()
    tenant_id = data.tenant_id.strip() or "default"
    try:
        res = await asyncio.to_thread(
            memory.authenticate_user, user_id, data.password, True)
        if not res["ok"]:
            raise HTTPException(status_code=401, detail=res["reason"])
        token = _issue_user_token(user_id, tenant_id)
    except HTTPException:
        raise
    except Exception as exc:  # 鎶婄湡瀹炲師鍥犳毚闇插嚭鏉?涓嶅啀瑁?500
        logger.exception("auth_login failed for %s", user_id)
        raise HTTPException(status_code=500,
                            detail=f"login failed: {type(exc).__name__}: {exc}")
    payload = {"ok": True, "user_id": user_id, "registered": res["registered"],
               "access_token": token, "token_type": "bearer",
               "session_id": f"user-{hashlib.sha256(user_id.encode()).hexdigest()[:24]}"}
    response = JSONResponse(payload)
    _set_auth_cookies(response, user_id, user_id)
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"ok": True})
    _clear_auth_cookies(response)
    return response

@app.get("/api/auth/me")
async def auth_me(request: Request):
    """杩斿洖褰撳墠璇锋眰鐨勮韩浠斤紙JWT sub / X-User-Id / 鍖垮悕锛夈€?"""
    user_id = _user_id_for_request(request)
    return {"user_id": user_id,
            "tenant_id": _tenant_for_request(request),
            "auth_scheme": getattr(request.state, "auth_scheme", "") or "anonymous",
            "authenticated": getattr(request.state, "auth_scheme", "") == "jwt"}


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# /api/admin/prompts 鈥?prompt registry 绠＄悊绔紙JWT scope=admin锛?
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

def _admin_gate(request: Request) -> None:
    jwt_configured = bool(os.getenv("JWT_SECRET", "").strip())
    claims = None
    auth_header = request.headers.get("Authorization", "") or ""
    if auth_header.startswith("Bearer "):
        claims = AuthMiddleware._decode_jwt(auth_header[7:].strip())
    allowed, status, reason = admin_auth_status(jwt_configured, claims)
    if not allowed:
        raise HTTPException(status_code=status, detail=reason)
    if prompt_registry is None:
        raise HTTPException(status_code=503,
                            detail="prompt registry unavailable")


@app.get("/api/admin/prompts")
async def admin_prompts_list(request: Request, name: str = "system_prompt"):
    _admin_gate(request)
    status, body = await asyncio.to_thread(
        admin_prompt_action, prompt_registry, "list", {"name": name})
    return JSONResponse(status_code=status, content=body)


@app.post("/api/admin/prompts/approve")
async def admin_prompts_approve(request: Request, data: ApproveRequest):
    _admin_gate(request)
    status, body = await asyncio.to_thread(
        admin_prompt_action, prompt_registry, "approve",
        {"version": data.version, "percent": data.percent, "name": data.name})
    return JSONResponse(status_code=status, content=body)


@app.post("/api/admin/prompts/promote")
async def admin_prompts_promote(request: Request, data: PromptActionRequest):
    _admin_gate(request)
    status, body = await asyncio.to_thread(
        admin_prompt_action, prompt_registry, "promote", {"name": data.name})
    return JSONResponse(status_code=status, content=body)


@app.post("/api/admin/prompts/rollback")
async def admin_prompts_rollback(request: Request, data: PromptActionRequest):
    _admin_gate(request)
    status, body = await asyncio.to_thread(
        admin_prompt_action, prompt_registry, "rollback", {"name": data.name})
    return JSONResponse(status_code=status, content=body)


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# 鍏ュ彛
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app_fastapi:app", host="0.0.0.0", port=PORT,
                workers=int(os.getenv("WORKERS", "1")),
                log_level=os.getenv("LOG_LEVEL", "info").lower())

