# -*- coding: utf-8 -*-
"""
Versioned Prompt Registry — SQLite 持久化的版本化 prompt 注册表（P4 重写版）。

四表模型：
- prompt_template  : prompt 名称 + kind(system|judge|tool_desc|other)
- prompt_version   : 版本内容 + 状态机(seed/released/candidate/pending_approval/
                     approved/rejected/retired) + 父版本 + 结构化 diff
- prompt_release   : env × tenant × 流量百分比 的灰度发布记录
- prompt_run       : 运行期使用记录（version 用量审计）

关键 API：
    reg = PromptRegistry()                      # 默认 data/p4_self_improve.db
    seed_default_prompts(reg)                   # 从 nodes.py 注入首版 system prompt
    pv = reg.get_active("system_prompt", tenant="acme", session_seed=sid)
    v2 = reg.create_version("system_prompt", new_text, status="candidate")
    reg.release("system_prompt", v2.version_no, percent=10)   # 10% 灰度
    reg.promote_full("system_prompt")                          # 全量
    reg.rollback("system_prompt")                              # 一步回上一版本

灰度分桶：sha256(f"{name}:{session_seed}") % 100 < percent → 灰度版本；
session_seed 为 None 时恒走全量基线（保守策略）。

兼容层：保留旧接口 register/get/load/render/render_and_validate 与
`_versions` 内存镜像，context_assembler.py 无需改动。

seed 数据：`seed_default_prompts()` 用 ast 从 agent/nodes.py 源码提取
_BASE_SYSTEM_PROMPT 常量文本注入首版（不 import nodes，避免三方依赖）；
judge prompt 与工具描述同样可通过 kind 字段注册。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from agent.runtime_db import connect, init_runtime_schema, database_url

VERSION_STATUSES = ("seed", "released", "candidate", "pending_approval",
                    "approved", "rejected", "retired")
PROMPT_KINDS = ("system", "judge", "tool_desc", "other")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_template (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL DEFAULT 'system',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_version (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id       INTEGER NOT NULL REFERENCES prompt_template(id),
    version_no        INTEGER NOT NULL,
    content           TEXT NOT NULL,
    variables_schema  TEXT NOT NULL DEFAULT '[]',
    parent_version_id INTEGER,
    status            TEXT NOT NULL DEFAULT 'candidate',
    change_reason     TEXT NOT NULL DEFAULT '',
    diff              TEXT,
    created_at        REAL NOT NULL,
    UNIQUE (template_id, version_no)
);
CREATE TABLE IF NOT EXISTS prompt_release (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES prompt_template(id),
    version_id  INTEGER NOT NULL REFERENCES prompt_version(id),
    env         TEXT NOT NULL DEFAULT 'prod',
    tenant      TEXT,
    percent     INTEGER NOT NULL DEFAULT 100,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_release_lookup
    ON prompt_release (template_id, env, active);
CREATE TABLE IF NOT EXISTS prompt_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    version_id  INTEGER NOT NULL,
    session_id  TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL
);
"""


def _default_db_path() -> str:
    env = os.getenv("P4_DB_PATH")
    if env:
        return env
    root = Path(__file__).resolve().parent.parent
    return str(root / "data" / "p4_self_improve.db")


@dataclass(frozen=True)
class PromptVersion:
    name: str
    version_no: int
    content: str
    variables_schema: tuple = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    change_reason: str = "startup load"
    version_id: int = 0
    template_id: int = 0
    kind: str = "system"
    status: str = "released"
    parent_version_id: Optional[int] = None
    diff: Optional[dict] = None


class _CompatRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _CursorCompat:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self._cursor.fetchone()
        return _CompatRow(row) if row is not None else None

    def fetchall(self):
        return [_CompatRow(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _SQLiteConnection:
    """SQLite fallback when PostgreSQL is unavailable.

    - With no ``db_path``: shared in-memory singleton (fast for simple tests).
    - With an explicit ``db_path``: private file-backed connection (full test isolation).
    """
    _shared: object | None = None

    @classmethod
    def _ensure(cls):
        if cls._shared is not None:
            return cls._shared
        import sqlite3
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        cls._shared = conn
        return conn

    @classmethod
    def close_shared(cls):
        if cls._shared is not None:
            cls._shared.close()
            cls._shared = None

    def __init__(self, db_path: str | None = None):
        import sqlite3
        if db_path and db_path != ":memory:":
            self._conn = sqlite3.connect(db_path, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._owns_conn = True
        else:
            self._conn = self._ensure()
            self._owns_conn = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            if self._owns_conn:
                self._conn.close()
        return False

    def execute(self, sql: str, params=()):
        return _CursorCompat(self._conn.execute(sql, tuple(params)))

    def executescript(self, _sql: str):
        self._conn.executescript(_SCHEMA)
        return self


class _PostgresConnection:
    def __init__(self):
        self._conn = connect()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False

    @staticmethod
    def _sql(sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params=()):
        return _CursorCompat(self._conn.execute(self._sql(sql), tuple(params)))

    def executescript(self, _sql: str):
        init_runtime_schema()
        return self


class PromptRegistry:
    """SQLite 版本化注册表（含灰度发布 + 一键回滚）。

    Auto-detects PostgreSQL availability. Falls back to shared in-memory SQLite
    when no DATABASE_URL / PG_DSN / etc. environment variable is set, enabling
    hermetic unit tests without a running PostgreSQL instance.
    """

    _VARIABLE = re.compile(r"{([A-Za-z_][A-Za-z0-9_]*)}")

    def __init__(self, db_path: Optional[str] = None):
        # An explicit non-PostgreSQL path is an intentional isolated test/one-off
        # adapter.  It must win over DATABASE_URL loaded by the application
        # (pytest may import app_fastapi first, which loads .env).  The live
        # application calls PromptRegistry() with no path and therefore remains
        # PostgreSQL-only whenever a DSN is configured.
        explicit_sqlite = bool(
            db_path and str(db_path).lower() not in {"postgresql", "postgres"}
        )
        self._use_sqlite = explicit_sqlite
        self.db_path = db_path if explicit_sqlite else "postgresql"
        if not self._use_sqlite:
            try:
                database_url()
            except RuntimeError:
                self._use_sqlite = True
                self.db_path = db_path or ":memory:"
        if not self._use_sqlite:
            init_runtime_schema()
        self._versions: Dict[str, List[PromptVersion]] = {}
        self._reload_mirror()

    def _conn(self):
        if self._use_sqlite:
            return _SQLiteConnection(db_path=self.db_path)
        return _PostgresConnection()

    def close(self) -> None:
        if self._use_sqlite:
            _SQLiteConnection.close_shared()

    def _row_to_version(self, row, name: str, kind: str) -> PromptVersion:
        diff = None
        if row["diff"]:
            try:
                diff = json.loads(row["diff"])
            except json.JSONDecodeError:
                diff = {"raw": row["diff"]}
        return PromptVersion(
            name=name, version_no=row["version_no"], content=row["content"],
            variables_schema=tuple(json.loads(row["variables_schema"])),
            created_at=datetime.fromtimestamp(row["created_at"], tz=timezone.utc),
            change_reason=row["change_reason"], version_id=row["id"],
            template_id=row["template_id"], kind=kind, status=row["status"],
            parent_version_id=row["parent_version_id"], diff=diff)

    def _template_row(self, conn, name: str):
        return conn.execute(
            "SELECT * FROM prompt_template WHERE name=?", (name,)).fetchone()

    def _reload_mirror(self) -> None:
        self._versions.clear()
        with self._conn() as conn:
            for trow in conn.execute("SELECT * FROM prompt_template").fetchall():
                rows = conn.execute(
                    "SELECT * FROM prompt_version WHERE template_id=?"
                    " ORDER BY version_no", (trow["id"],)).fetchall()
                self._versions[trow["name"]] = [
                    self._row_to_version(r, trow["name"], trow["kind"]) for r in rows]

    # ── 注册 / 版本创建 ─────────────────────────────────────────────
    def register(self, name: str, content: str, *, kind: str = "system",
                 variables_schema=(), change_reason: str = "startup load",
                 status: str = "released", release_full: bool = True) -> PromptVersion:
        """注册新版本并（默认）全量发布 —— 兼容旧 register 语义。"""
        pv = self.create_version(name, content, kind=kind,
                                 variables_schema=variables_schema,
                                 change_reason=change_reason, status=status)
        if release_full:
            self.release(name, pv.version_no, percent=100)
        return self.get_version(name, pv.version_no)

    def create_version(self, name: str, content: str, *, kind: str = "system",
                       variables_schema=(), parent_version_id: Optional[int] = None,
                       status: str = "candidate", change_reason: str = "",
                       diff: Optional[dict] = None) -> PromptVersion:
        """只建版本不发布（候选 prompt 走这里，状态默认 candidate）。"""
        if status not in VERSION_STATUSES:
            raise ValueError(f"invalid status: {status}")
        if kind not in PROMPT_KINDS:
            raise ValueError(f"invalid kind: {kind}")
        required = list(variables_schema) or sorted(set(self._VARIABLE.findall(content)))
        now = time.time()
        with self._conn() as conn:
            trow = self._template_row(conn, name)
            if trow is None:
                conn.execute(
                    "INSERT INTO prompt_template (name, kind, created_at) VALUES (?,?,?)",
                    (name, kind, now))
                trow = self._template_row(conn, name)
            template_id = trow["id"]
            last = conn.execute(
                "SELECT MAX(version_no) FROM prompt_version WHERE template_id=?",
                (template_id,)).fetchone()[0] or 0
            if parent_version_id is None and last:
                parent_version_id = conn.execute(
                    "SELECT id FROM prompt_version WHERE template_id=? AND version_no=?",
                    (template_id, last)).fetchone()[0]
            conn.execute(
                "INSERT INTO prompt_version (template_id, version_no, content,"
                " variables_schema, parent_version_id, status, change_reason,"
                " diff, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (template_id, last + 1, content, json.dumps(required),
                 parent_version_id, status, change_reason,
                 json.dumps(diff, ensure_ascii=False) if diff is not None else None,
                 now))
        self._reload_mirror()
        return self.get_version(name, last + 1)

    # ── 查询 / 状态 ─────────────────────────────────────────────────
    def get_version(self, name: str, version_no: Optional[int] = None) -> PromptVersion:
        with self._conn() as conn:
            trow = self._template_row(conn, name)
            if trow is None:
                raise KeyError(f"Unknown prompt: {name}")
            if version_no is None:
                row = conn.execute(
                    "SELECT * FROM prompt_version WHERE template_id=?"
                    " ORDER BY version_no DESC LIMIT 1", (trow["id"],)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM prompt_version WHERE template_id=? AND version_no=?",
                    (trow["id"], version_no)).fetchone()
            if row is None:
                raise KeyError(f"Unknown version: {name} v{version_no}")
            return self._row_to_version(row, name, trow["kind"])

    def get_version_by_id(self, version_id: int) -> PromptVersion:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM prompt_version WHERE id=?", (version_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown version_id: {version_id}")
            trow = conn.execute("SELECT * FROM prompt_template WHERE id=?",
                                (row["template_id"],)).fetchone()
            return self._row_to_version(row, trow["name"], trow["kind"])

    def list_versions(self, name: str) -> List[PromptVersion]:
        self._reload_mirror()
        return list(self._versions.get(name, []))

    def set_status(self, version_id: int, status: str) -> None:
        if status not in VERSION_STATUSES:
            raise ValueError(f"invalid status: {status}")
        with self._conn() as conn:
            conn.execute("UPDATE prompt_version SET status=? WHERE id=?",
                         (status, version_id))
        self._reload_mirror()

    # ── 灰度发布 / 全量 / 回滚 ──────────────────────────────────────
    def release(self, name: str, version_no: int, percent: int = 100, *,
                env: str = "prod", tenant: Optional[str] = None) -> Dict[str, Any]:
        """发布某版本到 env×tenant，percent<100 为灰度（叠加在当前全量基线上）。"""
        percent = max(0, min(100, int(percent)))
        pv = self.get_version(name, version_no)
        now = time.time()
        with self._conn() as conn:
            if percent >= 100:
                # 全量:停用同 scope 的所有 active 发布
                conn.execute(
                    "UPDATE prompt_release SET active=0 WHERE template_id=?"
                    " AND env=? AND tenant IS NOT DISTINCT FROM ?",
                    (pv.template_id, env, tenant))
            else:
                # 灰度:仅替换同 scope 的其他灰度，保留全量基线
                conn.execute(
                    "UPDATE prompt_release SET active=0 WHERE template_id=?"
                    " AND env=? AND tenant IS NOT DISTINCT FROM ? AND percent < 100",
                    (pv.template_id, env, tenant))
            conn.execute(
                "INSERT INTO prompt_release (template_id, version_id, env, tenant,"
                " percent, active, created_at) VALUES (?,?,?,?,?,1,?)",
                (pv.template_id, pv.version_id, env, tenant, percent, now))
        self.set_status(pv.version_id, "released")
        return {"name": name, "version_no": version_no, "percent": percent,
                "env": env, "tenant": tenant}

    def promote_full(self, name: str, version_no: Optional[int] = None, *,
                     env: str = "prod", tenant: Optional[str] = None) -> Dict[str, Any]:
        """把当前灰度版本（或指定版本）提为 100% 全量。"""
        if version_no is None:
            canary = self._active_releases(name, env, tenant)["canary"]
            if canary is None:
                raise ValueError(f"no canary release to promote for {name}")
            version_no = self.get_version_by_id(canary["version_id"]).version_no
        return self.release(name, version_no, 100, env=env, tenant=tenant)

    def rollback(self, name: str, *, env: str = "prod",
                 tenant: Optional[str] = None) -> Dict[str, Any]:
        """一步回滚：停用当前 active 发布，恢复上一个不同版本的全量发布。"""
        with self._conn() as conn:
            trow = self._template_row(conn, name)
            if trow is None:
                raise KeyError(f"Unknown prompt: {name}")
            history = conn.execute(
                "SELECT * FROM prompt_release WHERE template_id=? AND env=?"
                " AND tenant IS NOT DISTINCT FROM ? ORDER BY id DESC",
                (trow["id"], env, tenant)).fetchall()
        current = next((r for r in history if r["active"]), None)
        if current is None:
            raise ValueError(f"no active release to roll back for {name}")
        prev = next((r for r in history
                     if r["version_id"] != current["version_id"]
                     and r["percent"] >= 100), None)
        if prev is None:
            raise ValueError(f"no previous version to roll back to for {name}")
        prev_pv = self.get_version_by_id(prev["version_id"])
        result = self.release(name, prev_pv.version_no, 100, env=env, tenant=tenant)
        self.set_status(current["version_id"], "retired")
        result["rolled_back_from"] = self.get_version_by_id(
            current["version_id"]).version_no
        return result

    def _active_releases(self, name: str, env: str,
                         tenant: Optional[str]) -> Dict[str, Any]:
        """返回 {'full': row|None, 'canary': row|None}；tenant 精确 > 全局(NULL)。"""
        with self._conn() as conn:
            trow = self._template_row(conn, name)
            if trow is None:
                raise KeyError(f"Unknown prompt: {name}")
            rows = conn.execute(
                "SELECT * FROM prompt_release WHERE template_id=? AND env=?"
                " AND active=1 AND (tenant IS NULL OR tenant = ?)"
                " ORDER BY id DESC", (trow["id"], env, tenant)).fetchall()
        exact = [r for r in rows if r["tenant"] == tenant] if tenant is not None else []
        pool = exact or [r for r in rows if r["tenant"] is None]
        full = next((r for r in pool if r["percent"] >= 100), None)
        canary = next((r for r in pool if r["percent"] < 100), None)
        return {"full": full, "canary": canary}

    @staticmethod
    def _bucket(name: str, session_seed: str) -> int:
        digest = hashlib.sha256(f"{name}:{session_seed}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % 100

    def get_active(self, name: str, tenant: Optional[str] = None,
                   session_seed: Optional[str] = None, *, env: str = "prod",
                   log_run: bool = False) -> PromptVersion:
        """取当前生效版本：按 session_seed 哈希落灰度桶；无 seed 走全量基线。"""
        rel = self._active_releases(name, env, tenant)
        chosen = rel["full"]
        if rel["canary"] is not None and session_seed is not None and \
                self._bucket(name, str(session_seed)) < rel["canary"]["percent"]:
            chosen = rel["canary"]
        if chosen is None:
            # 无任何可用发布:兜底最新版本（兼容旧 get 语义）
            return self.get_version(name)
        pv = self.get_version_by_id(chosen["version_id"])
        if log_run:
            self.record_run(pv, session_id=str(session_seed or ""))
        return pv

    def record_run(self, pv: PromptVersion, session_id: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO prompt_run (template_id, version_id, session_id, ts)"
                " VALUES (?,?,?,?)",
                (pv.template_id, pv.version_id, session_id, time.time()))

    # ── 旧接口兼容层 ────────────────────────────────────────────────
    def get(self, name: str, version_no: Optional[int] = None) -> PromptVersion:
        return self.get_version(name, version_no)

    def load(self, name: str, *, file_path: Optional[str] = None,
             env_var: Optional[str] = None, default: Optional[str] = None,
             variables_schema=(), change_reason: str = "startup load") -> PromptVersion:
        content = os.getenv(env_var) if env_var else None
        if content is None and file_path:
            content = Path(file_path).read_text(encoding="utf-8")
        if content is None:
            content = default
        if content is None:
            raise ValueError(f"No prompt content configured for {name}")
        return self.register(name, content, variables_schema=variables_schema,
                             change_reason=change_reason)

    def render(self, name: str, variables: Mapping[str, object], *,
               version_no: Optional[int] = None) -> Tuple[str, PromptVersion]:
        prompt = self.get_version(name, version_no)
        missing = [k for k in prompt.variables_schema if k not in variables]
        if missing:
            raise ValueError(
                f"Missing required prompt variables for {name}: {', '.join(missing)}")
        return prompt.content.format(**variables), prompt

    def render_and_validate(self, system_prompt_template: str,
                            user_context: Mapping[str, object]) -> str:
        required = sorted(set(self._VARIABLE.findall(system_prompt_template)))
        missing = [k for k in required if k not in user_context]
        if missing:
            raise ValueError("Missing required prompt variables: " + ", ".join(missing))
        try:
            return system_prompt_template.format(**user_context)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid prompt rendering: {exc}") from exc


# ── Seed 数据:从 nodes.py 源码提取 SYSTEM_PROMPT 常量 ──────────────

DEFAULT_JUDGE_PROMPT = """你是客服回答质量评审员。给你一个用户问题和两个候选回答 A/B。
评审标准：准确性（是否符合参考事实）、针对性（是否回答了问题）、语气专业友好、
拒答正确性（对越权/离题请求应礼貌拒绝并引导回产品话题）。
只输出一个字母：A（A 更好）、B（B 更好）或 T（平手）。不要输出其他内容。"""

FALLBACK_SYSTEM_PROMPT = """你是一个专业的智能客服助手，服务于"智联科技"公司。
公司产品：智能音箱、智能家居套装、云服务。
用中文友好回答产品咨询；投诉先道歉再解决；不确定时诚实说明，不要编造信息。"""


def extract_system_prompt_from_nodes(nodes_path: Optional[str] = None) -> Optional[str]:
    """用 ast 从 nodes.py 源码提取 _BASE_SYSTEM_PROMPT 字符串常量（不 import nodes，
    避免拉起 langgraph 等三方依赖）。查找顺序：显式参数 > $P4_NODES_PATH >
    本模块同目录 nodes.py。"""
    candidates = []
    if nodes_path:
        candidates.append(Path(nodes_path))
    env = os.getenv("P4_NODES_PATH")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parent / "nodes.py")
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                        "_BASE_SYSTEM_PROMPT", "SYSTEM_PROMPT"):
                    if isinstance(node.value, ast.Constant) and \
                            isinstance(node.value.value, str):
                        return node.value.value
    return None


def seed_default_prompts(registry: PromptRegistry,
                         nodes_path: Optional[str] = None) -> Dict[str, int]:
    """幂等注入 seed 数据：system_prompt(来自 nodes.py) + judge_prompt。"""
    seeded: Dict[str, int] = {}
    if not registry._versions.get("system_prompt"):
        content = extract_system_prompt_from_nodes(nodes_path) or FALLBACK_SYSTEM_PROMPT
        pv = registry.register(
            "system_prompt", content, kind="system",
            change_reason="seed from agent/nodes.py _BASE_SYSTEM_PROMPT")
        seeded["system_prompt"] = pv.version_no
    if not registry._versions.get("judge_prompt"):
        pv = registry.register("judge_prompt", DEFAULT_JUDGE_PROMPT, kind="judge",
                               change_reason="seed default pairwise judge prompt")
        seeded["judge_prompt"] = pv.version_no
    return seeded
