# -*- coding: utf-8 -*-
"""
agent/user_memory.py — 用户级长期向量记忆（user_id 硬隔离）

职责：把"跨会话应当长期记住的事实/偏好/历史问题"抽取成结构化条目，
向量化后持久化；下轮对话按语义相似度召回，作为"用户背景"注入 prompt。

设计要点（与 RAG 证据资料分区分开）：
  * 表 user_memories(id, user_id, tenant_id, content, kind, embedding,
    importance, created_at, expires_at, source_session, dedup_key)。
  * 写入（extract_and_store）：
      - 注入的 llm_fn 提炼结构化事实；LLM 不可用时规则降级。
      - **幂等键 = 源消息范围哈希**（user_id+source_session+消息内容）防重复写入。
      - 假设性陈述（"我可能" / "也许" / "if"...）不入库。
      - PII 写入前脱敏（手机号 / 邮箱 / 身份证 / 银行卡）。
  * 召回（recall）：
      - query 向量化 → 相似度 × importance × decay(e^-λt) 打分。
      - 过期条目软删除（expires_at < now → is_deleted=1，不再召回）。
      - **租户 / user_id 硬过滤在 SQL WHERE 层强制**，不依赖调用方传参正确。
  * 后端：RAG_BACKEND=pgvector 时复用 pgvector（embedding 列 + 余弦）；
    否则降级 SQLite（JSON 存向量）+ 内存余弦。embed 走注入的 embed_fn
    或 EmbeddingClient。

三方守卫：psycopg / EmbeddingClient / httpx 全部延迟导入，缺席自动降级；
本模块可 py_compile、可被纯 stdlib 单测导入（embed_fn / llm_fn 全 mock）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

# ── 类型别名 ─────────────────────────────────────────────────
EmbedFn = Callable[[str], Sequence[float]]
# llm_fn(dialogue_text) -> List[{"content","kind","importance"}]（或原始 JSON 串）
LLMFn = Callable[[str], Any]

DEFAULT_DB_PATH = Path(__file__).parent.parent / "user_memory.db"

VALID_KINDS = ("fact", "preference", "issue")

# Supersede: same-kind memories with cosine similarity > threshold are
# marked superseded (excluded from recall) when a newer memory is stored.
SUPERSEDE_SIMILARITY_THRESHOLD: float = 0.7

logger = logging.getLogger("agent.user_memory")

# 衰减常数：半衰期 ~30 天 → λ = ln2 / (30*86400)
DECAY_LAMBDA = math.log(2) / (30 * 86400)

# 假设/不确定表述 → 不入库
_HYPOTHETICAL_PATTERNS = [
    r"可能", r"也许", r"大概", r"或许", r"说不定", r"假如", r"如果.*的话",
    r"\bmaybe\b", r"\bperhaps\b", r"\bprobably\b", r"\bif\b", r"\bmight\b",
    r"\bwould\b", r"不确定", r"不一定", r"应该是吧",
]

# PII 脱敏规则（写入前执行）。用 (?<!\d)/(?!\d) 数字边界代替 \b——
# \b 在"中文字符↔数字"之间不成立（二者都是 word char），会漏脱敏中文语境
# 里的手机号/身份证。顺序：身份证/银行卡先于手机号，避免长数字被拆。
_PII_RULES = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[ID]"),        # 身份证 18 位
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[CARD]"),         # 银行卡 16-19 位
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[PHONE]"),      # 手机号 11 位
]


# ════════════════════════════════════════════════════════════════════
# 纯函数（无 DB / 无网络，全部可独测）
# ════════════════════════════════════════════════════════════════════

def redact_pii(text: str) -> str:
    """写入前 PII 脱敏（手机号 / 邮箱 / 身份证 / 银行卡）。"""
    if not text:
        return text
    out = text
    for pattern, repl in _PII_RULES:
        out = pattern.sub(repl, out)
    return out


def is_hypothetical(text: str) -> bool:
    """判断是否为假设/不确定陈述（这类不写入长期记忆）。"""
    if not text:
        return True
    low = text.lower()
    for pat in _HYPOTHETICAL_PATTERNS:
        if re.search(pat, low):
            return True
    return False


def dedup_key(user_id: str, source_session: str, contents: Sequence[str]) -> str:
    """Return a stable idempotency key for a source message scope.

    The key includes the user and source session, plus a normalized set of
    non-empty contents. Re-extracting the same source message therefore
    resolves to the same key and is ignored by the database unique constraint.
    """
    normalized = "\x1f".join(
        sorted(re.sub(r"\s+", " ", str(content).strip())
               for content in contents if str(content).strip())
    )
    raw = f"{user_id}\x1f{source_session}\x1f{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_from_message(user_id: str, user_message: str,
                         source_session: str = "",
                         tenant_id: str = "default") -> Dict[str, Any]:
    """从单条用户消息中快速抽取值得记住的信息并入库。

    纯规则（无 LLM），只对命中关键词的消息仓储。
    放在 generate_reply 节点末尾调用，实现"某句话值得记住时就记住"的效果。
    """
    content = (user_message or "").strip()
    if not content or is_hypothetical(content):
        return {"stored": [], "reason": "empty_or_hypothetical"}

    # 用 rule_extract 的规则判断类型（引用模块级的 _ISSUE_HINT / _PREF_HINT 模式）
    if _ISSUE_HINT.search(content):
        kind, importance = "issue", 0.8
    elif _PREF_HINT.search(content):
        kind, importance = "preference", 0.6
    else:
        return {"stored": [], "reason": "no_interesting_pattern"}

    store = get_memory_store()
    mem_id = store.add_memory(
        user_id, content, kind=kind, importance=importance,
        tenant_id=tenant_id, source_session=source_session)
    if mem_id:
        return {"stored": [mem_id], "kind": kind, "importance": importance}
    else:
        return {"stored": [], "reason": "deduped_or_existing"}


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（纯 stdlib）。零向量或维度不匹配返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def decay(age_seconds: float, lam: float = DECAY_LAMBDA) -> float:
    """时间衰减因子 e^(-λ·Δt)，Δt 为条目年龄秒数。"""
    if age_seconds <= 0:
        return 1.0
    return math.exp(-lam * age_seconds)


def score_memory(relevance: float, importance: float,
                 age_seconds: float, lam: float = DECAY_LAMBDA) -> float:
    """综合打分 = relevance × importance × decay(e^-λt)。"""
    return relevance * importance * decay(age_seconds, lam)


def _now_ts() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now_ts(),
                                  tz=timezone.utc).isoformat()


def _parse_ts(value: Any) -> float:
    """ISO 字符串 / 数字 → epoch 秒。解析失败返回 0。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


# ── 规则降级提炼（llm_fn 不可用时）───────────────────────────

_PREF_HINT = re.compile(r"(喜欢|偏好|习惯|prefer|like|讨厌|不喜欢|感兴趣|买了|购买|下单|买过|用过|想要|希望|想买|需要|打算|打算买|准备|准备买|想要一个)")
_ISSUE_HINT = re.compile(r"(坏了|故障|投诉|退货|问题|报错|无法|不能|error|broken|退款|失灵|异响|不满|生气|愤怒|差评|不满意|无法使用|死机|卡顿|连不上|断连|没声音|噪音|降价|贵了|不值)")


def rule_extract(dialogue_text: str) -> List[Dict[str, Any]]:
    """规则降级：从对话文本按行抽取候选记忆（LLM 不可用时）。"""
    out: List[Dict[str, Any]] = []
    for line in dialogue_text.splitlines():
        line = line.strip()
        if not line or len(line) < 4:
            continue
        # 只从"用户:"行抽取
        if line.startswith("用户:") or line.lower().startswith("user:"):
            content = line.split(":", 1)[1].strip()
        else:
            continue
        if not content or is_hypothetical(content):
            continue
        if _ISSUE_HINT.search(content):
            kind, importance = "issue", 0.8
        elif _PREF_HINT.search(content):
            kind, importance = "preference", 0.6
        else:
            kind, importance = "fact", 0.4
        out.append({"content": content, "kind": kind, "importance": importance})
    return out


def _normalize_items(raw: Any) -> List[Dict[str, Any]]:
    """把 llm_fn 的返回（list[dict] 或 JSON 串）归一化为标准条目列表。"""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:
            return []
    if isinstance(data, dict):
        data = data.get("memories") or data.get("items") or []
    if not isinstance(data, list):
        return []
    items: List[Dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content", "")).strip()
        if not content:
            continue
        kind = str(entry.get("kind", "fact")).strip().lower()
        if kind not in VALID_KINDS:
            kind = "fact"
        try:
            importance = float(entry.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        importance = max(0.0, min(1.0, importance))
        items.append({"content": content, "kind": kind,
                      "importance": importance})
    return items


# ════════════════════════════════════════════════════════════════════
# MemoryStore
# ════════════════════════════════════════════════════════════════════

class MemoryStore:
    """用户级长期记忆存储。

    Args:
        db_path:  SQLite 路径（pgvector 后端时忽略）。
        embed_fn: fn(text)->vector；None 时尝试 EmbeddingClient.from_env。
        backend:  "sqlite" | "pgvector"；None 时读 RAG_BACKEND（缺省 sqlite）。
        ttl_days: 记忆默认有效期（天）；<=0 表示永不过期。
    """

    def __init__(self, db_path: Optional[str] = None,
                 embed_fn: Optional[EmbedFn] = None,
                 backend: Optional[str] = None,
                 ttl_days: float = 180.0):
        self._embed_fn = embed_fn
        self.ttl_days = ttl_days
        # PostgreSQL + pgvector is the production default. An explicit local
        # SQLite path is supported only as an isolated test/diagnostic adapter.
        explicit_sqlite = (backend is None and db_path is not None and
                           (db_path == ":memory:" or
                            str(db_path).lower().endswith((".db", ".sqlite", ".sqlite3"))))
        requested = ("sqlite" if explicit_sqlite else
                     (backend or os.environ.get("RAG_BACKEND", "pgvector"))).strip().lower()
        if requested not in {"pgvector", "sqlite"}:
            raise RuntimeError(f"Unsupported user memory backend: {requested}")
        if requested == "sqlite":
            self.backend = "sqlite"
            self.db_path = str(db_path or DEFAULT_DB_PATH)
            self._pg = None
            self._init_sqlite()
        else:
            self.backend = "pgvector"
            self.db_path = "postgresql"
            self._pg = None  # lazy pgvector store

    def _embed(self, text: str) -> List[float]:
        fn = self._embed_fn
        if fn is None:
            fn = self._resolve_embed_fn()
        if fn is None:
            return []
        try:
            vec = fn(text)
            return [float(x) for x in vec] if vec is not None else []
        except Exception:
            return []

    def _resolve_embed_fn(self) -> Optional[EmbedFn]:
        """惰性解析 EmbeddingClient（三方守卫）；成功则缓存。"""
        try:
            from .embedding_client import EmbeddingClient
            client = EmbeddingClient.from_env(strict=False)
            if client is not None:
                self._embed_fn = client.embed_one
                return self._embed_fn
        except Exception:
            pass
        return None

    # ── SQLite 后端 ──────────────────────────────────────────

    def _connect(self):
        if self.backend != "sqlite":
            raise RuntimeError("SQLite connection requested for pgvector backend")
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sqlite(self) -> None:
        """Create the isolated test/diagnostic SQLite schema."""
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    embedding TEXT,
                    importance REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    source_session TEXT,
                    dedup_key TEXT UNIQUE,
                    embedding_error TEXT,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    superseded_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_um_sqlite_user ON user_memories(user_id, tenant_id)")
            # Backward-compatible migration: add superseded_at if missing
            try:
                conn.execute("ALTER TABLE user_memories ADD COLUMN superseded_at REAL")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.commit()
        finally:
            conn.close()

    def _pg_store(self):
        """惰性建立 pgvector store（三方守卫）。

        自动处理连接关闭/超时：每次返回前敲 1 秒探活，不通则重建。
        """
        from .pgvector_hybrid import PgHybridStore  # noqa: F401 (导入即守卫)
        import psycopg  # noqa: F401
        dsn = (os.environ.get("PG_DSN") or os.environ.get("RAG_PG_DSN")
               or os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_DSN")
               or "postgresql://postgres:postgres@localhost:5432/agent")
        try:
            connect_timeout = max(1, int(os.environ.get("PG_CONNECT_TIMEOUT", "5")))
        except (TypeError, ValueError):
            connect_timeout = 5

        if self._pg is not None:
            try:
                with self._pg.cursor() as cur:
                    cur.execute("SELECT 1")
                return self._pg
            except psycopg.Error:
                # 连接已关闭 / 超时 → 清理后重建
                try:
                    self._pg.close()
                except Exception:
                    pass
                self._pg = None

        # Never let a missing/stopped PostgreSQL instance hang a request or
        # a test process indefinitely.  The live application still fails fast
        # and surfaces the database health error to the caller.
        conn = psycopg.connect(dsn, autocommit=True,
                               connect_timeout=connect_timeout)
        self._ensure_pg_schema(conn)
        self._pg = conn
        return self._pg

    def _ensure_pg_schema(self, conn) -> None:
        dim = int(os.environ.get("PGVECTOR_DIM",
                                 os.environ.get("RAG_EMBED_DIM", "1024")))
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS user_memories (
                    id             TEXT PRIMARY KEY,
                    user_id        TEXT NOT NULL,
                    tenant_id      TEXT NOT NULL DEFAULT 'default',
                    content        TEXT NOT NULL,
                    kind           TEXT NOT NULL DEFAULT 'fact',
                    embedding      vector({dim}),
                    importance     REAL NOT NULL DEFAULT 0.5,
                    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                    expires_at     TIMESTAMPTZ,
                    source_session TEXT,
                    dedup_key      TEXT UNIQUE,
                    embedding_error TEXT,
                    is_deleted     BOOLEAN NOT NULL DEFAULT FALSE,
                    superseded_at  DOUBLE PRECISION
                );
                ALTER TABLE user_memories
                    ADD COLUMN IF NOT EXISTS embedding_error TEXT;
                ALTER TABLE user_memories
                    ADD COLUMN IF NOT EXISTS superseded_at DOUBLE PRECISION;
                CREATE INDEX IF NOT EXISTS idx_um_user
                    ON user_memories(user_id, tenant_id);
            """)

    def _expected_embedding_dim(self) -> int:
        """Return the pgvector dimension configured for this deployment."""
        raw = os.environ.get("PGVECTOR_DIM", os.environ.get("RAG_EMBED_DIM", "1024"))
        try:
            dim = int(raw)
        except (TypeError, ValueError):
            logger.warning("Invalid PGVECTOR_DIM/RAG_EMBED_DIM=%r; using 1024", raw)
            return 1024
        return dim if dim > 0 else 1024

    def _embedding_error(self, vec: Sequence[float]) -> Optional[str]:
        """Return an actionable error if a vector cannot fit pgvector."""
        if not vec:
            return None
        expected = self._expected_embedding_dim()
        actual = len(vec)
        if actual == expected:
            return None
        return (f"embedding dimension mismatch: expected {expected}, got {actual}; "
                "memory content was saved without a vector")

    # ── 写入路径 ──────────────────────────────────────────────

    def add_memory(self, user_id: str, content: str, kind: str = "fact",
                   importance: float = 0.5, tenant_id: str = "default",
                   source_session: str = "",
                   dedup: Optional[str] = None,
                   expires_at: Optional[str] = None) -> Optional[str]:
        """写入单条记忆（PII 脱敏 + 幂等去重）。返回 id；被去重则返回 None。"""
        if not user_id or not content:
            return None
        content = redact_pii(content.strip())
        if kind not in VALID_KINDS:
            kind = "fact"
        importance = max(0.0, min(1.0, float(importance)))
        key = dedup or dedup_key(user_id, source_session, [content])
        if expires_at is None and self.ttl_days > 0:
            expires_at = _iso(_now_ts() + self.ttl_days * 86400)
        vec = self._embed(content)
        embedding_error = self._embedding_error(vec)
        if embedding_error:
            logger.warning("%s (user=%s, session=%s)",
                           embedding_error, user_id, source_session or "-")
            # Keep the extracted memory content even when the vector service
            # and pgvector schema are temporarily out of sync.
            vec = []
        mem_id = uuid.uuid4().hex

        if self.backend == "pgvector":
            return self._pg_add(mem_id, user_id, tenant_id, content, kind, vec,
                                importance, expires_at, source_session, key,
                                embedding_error=embedding_error)

        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO user_memories
                   (id, user_id, tenant_id, content, kind, embedding,
                    importance, created_at, expires_at, source_session, dedup_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mem_id, user_id, tenant_id, content, kind,
                 json.dumps(vec), importance, _iso(), expires_at,
                 source_session, key))
            conn.commit()
            if cur.rowcount > 0:
                self._supersede_old(user_id, kind, vec, _now_ts(),
                                    exclude_id=mem_id, tenant_id=tenant_id)
                return mem_id
            return None
        finally:
            conn.close()

    def _pg_add(self, mem_id, user_id, tenant_id, content, kind, vec,
                importance, expires_at, source_session, key,
                embedding_error: Optional[str] = None) -> Optional[str]:
        conn = self._pg_store()
        emb_literal = "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]" \
            if vec else None
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_memories
                   (id, user_id, tenant_id, content, kind, embedding,
                    importance, expires_at, source_session, dedup_key,
                    embedding_error)
                   VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s)
                   ON CONFLICT (dedup_key) DO NOTHING
                   RETURNING id""",
                (mem_id, user_id, tenant_id, content, kind, emb_literal,
                 importance, expires_at, source_session, key, embedding_error))
            row = cur.fetchone()
        if row:
            self._supersede_old(user_id, kind, vec, _now_ts(),
                                exclude_id=row[0], tenant_id=tenant_id)
            return row[0]
        return None

    def extract_and_store(self, user_id: str, messages: Sequence[Any],
                          source_session: str = "",
                          tenant_id: str = "default",
                          llm_fn: Optional[LLMFn] = None) -> Dict[str, Any]:
        """会话结束时提炼结构化记忆并写入。

        Args:
            messages: [{"role","content"}] 或带 .content 的消息对象序列。
            llm_fn:   fn(dialogue_text)->条目列表/JSON；None 时规则降级。

        Returns: {"stored":[ids], "skipped_hypothetical":n, "deduped":n}
        """
        dialogue = _format_dialogue(messages)
        contents = [c for _, c in _iter_role_content(messages)]

        items: List[Dict[str, Any]] = []
        if llm_fn is not None:
            try:
                items = _normalize_items(llm_fn(dialogue))
            except Exception:
                items = []
        if not items:
            items = rule_extract(dialogue)

        stored: List[str] = []
        skipped = 0
        deduped = 0
        seen_keys = set()
        for it in items:
            content = it["content"]
            if is_hypothetical(content):
                skipped += 1
                continue
            # 幂等键绑定源消息范围（同批内 + 跨批持久去重）
            key = dedup_key(user_id, source_session, [content])
            if key in seen_keys:
                deduped += 1
                continue
            seen_keys.add(key)
            mem_id = self.add_memory(
                user_id, content, kind=it["kind"],
                importance=it["importance"], tenant_id=tenant_id,
                source_session=source_session, dedup=key)
            if mem_id:
                stored.append(mem_id)
            else:
                deduped += 1
        _ = contents  # 保留：源消息范围（幂等键已覆盖）
        return {"stored": stored, "skipped_hypothetical": skipped,
                "deduped": deduped}

    def _supersede_old(
        self,
        user_id: str,
        kind: str,
        new_embedding: List[float],
        now: float,
        exclude_id: Optional[str] = None,
        tenant_id: str = "default",
    ) -> int:
        """Mark same-kind, high-similarity older memories as superseded.

        Only processes fact / preference / issue kinds.
        Threshold: normalized cosine > SUPERSEDE_SIMILARITY_THRESHOLD.
        exclude_id: the just-inserted memory ID, excluded from comparison.
        Returns the number of memories marked superseded.
        """
        if kind not in ("fact", "preference", "issue"):
            return 0
        if not new_embedding:
            return 0
        superseded = 0
        if self.backend == "pgvector":
            superseded = self._pg_supersede(user_id, kind, new_embedding,
                                            now, exclude_id, tenant_id)
        else:
            conn = self._connect()
            try:
                if exclude_id is not None:
                    rows = conn.execute(
                        """SELECT id, embedding FROM user_memories
                           WHERE user_id = ? AND kind = ? AND tenant_id = ?
                             AND superseded_at IS NULL
                             AND id != ?
                             AND (expires_at IS NULL OR expires_at > ?)""",
                        (user_id, kind, tenant_id, exclude_id, _iso(now)),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, embedding FROM user_memories
                           WHERE user_id = ? AND kind = ? AND tenant_id = ?
                             AND superseded_at IS NULL
                             AND (expires_at IS NULL OR expires_at > ?)""",
                        (user_id, kind, tenant_id, _iso(now)),
                    ).fetchall()
                for row in rows:
                    emb_raw = row["embedding"]
                    if not emb_raw:
                        continue
                    try:
                        old_emb = json.loads(emb_raw)
                    except (ValueError, json.JSONDecodeError, TypeError):
                        continue
                    if not old_emb:
                        continue
                    sim = (cosine(new_embedding, old_emb) + 1.0) / 2.0  # normalize to 0..1
                    if sim > SUPERSEDE_SIMILARITY_THRESHOLD:
                        conn.execute(
                            "UPDATE user_memories SET superseded_at = ? WHERE id = ?",
                            (now, row["id"]),
                        )
                        superseded += 1
                conn.commit()
            finally:
                conn.close()
        if superseded > 0:
            logger.info(
                "superseded %d old %s memories for user %s (sim > %.2f)",
                superseded, kind, user_id, SUPERSEDE_SIMILARITY_THRESHOLD,
            )
        return superseded

    def _pg_supersede(
        self,
        user_id: str,
        kind: str,
        new_embedding: List[float],
        now: float,
        exclude_id: Optional[str] = None,
        tenant_id: str = "default",
    ) -> int:
        """PostgreSQL variant of supersede detection."""
        conn = self._pg_store()
        superseded = 0
        with conn.cursor() as cur:
            if exclude_id is not None:
                cur.execute(
                    """SELECT id, embedding FROM user_memories
                       WHERE user_id = %s AND kind = %s AND tenant_id = %s
                         AND superseded_at IS NULL
                         AND id != %s
                         AND (expires_at IS NULL OR expires_at > %s)""",
                    (user_id, kind, tenant_id, exclude_id, _iso(now)),
                )
            else:
                cur.execute(
                    """SELECT id, embedding FROM user_memories
                       WHERE user_id = %s AND kind = %s AND tenant_id = %s
                         AND superseded_at IS NULL
                         AND (expires_at IS NULL OR expires_at > %s)""",
                    (user_id, kind, tenant_id, _iso(now)),
                )
            rows = cur.fetchall()
            for row in rows:
                emb = row[1]
                if isinstance(emb, str):
                    try:
                        old_emb = [float(x) for x in emb.strip("[]").split(",") if x]
                    except Exception:
                        continue
                elif emb:
                    old_emb = [float(x) for x in emb]
                else:
                    continue
                if not old_emb:
                    continue
                sim = (cosine(new_embedding, old_emb) + 1.0) / 2.0
                if sim > SUPERSEDE_SIMILARITY_THRESHOLD:
                    cur.execute(
                        "UPDATE user_memories SET superseded_at = %s WHERE id = %s",
                        (now, row[0]),
                    )
                    superseded += 1
            conn.commit()
        return superseded

    # ── 召回路径 ──────────────────────────────────────────────

    def recall(self, user_id: str, query: str, top_k: int = 5,
               tenant_id: str = "default",
               min_score: float = 0.0) -> List[Dict[str, Any]]:
        """语义召回该 user 的长期记忆，按 relevance×importance×decay 排序。

        - 租户 / user_id 硬过滤在 SQL WHERE 层强制（不靠调用方传对）。
        - 过期条目软删除后不再召回。
        """
        self.purge_expired(user_id=user_id, tenant_id=tenant_id)
        if self.backend == "pgvector":
            rows = self._pg_fetch(user_id, tenant_id)
        else:
            rows = self._sqlite_fetch(user_id, tenant_id)

        qvec = self._embed(query) if query else []
        now = _now_ts()
        scored: List[Dict[str, Any]] = []
        for r in rows:
            emb = r.get("embedding") or []
            if qvec and emb:
                relevance = max(0.0, cosine(qvec, emb))
            else:
                # 无向量可比时退化为 importance 排序（relevance=1 占位）
                relevance = 1.0
            age = max(0.0, now - _parse_ts(r.get("created_at")))
            s = score_memory(relevance, float(r.get("importance", 0.5)), age)
            if s < min_score:
                continue
            scored.append({
                "id": r.get("id"),
                "user_id": r.get("user_id"),
                "content": r.get("content"),
                "kind": r.get("kind"),
                "importance": float(r.get("importance", 0.5)),
                "created_at": r.get("created_at"),
                "relevance": round(relevance, 6),
                "score": round(s, 6),
            })
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:top_k]

    def _sqlite_fetch(self, user_id: str, tenant_id: str,
                      include_deleted: bool = False) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            sql = """SELECT id, user_id, content, kind, embedding, importance,
                             created_at, superseded_at
                      FROM user_memories
                      WHERE user_id = ? AND tenant_id = ?"""
            params = [user_id, tenant_id]
            if not include_deleted:
                sql += " AND is_deleted = 0 AND superseded_at IS NULL"
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["embedding"] = json.loads(d["embedding"]) if d["embedding"] else []
            except Exception:
                d["embedding"] = []
            out.append(d)
        return out

    def _pg_fetch(self, user_id: str, tenant_id: str,
                  include_deleted: bool = False) -> List[Dict[str, Any]]:
        conn = self._pg_store()
        with conn.cursor() as cur:
            sql = """SELECT id, user_id, content, kind, importance,
                             created_at, embedding, superseded_at
                      FROM user_memories
                      WHERE user_id = %s AND tenant_id = %s"""
            params = [user_id, tenant_id]
            if not include_deleted:
                sql += " AND is_deleted = FALSE AND superseded_at IS NULL"
            cur.execute(sql, params)
            rows = cur.fetchall()
        out = []
        for r in rows:
            emb = r[6]
            if isinstance(emb, str):
                try:
                    emb = [float(x) for x in emb.strip("[]").split(",") if x]
                except Exception:
                    emb = []
            out.append({"id": r[0], "user_id": r[1], "content": r[2],
                        "kind": r[3], "importance": r[4],
                        "created_at": r[5], "embedding": emb or [],
                        "superseded_at": r[7]})
        return out

    # ── 维护 / 管理（供 /api/memory 用户编辑）─────────────────

    def list_memories(self, user_id: str, tenant_id: str = "default",
                      include_deleted: bool = False) -> List[Dict[str, Any]]:
        """列出该 user 的记忆（供用户查看/编辑）。硬过滤 user_id+tenant。"""
        if self.backend == "pgvector":
            rows = self._pg_fetch(user_id, tenant_id, include_deleted=include_deleted)
        else:
            rows = self._sqlite_fetch(user_id, tenant_id, include_deleted=include_deleted)
        return [{"id": r["id"], "content": r["content"], "kind": r["kind"],
                 "importance": float(r.get("importance", 0.5)),
                 "created_at": r.get("created_at")} for r in rows]


    def backfill_missing_embeddings(self, user_id: Optional[str] = None,
                                    tenant_id: str = "default",
                                    limit: int = 100) -> Dict[str, Any]:
        """Backfill NULL pgvector embeddings for existing memories.

        Lossless: only fills missing embedding vectors; content/kind/importance
        are not changed. When user_id is provided, SQL hard-filters to that user.
        """
        if self.backend != "pgvector":
            return {"backend": self.backend, "scanned": 0, "updated": 0,
                    "skipped": 0, "errors": [], "message": "pgvector backend disabled"}
        try:
            limit = max(1, min(int(limit or 100), 1000))
        except Exception:
            limit = 100

        conn = self._pg_store()
        where = ["is_deleted = FALSE", "embedding IS NULL",
                 "embedding_error IS NULL", "tenant_id = %s"]
        params: List[Any] = [tenant_id]
        if user_id:
            where.append("user_id = %s")
            params.append(user_id)
        sql = ("SELECT id, content FROM user_memories WHERE " +
               " AND ".join(where) +
               " ORDER BY created_at ASC LIMIT %s")
        params.append(limit)

        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        updated = 0
        skipped = 0
        errors: List[Dict[str, str]] = []
        for mem_id, content in rows:
            vec = self._embed(str(content or ""))
            if not vec:
                skipped += 1
                continue
            embedding_error = self._embedding_error(vec)
            if embedding_error:
                skipped += 1
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE user_memories SET embedding_error=%s "
                        "WHERE id=%s AND embedding IS NULL",
                        (embedding_error, mem_id))
                errors.append({"id": str(mem_id), "error": embedding_error})
                continue
            emb_literal = "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE user_memories SET embedding=%s::vector, "
                        "embedding_error=NULL WHERE id=%s AND embedding IS NULL",
                        (emb_literal, mem_id))
                    updated += int(cur.rowcount or 0)
            except Exception as exc:
                errors.append({"id": str(mem_id), "error": str(exc)[:160]})
        return {"backend": "pgvector", "scanned": len(rows),
                "updated": updated, "skipped": skipped, "errors": errors}

    def delete_memory(self, user_id: str, mem_id: str,
                      tenant_id: str = "default", hard: bool = False) -> bool:
        """删除单条记忆（user_id 硬校验：只能删自己的）。默认软删除。"""
        if self.backend == "pgvector":
            conn = self._pg_store()
            with conn.cursor() as cur:
                if hard:
                    cur.execute(
                        "DELETE FROM user_memories WHERE id=%s AND user_id=%s "
                        "AND tenant_id=%s RETURNING id",
                        (mem_id, user_id, tenant_id))
                else:
                    cur.execute(
                        "UPDATE user_memories SET is_deleted=TRUE WHERE id=%s "
                        "AND user_id=%s AND tenant_id=%s RETURNING id",
                        (mem_id, user_id, tenant_id))
                return cur.fetchone() is not None
        conn = self._connect()
        try:
            if hard:
                cur = conn.execute(
                    "DELETE FROM user_memories WHERE id=? AND user_id=? AND tenant_id=?",
                    (mem_id, user_id, tenant_id))
            else:
                cur = conn.execute(
                    "UPDATE user_memories SET is_deleted=1 WHERE id=? AND user_id=? AND tenant_id=?",
                    (mem_id, user_id, tenant_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def purge_expired(self, user_id: Optional[str] = None,
                      tenant_id: str = "default") -> int:
        """过期软删除（expires_at < now）。返回受影响条数。"""
        now = _iso()
        if self.backend == "pgvector":
            conn = self._pg_store()
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "UPDATE user_memories SET is_deleted=TRUE "
                        "WHERE is_deleted=FALSE AND expires_at IS NOT NULL "
                        "AND expires_at < %s AND user_id=%s AND tenant_id=%s",
                        (now, user_id, tenant_id))
                else:
                    cur.execute(
                        "UPDATE user_memories SET is_deleted=TRUE "
                        "WHERE is_deleted=FALSE AND expires_at IS NOT NULL "
                        "AND expires_at < %s", (now,))
                return cur.rowcount or 0
        conn = self._connect()
        try:
            if user_id:
                cur = conn.execute(
                    "UPDATE user_memories SET is_deleted=1 "
                    "WHERE is_deleted=0 AND expires_at IS NOT NULL "
                    "AND expires_at < ? AND user_id=? AND tenant_id=?",
                    (now, user_id, tenant_id))
            else:
                cur = conn.execute(
                    "UPDATE user_memories SET is_deleted=1 "
                    "WHERE is_deleted=0 AND expires_at IS NOT NULL AND expires_at < ?",
                    (now,))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()

    def build_user_background(self, user_id: str, query: str = "",
                              top_k: int = 5,
                              tenant_id: str = "default") -> str:
        """把 top-K 记忆拼成 prompt 注入用的"用户背景"分区文本。"""
        hits = self.recall(user_id, query or "", top_k=top_k, tenant_id=tenant_id)
        if not hits:
            return ""
        label = {"fact": "事实", "preference": "偏好", "issue": "历史问题"}
        lines = ["## 用户背景（长期记忆）"]
        for h in hits:
            lines.append(f"- [{label.get(h['kind'], h['kind'])}] {h['content']}")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# 对话格式化辅助
# ════════════════════════════════════════════════════════════════════

def _iter_role_content(messages: Sequence[Any]):
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role", "user")
            content = m.get("content", "")
        else:
            content = getattr(m, "content", "")
            mtype = getattr(m, "type", "")
            role = "assistant" if mtype in ("ai", "assistant") else "user"
        if content:
            yield role, str(content)


def _format_dialogue(messages: Sequence[Any]) -> str:
    lines = []
    for role, content in _iter_role_content(messages):
        who = "用户" if role == "user" else "客服"
        lines.append(f"{who}: {content}")
    return "\n".join(lines)


# ── 全局单例（app 层复用）────────────────────────────────────

_store_singleton: Optional[MemoryStore] = None


def get_memory_store(embed_fn: Optional[EmbedFn] = None) -> MemoryStore:
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = MemoryStore(embed_fn=embed_fn)
    return _store_singleton


if __name__ == "__main__":
    # 冒烟：纯 stdlib，mock embed。
    def _fake_embed(t: str):
        h = hashlib.sha256(t.encode()).digest()
        return [b / 255.0 for b in h[:8]]

    store = MemoryStore(db_path=":memory:", embed_fn=_fake_embed, ttl_days=30)
    store._init_sqlite()
    print(store.extract_and_store(
        "u1", [{"role": "user", "content": "我喜欢简洁的回复"}],
        source_session="s1"))
    print(store.recall("u1", "回复风格"))
