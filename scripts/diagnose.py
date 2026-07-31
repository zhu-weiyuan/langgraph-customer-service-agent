# -*- coding: utf-8 -*-
"""
scripts/diagnose.py — 一键真实环境诊断（启动服务器之前运行）。

用法（Windows，项目根目录）:
    python scripts\\diagnose.py
    python scripts\\diagnose.py --server http://127.0.0.1:7860   # 附带在线服务检查

设计约束：
  * 仅用 stdlib + 项目已有依赖；所有三方 import 全部 try/except 守卫。
  * 任何单项检查失败都不会让脚本崩溃 — 输出 [FAIL]/[SKIP] + 中文修复提示。
  * 退出码 = FAIL 数（上限 250），供 CI / 批处理判断。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import traceback
import urllib.request
import urllib.error

# ── 项目根目录定位 + cwd 归一（相对路径 data/trace.db 等依赖 cwd）─────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LEAKED_KEY_PREFIX = "sk-ckmnb"   # .env.example 中泄露过的 key 前缀

# 输出基础设施：ANSI 颜色（Win10+ 启用 VT）、结果收集  # ══════

def _enable_ansi() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:  # Win10+ 打开虚拟终端转义序列
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False

_ANSI = _enable_ansi()
_COLORS = {"PASS": "\x1b[32m", "FAIL": "\x1b[31m", "WARN": "\x1b[33m", "SKIP": "\x1b[36m"}
RESULTS: list = []          # (status, group, name, detail)
_GROUP_ORDER = ["环境", "依赖", "模块导入", "数据层", "外部服务", "图冒烟", "HTTP层"]

def _emit(status: str, group: str, name: str, detail: str = "", hint: str = "") -> None:
    RESULTS.append((status, group, name, detail))
    tag = f"[{status}]"
    if _ANSI:
        tag = f"{_COLORS.get(status, '')}{tag}\x1b[0m"
    line = f"  {tag} {name}"
    if detail:
        line += f" — {detail}"
    if hint:
        line += f" (修复: {hint})"
    try:
        print(line)
    except UnicodeEncodeError:  # Windows GBK 控制台兜底
        print(line.encode("gbk", errors="replace").decode("gbk"))

def ok(g, n, d=""):   _emit("PASS", g, n, d)
def fail(g, n, d="", hint=""): _emit("FAIL", g, n, d, hint)
def warn(g, n, d="", hint=""): _emit("WARN", g, n, d, hint)
def skip(g, n, d="", hint=""): _emit("SKIP", g, n, d, hint)

def section(title: str) -> None:
    print(f"\n=== {title} ===")

def guarded(group: str, name: str, hint: str = ""):
    """装饰器：单项检查隔离，异常 → FAIL（绝不让脚本崩溃）。"""
    def deco(fn):
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as exc:
                fail(group, name, f"{type(exc).__name__}: {exc}", hint)
                return None
        return wrapper
    return deco

def compact_tb(exc: BaseException, frames: int = 4) -> str:
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    text = "".join(lines).strip().splitlines()
    return " | ".join(text[-frames:])

# A. 环境  # ══════

def load_dotenv_fallback(path: str) -> int:
    """手动解析 .env（python-dotenv 缺席时兜底），返回加载条数。"""
    count = 0
    with io.open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
                count += 1
    return count

@guarded("环境", "A组整体")
def check_env() -> None:
    g = "环境"
    section("A. 环境")
    # Python 版本
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        ok(g, "Python 版本", f"{v.major}.{v.minor}.{v.micro}")
    else:
        fail(g, "Python 版本", f"{v.major}.{v.minor} < 3.10", "安装 Python 3.10+（langgraph 需要）")
    # .env 加载
    env_path = os.path.join(ROOT, ".env")
    if not os.path.isfile(env_path):
        fail(g, ".env 文件", "不存在", "复制 .env.example 为 .env 并填入你自己的 OPENAI_API_KEY")
    else:
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
            ok(g, ".env 加载", "python-dotenv")
        except ImportError:
            n = load_dotenv_fallback(env_path)
            warn(g, ".env 加载", f"手动解析 {n} 条（python-dotenv 未装）", "pip install python-dotenv")
    # OPENAI_API_KEY
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        fail(g, "OPENAI_API_KEY", "未设置或为空", "在 .env 里配置 OPENAI_API_KEY=sk-...")
    elif key.startswith(LEAKED_KEY_PREFIX):
        fail(g, "OPENAI_API_KEY", "!! CRITICAL: 正在使用已泄露的示例 key " f"({LEAKED_KEY_PREFIX}...)", "该 key 曾提交进 .env.example 已公开泄露，立刻到服务商后台吊销并更换新 key")
    else:
        ok(g, "OPENAI_API_KEY", f"已设置 ({key[:7]}...{key[-4:]}, {len(key)} 字符)")
    # OPENAI_BASE_URL
    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not base:
        warn(g, "OPENAI_BASE_URL", "未设置，将用默认 https://api.xiaomimimo.com/v1", "在 .env 里显式配置你的网关地址（须含 /v1）")
    elif not base.startswith(("http://", "https://")):
        fail(g, "OPENAI_BASE_URL", f"格式非法: {base}", "应形如 https://host/v1（含协议头）")
    elif not base.rstrip("/").endswith("/v1"):
        warn(g, "OPENAI_BASE_URL", f"{base} 不以 /v1 结尾", "约定 base_url 包含 /v1（llm_client/llm_gateway 会在其后拼 /chat/completions）")
    else:
        ok(g, "OPENAI_BASE_URL", base)
    # 可选变量报告
    for var in ("OPENAI_MODEL", "EMBEDDING_MODEL", "PG_DSN", "RAG_BACKEND", "REDIS_URL", "PORT"):
        val = os.environ.get(var, "").strip()
        if val:
            shown = val if var != "PG_DSN" else re.sub(r":[^:@/]+@", ":***@", val)
            ok(g, f"可选 {var}", shown)
        else:
            skip(g, f"可选 {var}", "未设置（使用内部默认值）")

# B. 依赖  # ══════

def _mod_version(name: str) -> str:
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:
        try:
            return getattr(importlib.import_module(name), "__version__", "?")
        except Exception:
            return "?"

@guarded("依赖", "B组整体")
def check_deps() -> dict:
    g = "依赖"
    section("B. 依赖")
    present: dict = {}
    required = [("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                ("langgraph", "langgraph"), ("aiosqlite", "aiosqlite"),
                ("httpx", "httpx"), ("jieba", "jieba"),
                ("dotenv", "python-dotenv")]
    optional = [("redis", "redis"), ("psycopg", "psycopg[binary]"),
                ("pgvector", "pgvector"), ("prometheus_client", "prometheus-client"),
                ("structlog", "structlog"), ("tiktoken", "tiktoken"),
                ("locust", "locust")]
    for mod, pkg in required:
        try:
            importlib.import_module(mod)
            present[mod] = True
            ok(g, f"必需 {mod}", f"v{_mod_version(pkg.split('[')[0])}")
        except Exception as exc:
            present[mod] = False
            fail(g, f"必需 {mod}", f"{type(exc).__name__}: {exc}", f"pip install {pkg}")
    # AsyncSqliteSaver（checkpointer 关键路径）
    try:
        m = importlib.import_module("langgraph.checkpoint.sqlite.aio")
        getattr(m, "AsyncSqliteSaver")
        present["checkpoint_sqlite"] = True
        ok(g, "langgraph.checkpoint.sqlite (AsyncSqliteSaver)", "可导入")
    except Exception as exc:
        present["checkpoint_sqlite"] = False
        fail(g, "langgraph.checkpoint.sqlite (AsyncSqliteSaver)", f"{type(exc).__name__}: {exc}", "pip install langgraph-checkpoint-sqlite")
    for mod, pkg in optional:
        try:
            importlib.import_module(mod)
            present[mod] = True
            ok(g, f"可选 {mod}", f"v{_mod_version(pkg.split('[')[0])}")
        except Exception:
            present[mod] = False
            skip(g, f"可选 {mod}", "未安装", f"需要时 pip install {pkg}")
    return present

# C. 模块导入  # ══════

@guarded("模块导入", "C组整体")
def check_imports(deps: dict) -> bool:
    g = "模块导入"
    section("C. 模块导入 (agent/*.py + app_fastapi)")
    all_ok = True
    agent_dir = os.path.join(ROOT, "agent")
    names = sorted(f[:-3] for f in os.listdir(agent_dir) if f.endswith(".py") and f != "__init__.py")
    for name in names:
        mod = f"agent.{name}"
        try:
            importlib.import_module(mod)
            ok(g, mod)
        except Exception as exc:
            all_ok = False
            fail(g, mod, f"{type(exc).__name__}: {exc}", "语法/导入错误，按提示修改该文件；缺依赖则 pip install")
    if deps.get("fastapi"):
        try:
            importlib.import_module("app_fastapi")
            ok(g, "app_fastapi")
        except Exception as exc:
            all_ok = False
            fail(g, "app_fastapi", compact_tb(exc, 3), "服务入口无法导入，服务器将无法启动")
    else:
        skip(g, "app_fastapi", "fastapi 未安装，跳过", "先安装依赖")
    return all_ok

# D. 数据层  # ══════

@guarded("数据层", "D组整体")
def check_data() -> None:
    g = "数据层"
    section("D. 数据层")
    # trace.db schema 迁移检查（TRACE_DB env → data/trace.db → agent/trace.db）
    candidates = [os.environ.get("TRACE_DB", "").strip() or None, "data/trace.db", "agent/trace.db"]
    trace_path = next((p for p in candidates if p and os.path.isfile(p)), None)
    if trace_path is None:
        skip(g, "trace.db schema", "尚不存在（首次启动会自动创建）")
    else:
        try:
            conn = sqlite3.connect(trace_path, timeout=5)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(traces)")]
            conn.close()
            if not cols:
                warn(g, "trace.db schema", f"{trace_path} 无 traces 表", "启动时 TraceService 会自动建表")
            elif "session_id" in cols:
                ok(g, "trace.db schema", f"{trace_path} traces 表含 session_id")
            else:
                fail(g, "trace.db schema", f"{trace_path} traces 表缺 session_id 列", "启动时 observability 会自动补列；若报错可删除旧 trace.db 重建")
        except Exception as exc:
            fail(g, "trace.db schema", f"{type(exc).__name__}: {exc}", "文件可能损坏或被占用，关闭占用进程或删除重建")
    # user_memory.db
    mem = os.path.join(ROOT, "user_memory.db")
    if not os.path.isfile(mem):
        skip(g, "user_memory.db", "尚不存在（首次请求会自动创建）")
    else:
        try:
            conn = sqlite3.connect(mem, timeout=5)
            tables = [r[0] for r in conn.execute( "SELECT name FROM sqlite_master WHERE type='table'")]
            conn.close()
            ok(g, "user_memory.db", f"可读，{len(tables)} 张表: " + ", ".join(tables[:8]) + ("..." if len(tables) > 8 else ""))
        except Exception as exc:
            fail(g, "user_memory.db", f"{type(exc).__name__}: {exc}", "检查文件权限 / 是否被其他进程锁定")
    # checkpoints 目录可写
    ckpt = os.environ.get("CHECKPOINT_DB", "checkpoints/checkpoints.db")
    ckpt_dir = os.path.dirname(ckpt) or "."
    try:
        os.makedirs(ckpt_dir, exist_ok=True)
        probe = os.path.join(ckpt_dir, f".diag_probe_{os.getpid()}")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        ok(g, "checkpoints 目录可写", os.path.abspath(ckpt_dir))
    except Exception as exc:
        fail(g, "checkpoints 目录可写", f"{ckpt_dir}: {exc}", "检查目录权限，或用 CHECKPOINT_DB 环境变量指到可写路径")
    # prompt_registry
    try:
        from agent.prompt_registry import PromptRegistry, seed_default_prompts
        reg = PromptRegistry()
        seeded = seed_default_prompts(reg)
        ok(g, "prompt_registry", f"打开 {reg.db_path}，seed={seeded or '已就绪'}")
    except Exception as exc:
        fail(g, "prompt_registry", f"{type(exc).__name__}: {exc}", "检查 data/p4_self_improve.db 是否可写（或设 P4_DB_PATH）")
    # feedback_store 插入 + 回滚
    try:
        from agent.feedback_store import FeedbackStore
        store = FeedbackStore()
        conn = store._conn()
        try:
            conn.execute(
                "INSERT INTO bad_cases (ts, session_id, request_id, query, answer,"
                " signal_type, score, comment, trace_ref, processed)"
                " VALUES (?,?,?,?,?,?,?,?,?,0)",
                (time.time(), "diag-test", "diag", "诊断测试", "测试",
                 "rating", 5.0, "diagnose.py 探针", ""))
            conn.rollback()   # 测试行不落库
        finally:
            conn.close()
        ok(g, "feedback_store", f"{store.db_path} 插入+回滚成功")
    except Exception as exc:
        fail(g, "feedback_store", f"{type(exc).__name__}: {exc}", "检查 data/ 目录可写；schema 冲突可删 p4_self_improve.db 重建")

# E. 外部服务  # ══════

def _http_post_json(url: str, payload: dict, headers: dict, timeout: float) -> tuple:
    """stdlib POST，返回 (status, body_dict_or_text)。httpx 优先。"""
    data = json.dumps(payload).encode("utf-8")
    headers = dict(headers or {})
    headers.setdefault("Content-Type", "application/json")
    try:
        import httpx
        r = httpx.post(url, content=data, headers=headers, timeout=timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text[:200]
    except ImportError:
        pass
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, body[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:200]

@guarded("外部服务", "E组整体")
def check_services(deps: dict) -> bool:
    g = "外部服务"
    section("E. 外部服务（网络探测，3-5s 超时）")
    llm_ok = False
    # LLM chat ping
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    base = (os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.xiaomimimo.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "mimo-v2.5")
    if not key:
        skip(g, "LLM chat ping", "OPENAI_API_KEY 未设置", "配置 .env 后重跑")
    else:
        try:
            t0 = time.perf_counter()
            status, body = _http_post_json(
                f"{base}/chat/completions",
                {"model": model, "max_tokens": 8,
                 "messages": [{"role": "user", "content": "ping，回复 ok"}]},
                {"Authorization": f"Bearer {key}"},
                timeout=float(os.environ.get("DIAG_LLM_TIMEOUT", "30")))
            ms = (time.perf_counter() - t0) * 1000
            if status == 200:
                reply = ""
                if isinstance(body, dict):
                    reply = ((body.get("choices") or [{}])[0] .get("message", {}).get("content", "") or "")[:40]
                llm_ok = True
                ok(g, "LLM chat ping", f"{model} {ms:.0f}ms 回复={reply!r}")
            elif status == 401:
                fail(g, "LLM chat ping", f"401 未授权 ({ms:.0f}ms)", "OPENAI_API_KEY 无效或已吊销，去服务商后台重新生成")
            elif status == 404:
                fail(g, "LLM chat ping", f"404 ({base}/chat/completions)", "OPENAI_BASE_URL 路径不对，确认以 /v1 结尾")
            else:
                fail(g, "LLM chat ping", f"HTTP {status}: {str(body)[:120]}", "检查 base_url / model 名 / 账户余额")
        except Exception as exc:
            fail(g, "LLM chat ping", f"{type(exc).__name__}: {exc}",
                 "网络不通或超时(默认30s，本地大模型可 set DIAG_LLM_TIMEOUT=60)：检查代理/防火墙/base_url，"
                 "或确认本地模型服务已完成加载")
    # Embeddings ping
    try:
        from agent.embedding_client import EmbeddingClient
        client = EmbeddingClient.from_env(strict=False)
        if client is None:
            skip(g, "Embeddings ping", "OPENAI_API_KEY 未设置")
        else:
            client.timeout, client.max_retries = 10.0, 0
            req_dim = getattr(client, "dimensions", None)
            t0 = time.perf_counter()
            vec = client.embed_one("诊断测试")
            ms = (time.perf_counter() - t0) * 1000
            detail = f"{client.model} 维度={len(vec)}"
            if req_dim:
                detail += f" (请求降维至 {req_dim})"
            detail += f" {ms:.0f}ms"
            if len(vec) > 2000:
                warn(g, "Embeddings ping",
                     detail + " — 超过 pgvector 索引上限 2000 维",
                     "在 .env 设置 EMBEDDING_DIMENSIONS=1024(MRL 降维),或换 1024 维模型如 Qwen/Qwen3-Embedding-0.6B")
            else:
                ok(g, "Embeddings ping", detail)
    except Exception as exc:
        fail(g, "Embeddings ping", f"{type(exc).__name__}: {str(exc)[:150]}", "检查 EMBEDDING_MODEL 是否被网关支持；401 则换 key")
    # Redis
    if not deps.get("redis"):
        skip(g, "Redis PING", "redis 包未安装（限流将降级到本地保守限额）", "pip install redis")
    else:
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        try:
            import redis as redis_pkg
            r = redis_pkg.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
            r.ping()
            ok(g, "Redis PING", url)
        except Exception as exc:
            warn(g, "Redis PING", f"{url} 不可达: {exc}", "启动 redis-server 或配置 REDIS_URL；不启用则限流降级但可运行")
    # Postgres + pgvector
    dsn = (os.environ.get("PG_DSN", "").strip() or os.environ.get("RAG_PG_DSN", "").strip())
    if not dsn:
        skip(g, "Postgres/pgvector", "PG_DSN 未设置（RAG_BACKEND=pgvector 才需要）")
    elif not deps.get("psycopg"):
        fail(g, "Postgres/pgvector", "配置了 PG_DSN 但 psycopg 未安装", "pip install psycopg[binary] pgvector")
    else:
        try:
            import psycopg
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
                    has_vec = cur.fetchone() is not None
                    rows = "-"
                    cur.execute("SELECT to_regclass('rag_chunks')")
                    if cur.fetchone()[0]:
                        cur.execute("SELECT count(*) FROM rag_chunks")
                        rows = cur.fetchone()[0]
            if has_vec:
                ok(g, "Postgres/pgvector", f"连接成功，pgvector 已安装，rag_chunks 行数={rows}")
            else:
                fail(g, "Postgres/pgvector", "连接成功但缺 vector 扩展", "在库里执行 CREATE EXTENSION vector;")
        except Exception as exc:
            fail(g, "Postgres/pgvector", f"{type(exc).__name__}: {str(exc)[:120]}", "检查 PG_DSN、数据库是否启动、网络/密码")
    return llm_ok

# F. 图冒烟  # ══════

@guarded("图冒烟", "F组整体")
def check_graph_smoke(imports_ok: bool, llm_ok: bool) -> None:
    g = "图冒烟"
    section("F. 图冒烟（MemorySaver + 一轮 '你好'）")
    if not imports_ok:
        skip(g, "graph.ainvoke", "C 组模块导入未全部通过", "先修复导入错误")
        return
    if not llm_ok:
        skip(g, "graph.ainvoke", "E 组 LLM ping 未通过（会白等超时）", "先修复 LLM 连通性")
        return
    try:
        from langgraph.checkpoint.memory import MemorySaver
        from agent.graph import build_graph
        from agent import runner
        graph = build_graph(MemorySaver())
        t0 = time.perf_counter()
        result = asyncio.run(runner.run("diag-smoke", "你好", graph=graph, timeout=30))
        ms = (time.perf_counter() - t0) * 1000
        intent = result.get("intent", "unknown")
        reply = "\n".join(r.get("content", "") for r in result.get("replies", [])).strip()
        if reply:
            ok(g, "graph.ainvoke", f"{ms:.0f}ms intent={intent} 回复 {len(reply)} 字: {reply[:50]!r}")
        else:
            fail(g, "graph.ainvoke", f"{ms:.0f}ms intent={intent} 但回复为空", "检查 generate_reply 节点日志 / LLM 返回内容")
    except asyncio.TimeoutError:
        fail(g, "graph.ainvoke", "30s 超时", "LLM 太慢或节点死循环；查 GRAPH_TIMEOUT_SECONDS 与网关延迟")
    except Exception as exc:
        fail(g, "graph.ainvoke", compact_tb(exc, 4), "按上方 traceback 定位节点错误")

# G. HTTP 层（--server 时启用）  # ══════

def validate_prometheus(text: str) -> list:
    """校验 Prometheus 文本：'# TYPE' 行不得含 '{'（标签写进 TYPE 是格式错误）。"""
    return [ln for ln in text.splitlines()
            if ln.startswith("# TYPE") and "{" in ln]

def _server_get(base: str, path: str, timeout: float = 5.0) -> tuple:
    req = urllib.request.Request(base + path)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status, resp.read().decode("utf-8", "replace"), (time.perf_counter() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return (e.code, e.read().decode("utf-8", "replace")[:200], (time.perf_counter() - t0) * 1000)

@guarded("HTTP层", "G组整体")
def check_http(server: str) -> None:
    g = "HTTP层"
    section(f"G. HTTP 层（{server}）")
    base = server.rstrip("/")
    for path in ("/healthz", "/api/ready", "/api/sessions", "/api/analytics"):
        try:
            status, body, ms = _server_get(base, path)
            if status == 200:
                ok(g, f"GET {path}", f"200 {ms:.0f}ms")
            elif path == "/api/ready" and status == 503:
                fail(g, f"GET {path}", f"503 未就绪 {ms:.0f}ms: {body[:120]}", "看响应里哪个 check 为 false（graph/sqlite/config）")
            else:
                fail(g, f"GET {path}", f"HTTP {status} {ms:.0f}ms")
        except Exception as exc:
            fail(g, f"GET {path}", f"{type(exc).__name__}: {exc}", "服务器是否已启动？端口是否正确？")
    # /api/metrics + Prometheus 格式校验
    try:
        status, body, ms = _server_get(base, "/api/metrics")
        bad = validate_prometheus(body) if status == 200 else []
        if status != 200:
            fail(g, "GET /api/metrics", f"HTTP {status}")
        elif bad:
            fail(g, "GET /api/metrics", f"TYPE 行含 '{{': {bad[0][:80]}", "agent/metrics.py 的 render() 把标签写进 TYPE 行了")
        else:
            ok(g, "GET /api/metrics", f"200 {ms:.0f}ms，Prometheus 格式合法")
    except Exception as exc:
        fail(g, "GET /api/metrics", f"{type(exc).__name__}: {exc}")
    # POST /api/chat 非流式
    try:
        t0 = time.perf_counter()
        status, body = _http_post_json(base + "/api/chat", {"message": "你好", "stream": False}, {}, timeout=60.0)
        ms = (time.perf_counter() - t0) * 1000
        if status == 200 and isinstance(body, dict) and body.get("replies"):
            ok(g, "POST /api/chat (非流式)", f"200 {ms:.0f}ms intent={body.get('intent')}")
        else:
            fail(g, "POST /api/chat (非流式)", f"HTTP {status} {ms:.0f}ms: {str(body)[:120]}", "查服务器日志（graph 未就绪→503，LLM 失败→500）")
    except Exception as exc:
        fail(g, "POST /api/chat (非流式)", f"{type(exc).__name__}: {exc}")
    # POST /api/chat SSE：读前 3 帧
    try:
        req = urllib.request.Request(
            base + "/api/chat",
            data=json.dumps({"message": "你好", "stream": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        frames = []
        with urllib.request.urlopen(req, timeout=60) as resp:
            while len(frames) < 3:
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    frames.append(line[5:].strip()[:60])
        if frames:
            ok(g, "POST /api/chat (SSE)", f"收到 {len(frames)} 帧: {frames[0]}")
        else:
            fail(g, "POST /api/chat (SSE)", "未收到任何 data: 帧", "检查 StreamingResponse / 反代是否缓冲了 SSE")
    except Exception as exc:
        fail(g, "POST /api/chat (SSE)", f"{type(exc).__name__}: {exc}")
    # POST /api/rating
    try:
        status, body = _http_post_json( base + "/api/rating", {"session_id": "diag-smoke", "message_index": 0, "stars": 5}, {}, timeout=5.0)
        if status == 200:
            ok(g, "POST /api/rating", "200")
        else:
            fail(g, "POST /api/rating", f"HTTP {status}: {str(body)[:100]}")
    except Exception as exc:
        fail(g, "POST /api/rating", f"{type(exc).__name__}: {exc}")

# 汇总  # ══════

def summarize() -> int:
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
    for status, *_ in RESULTS:
        counts[status] = counts.get(status, 0) + 1
    section("汇总")
    print(f"  {counts['PASS']} pass / {counts['FAIL']} fail / " f"{counts['WARN']} warn / {counts['SKIP']} skip")
    fails = [(g, n, d) for s, g, n, d in RESULTS if s == "FAIL"]
    if fails:
        # 按依赖链排序：环境→依赖→模块导入→数据层→外部服务→图冒烟→HTTP
        fails.sort(key=lambda x: _GROUP_ORDER.index(x[0]) if x[0] in _GROUP_ORDER else 99)
        print("\n  建议修复顺序（前 3 项）:")
        for i, (grp, name, detail) in enumerate(fails[:3], 1):
            print(f"    {i}. [{grp}] {name} — {detail[:100]}")
    else:
        print("  全部关键检查通过，可以启动服务器: python app_fastapi.py")
    return min(counts["FAIL"], 250)

def main() -> int:
    parser = argparse.ArgumentParser(description="LangGraph 客服项目一键诊断")
    parser.add_argument("--server", default="", help="已运行服务器地址，如 http://127.0.0.1:7860（启用 G 组在线检查）")
    args = parser.parse_args()
    print("LangGraph Customer Service Agent — 环境诊断")
    print(f"项目根目录: {ROOT}")
    check_env()
    deps = check_deps() or {}
    imports_ok = bool(check_imports(deps))
    check_data()
    llm_ok = bool(check_services(deps))
    check_graph_smoke(imports_ok, llm_ok)
    if args.server:
        check_http(args.server)
    else:
        section("G. HTTP 层")
        skip("HTTP层", "在线服务检查", "未提供 --server 参数", "服务器启动后运行: python scripts\\diagnose.py --server http://127.0.0.1:7860")
    return summarize()

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断退出")
        sys.exit(130)
    except Exception as exc:  # 最外层兜底：诊断脚本本身绝不崩溃
        print(f"[FAIL] 诊断脚本内部错误: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
