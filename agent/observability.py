<<<<<<< HEAD
"""
agent/observability.py — Trace 持久化 + 滑动窗口告警 (P3 可观测性栈)

**升级 (可回放 Trace)**: TraceSession 从"只记 input_text + events"升级为
**完整可回放**的结构化追踪。八个结构化分区, 每区一个 dataclass:

  - prompt    : 模板名 / 版本 / 变量摘要 / 最终渲染消息结构
  - retrieval : Query / 召回片段 / 分数 / 来源 / 权限过滤 / Rerank 排名
  - memory    : 命中记忆 / 来源 / 更新时间 / 置信度
  - tools     : 工具名 / 参数 / 权限结果 / 耗时 / 返回摘要 / 错误  (列表, 可多次)
  - model     : 供应商 / 模型 / 采样参数 / 输入输出 Token / finish / TTFT (列表, 可多次)
  - latency   : 入口 / 检索 / 模型 TTFT / 工具 / 总耗时
  - cost      : 输入成本 / 输出成本 / 缓存命中 / 租户+场景归因
  - result    : 最终答案 / 结构化解析 / 用户反馈 / 评测分数

便捷记录方法: record_prompt / record_retrieval / record_memory / record_tool /
record_model / record_latency / record_cost / record_result。旧的
`add_event(...)` 与 `events` 字段完整保留 (runner/app 在用)。

**PII**: 所有可能含 PII 的字段 (input_text / 渲染 prompt / 记忆 / 工具参数与返回 /
最终答案 / 检索片段) 在**落盘前**统一过 `redact_pii` 脱敏钩子 (深度递归)。

**落盘**: traces 表用 trace_json 存完整可回放结构; 关键字段做独立列并建索引
(request_id / session_id / user_id / tenant / scene / total_ms / cost / created_at)。
大字段 (完整 prompt / 答案) 只存 trace_json, 业务列存摘要/hash。PRAGMA 迁移兼容旧库。

组成:
1. Trace* dataclasses + TraceEvent + TraceSession — 一次请求的结构化追踪。
2. TraceService — 异步持久化到 SQLite (aiosqlite 守卫; 降级 sqlite3 WAL)。
   **app 层必须在请求结束时调用 `await trace_service.finalize_and_save(trace)`**。
3. AlertService — 真滑动窗口告警。

指标收集不在本模块 — 统一见 agent/metrics.py。回放能力见 agent/trace_replay.py。
trace = 单请求全量可回放; metrics = 聚合; log = 事件流。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("agent.observability")

# ── 可选依赖: aiosqlite ───────────────────────────────────────
from psycopg.types.json import Jsonb
from agent.runtime_db import connect, init_runtime_schema

try:
    from agent.security.pii_redactor import redact as _pii_redact
except Exception:  # pragma: no cover
    try:
        from security.pii_redactor import redact as _pii_redact  # type: ignore
    except Exception:
        _pii_redact = None


def redact_pii(text: str) -> str:
    """PII 脱敏钩子: 脱敏失败/模块缺失时原样返回, 绝不阻断主流程。"""
    if not text or not isinstance(text, str) or _pii_redact is None:
        return text
    try:
        result = _pii_redact(text)
        return getattr(result, "redacted_text", None) or text
    except Exception:
        logger.warning("pii redaction failed; storing original text", exc_info=True)
        return text


def _deep_redact(obj: Any) -> Any:
    """递归对结构中的所有字符串过 PII 脱敏钩子 (dict/list/tuple/str)。

    非字符串标量原样返回; PII 正则只命中手机号/身份证/邮箱/银行卡, 对
    request_id / 模型名 / 时间戳 等无副作用。
    """
    if isinstance(obj, str):
        return redact_pii(obj)
    if isinstance(obj, dict):
        return {k: _deep_redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_deep_redact(v) for v in obj]
    return obj


def _summarize(value: Any, limit: int = 200) -> str:
    """把任意值压成短摘要字符串 (用于变量摘要 / 返回摘要)。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "…(+%d)" % (len(value) - limit)


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# ── 结构化分区 dataclass ──────────────────────────────────────
@dataclass
class PromptRecord:
    """Prompt 分区: 模板名 / 版本 / 变量摘要 / 渲染后消息结构。"""
    template_name: str = ""
    version: str = ""
    variables_summary: Dict[str, str] = field(default_factory=dict)
    rendered_messages: List[Dict[str, str]] = field(default_factory=list)
    rendered_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetrievalChunk:
    """单个召回片段。"""
    text_summary: str = ""
    score: Optional[float] = None
    source: str = ""
    acl_allowed: Optional[bool] = None
    rerank_rank: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetrievalRecord:
    """检索分区: Query / 召回片段 / 分数 / 来源 / 权限过滤 / Rerank。"""
    query: str = ""
    chunks: List[RetrievalChunk] = field(default_factory=list)
    num_recalled: int = 0
    num_acl_filtered: int = 0
    rerank_applied: bool = False

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "chunks": [c.to_dict() for c in self.chunks],
            "num_recalled": self.num_recalled,
            "num_acl_filtered": self.num_acl_filtered,
            "rerank_applied": self.rerank_applied,
        }


@dataclass
class MemoryHit:
    """命中的一条记忆。"""
    content: str = ""
    source: str = ""
    updated_at: str = ""
    confidence: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolRecord:
    """工具调用: 名称 / 参数 / 权限结果 / 耗时 / 返回摘要 / 错误。"""
    name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    acl_result: Optional[str] = None
    duration_ms: float = 0.0
    result_summary: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ModelRecord:
    """模型调用: 供应商 / 模型 / 采样参数 / 输入输出 Token / finish / TTFT。"""
    provider: str = ""
    model: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""
    ttft_ms: Optional[float] = None
    stage: str = ""  # intent / generate / ...

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LatencyRecord:
    """延迟分区: 入口 / 检索 / 模型 TTFT / 工具 / 总耗时。"""
    entry_ms: Optional[float] = None
    retrieval_ms: Optional[float] = None
    model_ttft_ms: Optional[float] = None
    tool_ms: Optional[float] = None
    total_ms: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CostRecord:
    """成本分区: 输入成本 / 输出成本 / 缓存命中 / 租户+场景归因。"""
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_hit: bool = False
    tenant: str = ""
    scene: str = ""
    prompt_version: str = ""

    @property
    def total_cost(self) -> float:
        return round(float(self.input_cost) + float(self.output_cost), 8)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_cost"] = self.total_cost
        return d


@dataclass
class ResultRecord:
    """结果分区: 最终答案 / 结构化解析 / 用户反馈 / 评测分数。"""
    answer: str = ""
    parsed: Dict[str, Any] = field(default_factory=dict)
    feedback: Optional[str] = None
    eval_score: Optional[float] = None
    answer_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Trace 结构 ────────────────────────────────────────────────
class TraceEvent:
    """单个追踪事件 (向后兼容的事件流)。"""

    __slots__ = ("event_type", "data", "duration_ms", "timestamp")

    def __init__(self, event_type: str, data: dict, duration_ms: float = 0):
        self.event_type = event_type
        self.data = data
        self.duration_ms = duration_ms
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "type": self.event_type,
            "data": self.data,
            "duration_ms": round(self.duration_ms, 2),
            "timestamp": self.timestamp,
        }


class TraceSession:
    """一次请求的完整可回放追踪。

    request_id 用于跨日志/指标/trace 的关联传播。除向后兼容的 events 事件流外,
    新增八个结构化分区 (prompt/retrieval/memory/tools/model/latency/cost/result),
    通过 record_* 便捷方法填充。所有可能含 PII 的字段在落盘/序列化(redact=True)时
    统一过脱敏钩子。
    """

    def __init__(self, request_id: Optional[str] = None, user_id: str = "",
                 input_text: str = "", session_id: str = "",
                 tenant: str = "", scene: str = ""):
        self.request_id = request_id or uuid.uuid4().hex
        self.user_id = user_id
        self.session_id = session_id
        self.tenant = tenant
        self.scene = scene
        self.input_text = (input_text or "")[:500]
        self.events: List[TraceEvent] = []
        self.start_time = time.time()
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.total_latency_ms: float = 0.0
        self._finalized = False

        # 八个结构化分区
        self.prompt: Optional[PromptRecord] = None
        self.retrieval: Optional[RetrievalRecord] = None
        self.memory: List[MemoryHit] = []
        self.tools: List[ToolRecord] = []
        self.model: List[ModelRecord] = []
        self.latency: LatencyRecord = LatencyRecord()
        self.cost: Optional[CostRecord] = None
        self.result: Optional[ResultRecord] = None

    # ── 向后兼容的事件流 ──
    def add_event(self, event_type: str, data: dict, duration_ms: float = 0) -> None:
        self.events.append(TraceEvent(event_type, data, duration_ms))

    # ── record_* 便捷方法 ──
    def record_prompt(self, template_name: str = "", version: str = "",
                      variables: Optional[Dict[str, Any]] = None,
                      rendered_messages: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        """记录 Prompt: 模板名 / 版本 / 变量摘要 / 最终渲染消息结构。"""
        variables = variables or {}
        msgs = [
            {"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
            for m in (rendered_messages or [])
            if isinstance(m, dict)
        ]
        rendered_flat = "\n".join(m["content"] for m in msgs)
        self.prompt = PromptRecord(
            template_name=template_name,
            version=version,
            variables_summary={str(k): _summarize(v, 120) for k, v in variables.items()},
            rendered_messages=msgs,
            rendered_hash=_hash(rendered_flat),
        )

    def record_retrieval(self, query: str = "",
                         chunks: Optional[Sequence[Any]] = None,
                         scores: Optional[Sequence[float]] = None,
                         sources: Optional[Sequence[str]] = None,
                         acl: Optional[Sequence[bool]] = None,
                         rerank: Optional[Sequence[int]] = None) -> None:
        """记录检索: Query / 召回片段 / 分数 / 来源 / 权限过滤 / Rerank 排名。

        chunks/scores/sources/acl/rerank 为按下标并行的序列。
        """
        chunks = list(chunks or [])
        scores = list(scores or [])
        sources = list(sources or [])
        acl = list(acl or [])
        rerank = list(rerank or [])

        def _at(seq: List[Any], i: int) -> Any:
            return seq[i] if i < len(seq) else None

        recs: List[RetrievalChunk] = []
        acl_filtered = 0
        for i, ch in enumerate(chunks):
            allowed = _at(acl, i)
            if allowed is False:
                acl_filtered += 1
            recs.append(RetrievalChunk(
                text_summary=_summarize(ch if isinstance(ch, str) else
                                        (ch.get("text") if isinstance(ch, dict) else ch), 300),
                score=(_at(scores, i) if _at(scores, i) is None
                       else float(_at(scores, i))),
                source=str(_at(sources, i) or (ch.get("source") if isinstance(ch, dict) else "") or ""),
                acl_allowed=(None if allowed is None else bool(allowed)),
                rerank_rank=(None if _at(rerank, i) is None else int(_at(rerank, i))),
            ))
        self.retrieval = RetrievalRecord(
            query=query,
            chunks=recs,
            num_recalled=len(recs),
            num_acl_filtered=acl_filtered,
            rerank_applied=bool(rerank),
        )

    def record_memory(self, hits: Optional[Sequence[Any]] = None) -> None:
        """记录命中的记忆: 内容 / 来源 / 更新时间 / 置信度。"""
        out: List[MemoryHit] = []
        for h in (hits or []):
            if isinstance(h, dict):
                out.append(MemoryHit(
                    content=_summarize(h.get("content") or h.get("value") or h.get("text"), 300),
                    source=str(h.get("source", "")),
                    updated_at=str(h.get("updated_at", "") or h.get("updatedAt", "")),
                    confidence=(None if h.get("confidence") is None else float(h.get("confidence"))),
                ))
            else:
                out.append(MemoryHit(content=_summarize(h, 300)))
        self.memory = out

    def record_tool(self, name: str, args: Optional[Dict[str, Any]] = None,
                    acl: Optional[str] = None, ms: float = 0.0,
                    result: Any = None, error: Optional[str] = None) -> None:
        """记录一次工具调用 (可多次, 追加到 tools 列表)。"""
        self.tools.append(ToolRecord(
            name=name,
            args=dict(args or {}),
            acl_result=(None if acl is None else str(acl)),
            duration_ms=round(float(ms), 2),
            result_summary=_summarize(result, 300),
            error=(None if error is None else str(error)),
        ))

    def record_model(self, provider: str = "", model: str = "",
                     params: Optional[Dict[str, Any]] = None,
                     in_tok: int = 0, out_tok: int = 0,
                     finish: str = "", ttft_ms: Optional[float] = None,
                     stage: str = "") -> None:
        """记录一次模型调用 (可多次: 意图/生成…, 追加到 model 列表)。"""
        self.model.append(ModelRecord(
            provider=provider, model=model,
            params=dict(params or {}),
            input_tokens=int(in_tok or 0), output_tokens=int(out_tok or 0),
            finish_reason=finish,
            ttft_ms=(None if ttft_ms is None else round(float(ttft_ms), 2)),
            stage=stage,
        ))

    def record_latency(self, entry_ms: Optional[float] = None,
                       retrieval_ms: Optional[float] = None,
                       model_ttft_ms: Optional[float] = None,
                       tool_ms: Optional[float] = None,
                       total_ms: Optional[float] = None) -> None:
        """记录延迟分区 (只覆盖显式传入的字段)。"""
        lat = self.latency
        if entry_ms is not None:
            lat.entry_ms = round(float(entry_ms), 2)
        if retrieval_ms is not None:
            lat.retrieval_ms = round(float(retrieval_ms), 2)
        if model_ttft_ms is not None:
            lat.model_ttft_ms = round(float(model_ttft_ms), 2)
        if tool_ms is not None:
            lat.tool_ms = round(float(tool_ms), 2)
        if total_ms is not None:
            lat.total_ms = round(float(total_ms), 2)

    def record_cost(self, input_cost: float = 0.0, output_cost: float = 0.0,
                    cache_hit: bool = False, tenant: str = "",
                    scene: str = "", prompt_version: str = "") -> None:
        """记录成本分区 (按租户和场景归因)。"""
        self.cost = CostRecord(
            input_cost=float(input_cost or 0.0),
            output_cost=float(output_cost or 0.0),
            cache_hit=bool(cache_hit),
            tenant=tenant or self.tenant,
            scene=scene or self.scene,
            prompt_version=prompt_version,
        )
        if tenant:
            self.tenant = tenant
        if scene:
            self.scene = scene

    def record_result(self, answer: str = "", parsed: Optional[Dict[str, Any]] = None,
                      feedback: Optional[str] = None,
                      eval_score: Optional[float] = None) -> None:
        """记录最终结果: 答案 / 结构化解析 / 用户反馈 / 评测分数。"""
        answer = answer or ""
        self.result = ResultRecord(
            answer=answer,
            parsed=dict(parsed or {}),
            feedback=(None if feedback is None else str(feedback)),
            eval_score=(None if eval_score is None else float(eval_score)),
            answer_hash=_hash(answer),
        )

    # ── 派生属性 ──
    @property
    def total_ms(self) -> float:
        if self.latency.total_ms is not None:
            return round(self.latency.total_ms, 2)
        return round(self.total_latency_ms, 2)

    @property
    def total_cost(self) -> float:
        return self.cost.total_cost if self.cost else 0.0

    def is_failed(self) -> bool:
        """请求是否失败: 任一工具报错 / 模型 finish=error / 有答案分区但答案为空。"""
        if any(t.error for t in self.tools):
            return True
        if any((m.finish_reason or "").lower() in ("error", "content_filter")
               for m in self.model):
            return True
        if self.result is not None and not (self.result.answer or "").strip():
            return True
        return False

    def is_low_score(self, threshold: float = 0.5) -> bool:
        """是否低分/差评: eval_score < threshold 或负向反馈。"""
        if self.result is None:
            return False
        if self.result.eval_score is not None and self.result.eval_score < threshold:
            return True
        fb = (self.result.feedback or "").lower()
        return fb in ("thumbs_down", "negative", "bad", "down", "0")

    def finalize(self) -> None:
        """完成追踪, 计算总耗时 (幂等)。"""
        if not self._finalized:
            self.total_latency_ms = (time.time() - self.start_time) * 1000
            if self.latency.total_ms is None:
                self.latency.total_ms = round(self.total_latency_ms, 2)
            self._finalized = True

    def to_dict(self, redact: bool = False) -> dict:
        """完整可序列化结构。redact=True 时对全部字符串深度脱敏 (落盘用)。"""
        data = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant": self.tenant,
            "scene": self.scene,
            "input_text": self.input_text,
            "total_latency_ms": round(self.total_latency_ms, 2),
            "total_ms": self.total_ms,
            "total_cost": self.total_cost,
            "failed": self.is_failed(),
            "low_score": self.is_low_score(),
            "created_at": self.created_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            # 结构化分区
            "prompt": self.prompt.to_dict() if self.prompt else None,
            "retrieval": self.retrieval.to_dict() if self.retrieval else None,
            "memory": [m.to_dict() for m in self.memory],
            "tools": [t.to_dict() for t in self.tools],
            "model": [m.to_dict() for m in self.model],
            "latency": self.latency.to_dict(),
            "cost": self.cost.to_dict() if self.cost else None,
            "result": self.result.to_dict() if self.result else None,
            # 向后兼容事件流
            "events": [e.to_dict() for e in self.events],
        }
        if redact:
            data = _deep_redact(data)
        return data


# ── TraceService ─────────────────────────────────────────────
_COLUMNS = ("request_id", "session_id", "user_id", "tenant", "scene",
            "input_text", "total_latency_ms", "total_ms", "cost",
            "failed", "low_score", "created_at", "completed_at", "trace_json")
_SELECT_COLS = ", ".join(_COLUMNS)
_INSERT_SQL = """INSERT INTO traces (
    request_id, session_id, user_id, tenant, scene, input_text,
    total_latency_ms, total_ms, cost, failed, low_score,
    created_at, completed_at, trace_json
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (request_id) DO UPDATE SET
    session_id=EXCLUDED.session_id, user_id=EXCLUDED.user_id,
    tenant=EXCLUDED.tenant, scene=EXCLUDED.scene,
    input_text=EXCLUDED.input_text,
    total_latency_ms=EXCLUDED.total_latency_ms, total_ms=EXCLUDED.total_ms,
    cost=EXCLUDED.cost, failed=EXCLUDED.failed, low_score=EXCLUDED.low_score,
    created_at=EXCLUDED.created_at, completed_at=EXCLUDED.completed_at,
    trace_json=EXCLUDED.trace_json"""


class TraceService:
    """Trace persistence service.

    Production uses PostgreSQL/JSONB.  An explicitly supplied SQLite path is
    retained as a hermetic test/migration adapter only; the application never
    selects it implicitly.  This keeps the runtime PostgreSQL + pgvector-only
    while preserving the legacy trace migration tests and local replay tools.
    """

    _SQLITE_COLUMNS = {
        "request_id": "TEXT PRIMARY KEY",
        "session_id": "TEXT",
        "user_id": "TEXT",
        "tenant": "TEXT",
        "scene": "TEXT",
        "input_text": "TEXT",
        "total_latency_ms": "REAL",
        "total_ms": "REAL",
        "cost": "REAL",
        "failed": "INTEGER NOT NULL DEFAULT 0",
        "low_score": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "TEXT",
        "completed_at": "TEXT",
        "trace_json": "TEXT NOT NULL DEFAULT '{}'",
    }

    def __init__(self, db_path: str = "postgresql"):
        # Only an explicit non-default path enables the test/migration adapter.
        self.db_path = str(db_path or "postgresql")
        self._sqlite = self.db_path.lower() not in {"postgresql", "postgres", "pg"}
        if self._sqlite:
            self._init_sqlite_schema()
        else:
            init_runtime_schema()

    def close(self) -> None:
        # Connections are intentionally per-operation, so there is no pool to
        # close and TemporaryDirectory cleanup is safe on Windows.
        return None

    def _sqlite_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sqlite_schema(self) -> None:
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = self._sqlite_connection()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS traces (request_id TEXT PRIMARY KEY)")
            existing = {row[1] for row in conn.execute("PRAGMA table_info(traces)")}
            for name, ddl in self._SQLITE_COLUMNS.items():
                if name not in existing and name != "request_id":
                    conn.execute(f"ALTER TABLE traces ADD COLUMN {name} {ddl}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_user ON traces(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_time ON traces(created_at)")
            conn.commit()
        finally:
            conn.close()

    def _prepare_row(self, trace: TraceSession) -> Tuple:
        trace.finalize()
        data = trace.to_dict(redact=True)
        trace.input_text = data["input_text"]
        trace_json = (json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                      if self._sqlite else Jsonb(data))
        return (
            trace.request_id, trace.session_id, trace.user_id,
            data.get("tenant", ""), data.get("scene", ""), data["input_text"],
            trace.total_latency_ms, data["total_ms"], data["total_cost"],
            int(bool(data.get("failed"))), int(bool(data.get("low_score"))),
            data.get("created_at"), data.get("completed_at"), trace_json,
        )

    def save_trace(self, trace: TraceSession) -> None:
        values = self._prepare_row(trace)
        if self._sqlite:
            conn = self._sqlite_connection()
            try:
                conn.execute(
                    """INSERT INTO traces
                    (request_id,session_id,user_id,tenant,scene,input_text,
                     total_latency_ms,total_ms,cost,failed,low_score,
                     created_at,completed_at,trace_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(request_id) DO UPDATE SET
                     session_id=excluded.session_id,user_id=excluded.user_id,
                     tenant=excluded.tenant,scene=excluded.scene,
                     input_text=excluded.input_text,
                     total_latency_ms=excluded.total_latency_ms,
                     total_ms=excluded.total_ms,cost=excluded.cost,
                     failed=excluded.failed,low_score=excluded.low_score,
                     created_at=excluded.created_at,completed_at=excluded.completed_at,
                     trace_json=excluded.trace_json""", values)
                conn.commit()
            finally:
                conn.close()
            return
        conn = connect()
        try:
            conn.execute(_INSERT_SQL, values)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def finalize_and_save(self, trace: TraceSession) -> None:
        await asyncio.to_thread(self.save_trace, trace)

    @staticmethod
    def _row_to_dict(row) -> dict:
        if row is None:
            return {}
        result = dict(row)
        payload = result.get("trace_json")
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        result["trace_json"] = payload or {}
        for key in ("created_at", "completed_at"):
            if result.get(key) is not None:
                result[key] = str(result[key])
        return result

    def get_recent_traces(self, limit: int = 20) -> List[dict]:
        return self.select_traces(limit=limit)

    def get_trace_by_id(self, request_id: str) -> Optional[dict]:
        if self._sqlite:
            conn = self._sqlite_connection()
            try:
                row = conn.execute(
                    f"SELECT {_SELECT_COLS} FROM traces WHERE request_id=?",
                    (request_id,)).fetchone()
                return self._row_to_dict(row) if row else None
            finally:
                conn.close()
        conn = connect()
        try:
            row = conn.execute(
                f"SELECT {_SELECT_COLS} FROM traces WHERE request_id=%s",
                (request_id,)).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def select_traces(self, *, user_id: Optional[str] = None,
                      session_id: Optional[str] = None,
                      tenant: Optional[str] = None, scene: Optional[str] = None,
                      start_time: Optional[str] = None,
                      end_time: Optional[str] = None,
                      limit: int = 100) -> List[dict]:
        clauses: List[str] = []
        params: List[Any] = []
        placeholder = "?" if self._sqlite else "%s"
        for col, val in (("user_id", user_id), ("session_id", session_id),
                         ("tenant", tenant), ("scene", scene)):
            if val is not None:
                clauses.append(f"{col} = {placeholder}")
                params.append(val)
        if start_time is not None:
            clauses.append(f"created_at >= {placeholder}")
            params.append(start_time)
        if end_time is not None:
            clauses.append(f"created_at <= {placeholder}")
            params.append(end_time)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        if self._sqlite:
            conn = self._sqlite_connection()
            try:
                rows = conn.execute(
                    f"SELECT {_SELECT_COLS} FROM traces{where} "
                    "ORDER BY created_at DESC LIMIT ?", tuple(params)).fetchall()
                return [self._row_to_dict(row) for row in rows]
            finally:
                conn.close()
        conn = connect()
        try:
            rows = conn.execute(
                f"SELECT {_SELECT_COLS} FROM traces{where} "
                "ORDER BY created_at DESC NULLS LAST LIMIT %s", tuple(params)).fetchall()
            return [self._row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def get_stats(self) -> dict:
        if self._sqlite:
            conn = self._sqlite_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS count, AVG(total_latency_ms) AS avg_latency, "
                    "SUM(cost) AS total_cost FROM traces").fetchone()
            finally:
                conn.close()
        else:
            conn = connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS count, AVG(total_latency_ms) AS avg_latency, "
                    "SUM(cost) AS total_cost FROM traces").fetchone()
            finally:
                conn.close()
        return {"total_requests": row["count"] or 0,
                "avg_latency_ms": round(float(row["avg_latency"] or 0), 2),
                "total_cost": round(float(row["total_cost"] or 0), 6)}


class AlertRule:

    """单条告警规则。

    agg: 'avg' | 'max' | 'sum' | 'count' | 'rate' (count/窗口秒数)
    op:  'gt' | 'lt'
    """

    __slots__ = ("name", "metric_name", "threshold", "agg", "op",
                 "window_seconds", "cooldown_seconds", "min_samples")

    def __init__(self, name: str, metric_name: str, threshold: float,
                 agg: str = "avg", op: str = "gt",
                 window_seconds: int = 300, cooldown_seconds: int = 600,
                 min_samples: int = 1):
        self.name = name
        self.metric_name = metric_name
        self.threshold = threshold
        self.agg = agg
        self.op = op
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.min_samples = min_samples


class AlertService:
    """滑动窗口告警服务。

    - 请求路径: `record(metric_name, value)` — O(1) 入队。
    - 后台任务: 周期调用 `check_and_alert()` (例如每 30s), **不要在请求内联调用**。

    - `time_fn` 可注入 (测试用), 默认 time.time。
    - `add_handler(fn)` 注册告警回调, 触发时收到 dict payload。
    """

    _MAX_POINTS_PER_METRIC = 100_000

    def __init__(self, time_fn: Callable[[], float] = time.time):
        self._time_fn = time_fn
        self._windows: Dict[str, Deque[Tuple[float, float]]] = {}
        self._rules: List[AlertRule] = []
        self._last_fired: Dict[str, float] = {}
        self._handlers: List[Callable[[dict], Any]] = []
        self._lock = threading.Lock()

    def add_rule(self, name: str, metric_name: str, threshold: float,
                 agg: str = "avg", op: str = "gt",
                 window_seconds: int = 300, cooldown_seconds: int = 600,
                 min_samples: int = 1) -> None:
        self._rules.append(AlertRule(
            name, metric_name, threshold, agg, op,
            window_seconds, cooldown_seconds, min_samples))

    def add_handler(self, handler: Callable[[dict], Any]) -> None:
        self._handlers.append(handler)

    def record(self, metric_name: str, value: float = 1.0) -> None:
        """记录一个观测点 (ts, value)。请求路径调用, O(1)。"""
        now = self._time_fn()
        with self._lock:
            dq = self._windows.get(metric_name)
            if dq is None:
                dq = deque(maxlen=self._MAX_POINTS_PER_METRIC)
                self._windows[metric_name] = dq
            dq.append((now, float(value)))

    @staticmethod
    def _prune(dq: Deque[Tuple[float, float]], cutoff: float) -> None:
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _window_values(self, metric_name: str, window_seconds: int) -> List[float]:
        now = self._time_fn()
        with self._lock:
            dq = self._windows.get(metric_name)
            if not dq:
                return []
            self._prune(dq, now - window_seconds)
            return [v for (_, v) in dq]

    @staticmethod
    def _aggregate(values: List[float], agg: str, window_seconds: int) -> Optional[float]:
        if agg == "count":
            return float(len(values))
        if agg == "rate":
            return len(values) / window_seconds if window_seconds > 0 else None
        if not values:
            return None
        if agg == "avg":
            return sum(values) / len(values)
        if agg == "max":
            return max(values)
        if agg == "sum":
            return sum(values)
        return None

    def check_and_alert(self) -> List[dict]:
        """评估所有规则; 返回本次触发的告警 payload 列表。

        由后台任务周期调用 (见类 docstring), 不在请求内联。
        """
        now = self._time_fn()
        fired: List[dict] = []
        for rule in self._rules:
            last = self._last_fired.get(rule.name)
            if last is not None and (now - last) < rule.cooldown_seconds:
                continue
            values = self._window_values(rule.metric_name, rule.window_seconds)
            if rule.agg not in ("count", "rate") and len(values) < rule.min_samples:
                continue
            current = self._aggregate(values, rule.agg, rule.window_seconds)
            if current is None:
                continue
            breached = current > rule.threshold if rule.op == "gt" else current < rule.threshold
            if not breached:
                continue
            self._last_fired[rule.name] = now
            payload = {
                "alert": rule.name,
                "metric": rule.metric_name,
                "agg": rule.agg,
                "value": round(current, 4),
                "threshold": rule.threshold,
                "window_seconds": rule.window_seconds,
                "fired_at": now,
            }
            fired.append(payload)
            logger.warning("ALERT %s: %s(%s)=%.4f threshold=%s",
                           rule.name, rule.agg, rule.metric_name,
                           current, rule.threshold)
            for h in self._handlers:
                try:
                    h(payload)
                except Exception:
                    logger.error("alert handler failed", exc_info=True)
        return fired


# ── 模块级单例 (惰性 TraceService, 便于自定义 db 路径) ─────────
alert_service = AlertService()

_trace_service: Optional[TraceService] = None
_trace_lock = threading.Lock()


def get_trace_service(db_path: str = "postgresql") -> TraceService:
    """获取全局 TraceService 单例 (首次调用决定 db 路径)。"""
    global _trace_service
    if _trace_service is None:
        with _trace_lock:
            if _trace_service is None:
                _trace_service = TraceService(db_path)
    return _trace_service


__all__ = [
    "TraceEvent", "TraceSession", "TraceService",
    "PromptRecord", "RetrievalRecord", "RetrievalChunk", "MemoryHit",
    "ToolRecord", "ModelRecord", "LatencyRecord", "CostRecord", "ResultRecord",
    "AlertRule", "AlertService", "alert_service",
    "get_trace_service", "redact_pii",
]
=======
"""
agent/observability.py — Trace 持久化 + 滑动窗口告警 (P3 可观测性栈)

**升级 (可回放 Trace)**: TraceSession 从"只记 input_text + events"升级为
**完整可回放**的结构化追踪。八个结构化分区, 每区一个 dataclass:

  - prompt    : 模板名 / 版本 / 变量摘要 / 最终渲染消息结构
  - retrieval : Query / 召回片段 / 分数 / 来源 / 权限过滤 / Rerank 排名
  - memory    : 命中记忆 / 来源 / 更新时间 / 置信度
  - tools     : 工具名 / 参数 / 权限结果 / 耗时 / 返回摘要 / 错误  (列表, 可多次)
  - model     : 供应商 / 模型 / 采样参数 / 输入输出 Token / finish / TTFT (列表, 可多次)
  - latency   : 入口 / 检索 / 模型 TTFT / 工具 / 总耗时
  - cost      : 输入成本 / 输出成本 / 缓存命中 / 租户+场景归因
  - result    : 最终答案 / 结构化解析 / 用户反馈 / 评测分数

便捷记录方法: record_prompt / record_retrieval / record_memory / record_tool /
record_model / record_latency / record_cost / record_result。旧的
`add_event(...)` 与 `events` 字段完整保留 (runner/app 在用)。

**PII**: 所有可能含 PII 的字段 (input_text / 渲染 prompt / 记忆 / 工具参数与返回 /
最终答案 / 检索片段) 在**落盘前**统一过 `redact_pii` 脱敏钩子 (深度递归)。

**落盘**: traces 表用 trace_json 存完整可回放结构; 关键字段做独立列并建索引
(request_id / session_id / user_id / tenant / scene / total_ms / cost / created_at)。
大字段 (完整 prompt / 答案) 只存 trace_json, 业务列存摘要/hash。PRAGMA 迁移兼容旧库。

组成:
1. Trace* dataclasses + TraceEvent + TraceSession — 一次请求的结构化追踪。
2. TraceService — 异步持久化到 SQLite (aiosqlite 守卫; 降级 sqlite3 WAL)。
   **app 层必须在请求结束时调用 `await trace_service.finalize_and_save(trace)`**。
3. AlertService — 真滑动窗口告警。

指标收集不在本模块 — 统一见 agent/metrics.py。回放能力见 agent/trace_replay.py。
trace = 单请求全量可回放; metrics = 聚合; log = 事件流。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("agent.observability")

# ── 可选依赖: aiosqlite ───────────────────────────────────────
from psycopg.types.json import Jsonb
from agent.runtime_db import connect, init_runtime_schema

try:
    from agent.security.pii_redactor import redact as _pii_redact
except Exception:  # pragma: no cover
    try:
        from security.pii_redactor import redact as _pii_redact  # type: ignore
    except Exception:
        _pii_redact = None


def redact_pii(text: str) -> str:
    """PII 脱敏钩子: 脱敏失败/模块缺失时原样返回, 绝不阻断主流程。"""
    if not text or not isinstance(text, str) or _pii_redact is None:
        return text
    try:
        result = _pii_redact(text)
        return getattr(result, "redacted_text", None) or text
    except Exception:
        logger.warning("pii redaction failed; storing original text", exc_info=True)
        return text


def _deep_redact(obj: Any) -> Any:
    """递归对结构中的所有字符串过 PII 脱敏钩子 (dict/list/tuple/str)。

    非字符串标量原样返回; PII 正则只命中手机号/身份证/邮箱/银行卡, 对
    request_id / 模型名 / 时间戳 等无副作用。
    """
    if isinstance(obj, str):
        return redact_pii(obj)
    if isinstance(obj, dict):
        return {k: _deep_redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_deep_redact(v) for v in obj]
    return obj


def _summarize(value: Any, limit: int = 200) -> str:
    """把任意值压成短摘要字符串 (用于变量摘要 / 返回摘要)。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "…(+%d)" % (len(value) - limit)


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# ── 结构化分区 dataclass ──────────────────────────────────────
@dataclass
class PromptRecord:
    """Prompt 分区: 模板名 / 版本 / 变量摘要 / 渲染后消息结构。"""
    template_name: str = ""
    version: str = ""
    variables_summary: Dict[str, str] = field(default_factory=dict)
    rendered_messages: List[Dict[str, str]] = field(default_factory=list)
    rendered_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetrievalChunk:
    """单个召回片段。"""
    text_summary: str = ""
    score: Optional[float] = None
    source: str = ""
    acl_allowed: Optional[bool] = None
    rerank_rank: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetrievalRecord:
    """检索分区: Query / 召回片段 / 分数 / 来源 / 权限过滤 / Rerank。"""
    query: str = ""
    chunks: List[RetrievalChunk] = field(default_factory=list)
    num_recalled: int = 0
    num_acl_filtered: int = 0
    rerank_applied: bool = False

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "chunks": [c.to_dict() for c in self.chunks],
            "num_recalled": self.num_recalled,
            "num_acl_filtered": self.num_acl_filtered,
            "rerank_applied": self.rerank_applied,
        }


@dataclass
class MemoryHit:
    """命中的一条记忆。"""
    content: str = ""
    source: str = ""
    updated_at: str = ""
    confidence: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolRecord:
    """工具调用: 名称 / 参数 / 权限结果 / 耗时 / 返回摘要 / 错误。"""
    name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    acl_result: Optional[str] = None
    duration_ms: float = 0.0
    result_summary: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ModelRecord:
    """模型调用: 供应商 / 模型 / 采样参数 / 输入输出 Token / finish / TTFT。"""
    provider: str = ""
    model: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""
    ttft_ms: Optional[float] = None
    stage: str = ""  # intent / generate / ...

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LatencyRecord:
    """延迟分区: 入口 / 检索 / 模型 TTFT / 工具 / 总耗时。"""
    entry_ms: Optional[float] = None
    retrieval_ms: Optional[float] = None
    model_ttft_ms: Optional[float] = None
    tool_ms: Optional[float] = None
    total_ms: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CostRecord:
    """成本分区: 输入成本 / 输出成本 / 缓存命中 / 租户+场景归因。"""
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_hit: bool = False
    tenant: str = ""
    scene: str = ""
    prompt_version: str = ""

    @property
    def total_cost(self) -> float:
        return round(float(self.input_cost) + float(self.output_cost), 8)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_cost"] = self.total_cost
        return d


@dataclass
class ResultRecord:
    """结果分区: 最终答案 / 结构化解析 / 用户反馈 / 评测分数。"""
    answer: str = ""
    parsed: Dict[str, Any] = field(default_factory=dict)
    feedback: Optional[str] = None
    eval_score: Optional[float] = None
    answer_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Trace 结构 ────────────────────────────────────────────────
class TraceEvent:
    """单个追踪事件 (向后兼容的事件流)。"""

    __slots__ = ("event_type", "data", "duration_ms", "timestamp")

    def __init__(self, event_type: str, data: dict, duration_ms: float = 0):
        self.event_type = event_type
        self.data = data
        self.duration_ms = duration_ms
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "type": self.event_type,
            "data": self.data,
            "duration_ms": round(self.duration_ms, 2),
            "timestamp": self.timestamp,
        }


class TraceSession:
    """一次请求的完整可回放追踪。

    request_id 用于跨日志/指标/trace 的关联传播。除向后兼容的 events 事件流外,
    新增八个结构化分区 (prompt/retrieval/memory/tools/model/latency/cost/result),
    通过 record_* 便捷方法填充。所有可能含 PII 的字段在落盘/序列化(redact=True)时
    统一过脱敏钩子。
    """

    def __init__(self, request_id: Optional[str] = None, user_id: str = "",
                 input_text: str = "", session_id: str = "",
                 tenant: str = "", scene: str = ""):
        self.request_id = request_id or uuid.uuid4().hex
        self.user_id = user_id
        self.session_id = session_id
        self.tenant = tenant
        self.scene = scene
        self.input_text = (input_text or "")[:500]
        self.events: List[TraceEvent] = []
        self.start_time = time.time()
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.total_latency_ms: float = 0.0
        self._finalized = False

        # 八个结构化分区
        self.prompt: Optional[PromptRecord] = None
        self.retrieval: Optional[RetrievalRecord] = None
        self.memory: List[MemoryHit] = []
        self.tools: List[ToolRecord] = []
        self.model: List[ModelRecord] = []
        self.latency: LatencyRecord = LatencyRecord()
        self.cost: Optional[CostRecord] = None
        self.result: Optional[ResultRecord] = None

    # ── 向后兼容的事件流 ──
    def add_event(self, event_type: str, data: dict, duration_ms: float = 0) -> None:
        self.events.append(TraceEvent(event_type, data, duration_ms))

    # ── record_* 便捷方法 ──
    def record_prompt(self, template_name: str = "", version: str = "",
                      variables: Optional[Dict[str, Any]] = None,
                      rendered_messages: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        """记录 Prompt: 模板名 / 版本 / 变量摘要 / 最终渲染消息结构。"""
        variables = variables or {}
        msgs = [
            {"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
            for m in (rendered_messages or [])
            if isinstance(m, dict)
        ]
        rendered_flat = "\n".join(m["content"] for m in msgs)
        self.prompt = PromptRecord(
            template_name=template_name,
            version=version,
            variables_summary={str(k): _summarize(v, 120) for k, v in variables.items()},
            rendered_messages=msgs,
            rendered_hash=_hash(rendered_flat),
        )

    def record_retrieval(self, query: str = "",
                         chunks: Optional[Sequence[Any]] = None,
                         scores: Optional[Sequence[float]] = None,
                         sources: Optional[Sequence[str]] = None,
                         acl: Optional[Sequence[bool]] = None,
                         rerank: Optional[Sequence[int]] = None) -> None:
        """记录检索: Query / 召回片段 / 分数 / 来源 / 权限过滤 / Rerank 排名。

        chunks/scores/sources/acl/rerank 为按下标并行的序列。
        """
        chunks = list(chunks or [])
        scores = list(scores or [])
        sources = list(sources or [])
        acl = list(acl or [])
        rerank = list(rerank or [])

        def _at(seq: List[Any], i: int) -> Any:
            return seq[i] if i < len(seq) else None

        recs: List[RetrievalChunk] = []
        acl_filtered = 0
        for i, ch in enumerate(chunks):
            allowed = _at(acl, i)
            if allowed is False:
                acl_filtered += 1
            recs.append(RetrievalChunk(
                text_summary=_summarize(ch if isinstance(ch, str) else
                                        (ch.get("text") if isinstance(ch, dict) else ch), 300),
                score=(_at(scores, i) if _at(scores, i) is None
                       else float(_at(scores, i))),
                source=str(_at(sources, i) or (ch.get("source") if isinstance(ch, dict) else "") or ""),
                acl_allowed=(None if allowed is None else bool(allowed)),
                rerank_rank=(None if _at(rerank, i) is None else int(_at(rerank, i))),
            ))
        self.retrieval = RetrievalRecord(
            query=query,
            chunks=recs,
            num_recalled=len(recs),
            num_acl_filtered=acl_filtered,
            rerank_applied=bool(rerank),
        )

    def record_memory(self, hits: Optional[Sequence[Any]] = None) -> None:
        """记录命中的记忆: 内容 / 来源 / 更新时间 / 置信度。"""
        out: List[MemoryHit] = []
        for h in (hits or []):
            if isinstance(h, dict):
                out.append(MemoryHit(
                    content=_summarize(h.get("content") or h.get("value") or h.get("text"), 300),
                    source=str(h.get("source", "")),
                    updated_at=str(h.get("updated_at", "") or h.get("updatedAt", "")),
                    confidence=(None if h.get("confidence") is None else float(h.get("confidence"))),
                ))
            else:
                out.append(MemoryHit(content=_summarize(h, 300)))
        self.memory = out

    def record_tool(self, name: str, args: Optional[Dict[str, Any]] = None,
                    acl: Optional[str] = None, ms: float = 0.0,
                    result: Any = None, error: Optional[str] = None) -> None:
        """记录一次工具调用 (可多次, 追加到 tools 列表)。"""
        self.tools.append(ToolRecord(
            name=name,
            args=dict(args or {}),
            acl_result=(None if acl is None else str(acl)),
            duration_ms=round(float(ms), 2),
            result_summary=_summarize(result, 300),
            error=(None if error is None else str(error)),
        ))

    def record_model(self, provider: str = "", model: str = "",
                     params: Optional[Dict[str, Any]] = None,
                     in_tok: int = 0, out_tok: int = 0,
                     finish: str = "", ttft_ms: Optional[float] = None,
                     stage: str = "") -> None:
        """记录一次模型调用 (可多次: 意图/生成…, 追加到 model 列表)。"""
        self.model.append(ModelRecord(
            provider=provider, model=model,
            params=dict(params or {}),
            input_tokens=int(in_tok or 0), output_tokens=int(out_tok or 0),
            finish_reason=finish,
            ttft_ms=(None if ttft_ms is None else round(float(ttft_ms), 2)),
            stage=stage,
        ))

    def record_latency(self, entry_ms: Optional[float] = None,
                       retrieval_ms: Optional[float] = None,
                       model_ttft_ms: Optional[float] = None,
                       tool_ms: Optional[float] = None,
                       total_ms: Optional[float] = None) -> None:
        """记录延迟分区 (只覆盖显式传入的字段)。"""
        lat = self.latency
        if entry_ms is not None:
            lat.entry_ms = round(float(entry_ms), 2)
        if retrieval_ms is not None:
            lat.retrieval_ms = round(float(retrieval_ms), 2)
        if model_ttft_ms is not None:
            lat.model_ttft_ms = round(float(model_ttft_ms), 2)
        if tool_ms is not None:
            lat.tool_ms = round(float(tool_ms), 2)
        if total_ms is not None:
            lat.total_ms = round(float(total_ms), 2)

    def record_cost(self, input_cost: float = 0.0, output_cost: float = 0.0,
                    cache_hit: bool = False, tenant: str = "",
                    scene: str = "", prompt_version: str = "") -> None:
        """记录成本分区 (按租户和场景归因)。"""
        self.cost = CostRecord(
            input_cost=float(input_cost or 0.0),
            output_cost=float(output_cost or 0.0),
            cache_hit=bool(cache_hit),
            tenant=tenant or self.tenant,
            scene=scene or self.scene,
            prompt_version=prompt_version,
        )
        if tenant:
            self.tenant = tenant
        if scene:
            self.scene = scene

    def record_result(self, answer: str = "", parsed: Optional[Dict[str, Any]] = None,
                      feedback: Optional[str] = None,
                      eval_score: Optional[float] = None) -> None:
        """记录最终结果: 答案 / 结构化解析 / 用户反馈 / 评测分数。"""
        answer = answer or ""
        self.result = ResultRecord(
            answer=answer,
            parsed=dict(parsed or {}),
            feedback=(None if feedback is None else str(feedback)),
            eval_score=(None if eval_score is None else float(eval_score)),
            answer_hash=_hash(answer),
        )

    # ── 派生属性 ──
    @property
    def total_ms(self) -> float:
        if self.latency.total_ms is not None:
            return round(self.latency.total_ms, 2)
        return round(self.total_latency_ms, 2)

    @property
    def total_cost(self) -> float:
        return self.cost.total_cost if self.cost else 0.0

    def is_failed(self) -> bool:
        """请求是否失败: 任一工具报错 / 模型 finish=error / 有答案分区但答案为空。"""
        if any(t.error for t in self.tools):
            return True
        if any((m.finish_reason or "").lower() in ("error", "content_filter")
               for m in self.model):
            return True
        if self.result is not None and not (self.result.answer or "").strip():
            return True
        return False

    def is_low_score(self, threshold: float = 0.5) -> bool:
        """是否低分/差评: eval_score < threshold 或负向反馈。"""
        if self.result is None:
            return False
        if self.result.eval_score is not None and self.result.eval_score < threshold:
            return True
        fb = (self.result.feedback or "").lower()
        return fb in ("thumbs_down", "negative", "bad", "down", "0")

    def finalize(self) -> None:
        """完成追踪, 计算总耗时 (幂等)。"""
        if not self._finalized:
            self.total_latency_ms = (time.time() - self.start_time) * 1000
            if self.latency.total_ms is None:
                self.latency.total_ms = round(self.total_latency_ms, 2)
            self._finalized = True

    def to_dict(self, redact: bool = False) -> dict:
        """完整可序列化结构。redact=True 时对全部字符串深度脱敏 (落盘用)。"""
        data = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant": self.tenant,
            "scene": self.scene,
            "input_text": self.input_text,
            "total_latency_ms": round(self.total_latency_ms, 2),
            "total_ms": self.total_ms,
            "total_cost": self.total_cost,
            "failed": self.is_failed(),
            "low_score": self.is_low_score(),
            "created_at": self.created_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            # 结构化分区
            "prompt": self.prompt.to_dict() if self.prompt else None,
            "retrieval": self.retrieval.to_dict() if self.retrieval else None,
            "memory": [m.to_dict() for m in self.memory],
            "tools": [t.to_dict() for t in self.tools],
            "model": [m.to_dict() for m in self.model],
            "latency": self.latency.to_dict(),
            "cost": self.cost.to_dict() if self.cost else None,
            "result": self.result.to_dict() if self.result else None,
            # 向后兼容事件流
            "events": [e.to_dict() for e in self.events],
        }
        if redact:
            data = _deep_redact(data)
        return data


# ── TraceService ─────────────────────────────────────────────
_COLUMNS = ("request_id", "session_id", "user_id", "tenant", "scene",
            "input_text", "total_latency_ms", "total_ms", "cost",
            "failed", "low_score", "created_at", "completed_at", "trace_json")
_SELECT_COLS = ", ".join(_COLUMNS)
_INSERT_SQL = """INSERT INTO traces (
    request_id, session_id, user_id, tenant, scene, input_text,
    total_latency_ms, total_ms, cost, failed, low_score,
    created_at, completed_at, trace_json
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (request_id) DO UPDATE SET
    session_id=EXCLUDED.session_id, user_id=EXCLUDED.user_id,
    tenant=EXCLUDED.tenant, scene=EXCLUDED.scene,
    input_text=EXCLUDED.input_text,
    total_latency_ms=EXCLUDED.total_latency_ms, total_ms=EXCLUDED.total_ms,
    cost=EXCLUDED.cost, failed=EXCLUDED.failed, low_score=EXCLUDED.low_score,
    created_at=EXCLUDED.created_at, completed_at=EXCLUDED.completed_at,
    trace_json=EXCLUDED.trace_json"""


class TraceService:
    """PostgreSQL-backed trace persistence service."""

    def __init__(self, db_path: str = "postgresql"):
        self.db_path = "postgresql"
        init_runtime_schema()

    def close(self) -> None:
        return None

    def _prepare_row(self, trace: TraceSession) -> Tuple:
        trace.finalize()
        data = trace.to_dict(redact=True)
        trace.input_text = data["input_text"]
        return (
            trace.request_id, trace.session_id, trace.user_id,
            data.get("tenant", ""), data.get("scene", ""), data["input_text"],
            trace.total_latency_ms, data["total_ms"], data["total_cost"],
            int(bool(data.get("failed"))), int(bool(data.get("low_score"))),
            data.get("created_at"), data.get("completed_at"), Jsonb(data),
        )

    def save_trace(self, trace: TraceSession) -> None:
        conn = connect()
        try:
            conn.execute(_INSERT_SQL, self._prepare_row(trace))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def finalize_and_save(self, trace: TraceSession) -> None:
        await asyncio.to_thread(self.save_trace, trace)

    @staticmethod
    def _row_to_dict(row) -> dict:
        if row is None:
            return {}
        result = dict(row)
        payload = result.get("trace_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        result["trace_json"] = payload or {}
        for key in ("created_at", "completed_at"):
            if result.get(key) is not None:
                result[key] = str(result[key])
        return result

    def get_recent_traces(self, limit: int = 20) -> List[dict]:
        return self.select_traces(limit=limit)

    def get_trace_by_id(self, request_id: str) -> Optional[dict]:
        conn = connect()
        try:
            row = conn.execute(f"SELECT {_SELECT_COLS} FROM traces WHERE request_id=%s", (request_id,)).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def select_traces(self, *, user_id: Optional[str] = None,
                      session_id: Optional[str] = None,
                      tenant: Optional[str] = None, scene: Optional[str] = None,
                      start_time: Optional[str] = None,
                      end_time: Optional[str] = None,
                      limit: int = 100) -> List[dict]:
        clauses: List[str] = []
        params: List[Any] = []
        for col, val in (("user_id", user_id), ("session_id", session_id),
                         ("tenant", tenant), ("scene", scene)):
            if val is not None:
                clauses.append(f"{col} = %s")
                params.append(val)
        if start_time is not None:
            clauses.append("created_at >= %s")
            params.append(start_time)
        if end_time is not None:
            clauses.append("created_at <= %s")
            params.append(end_time)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        conn = connect()
        try:
            rows = conn.execute(
                f"SELECT {_SELECT_COLS} FROM traces{where} ORDER BY created_at DESC NULLS LAST LIMIT %s",
                tuple(params)).fetchall()
            return [self._row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def get_stats(self) -> dict:
        conn = connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS count, AVG(total_latency_ms) AS avg_latency, SUM(cost) AS total_cost FROM traces").fetchone()
            return {"total_requests": row["count"] or 0,
                    "avg_latency_ms": round(float(row["avg_latency"] or 0), 2),
                    "total_cost": round(float(row["total_cost"] or 0), 6)}
        finally:
            conn.close()


class AlertRule:

    """单条告警规则。

    agg: 'avg' | 'max' | 'sum' | 'count' | 'rate' (count/窗口秒数)
    op:  'gt' | 'lt'
    """

    __slots__ = ("name", "metric_name", "threshold", "agg", "op",
                 "window_seconds", "cooldown_seconds", "min_samples")

    def __init__(self, name: str, metric_name: str, threshold: float,
                 agg: str = "avg", op: str = "gt",
                 window_seconds: int = 300, cooldown_seconds: int = 600,
                 min_samples: int = 1):
        self.name = name
        self.metric_name = metric_name
        self.threshold = threshold
        self.agg = agg
        self.op = op
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.min_samples = min_samples


class AlertService:
    """滑动窗口告警服务。

    - 请求路径: `record(metric_name, value)` — O(1) 入队。
    - 后台任务: 周期调用 `check_and_alert()` (例如每 30s), **不要在请求内联调用**。

    - `time_fn` 可注入 (测试用), 默认 time.time。
    - `add_handler(fn)` 注册告警回调, 触发时收到 dict payload。
    """

    _MAX_POINTS_PER_METRIC = 100_000

    def __init__(self, time_fn: Callable[[], float] = time.time):
        self._time_fn = time_fn
        self._windows: Dict[str, Deque[Tuple[float, float]]] = {}
        self._rules: List[AlertRule] = []
        self._last_fired: Dict[str, float] = {}
        self._handlers: List[Callable[[dict], Any]] = []
        self._lock = threading.Lock()

    def add_rule(self, name: str, metric_name: str, threshold: float,
                 agg: str = "avg", op: str = "gt",
                 window_seconds: int = 300, cooldown_seconds: int = 600,
                 min_samples: int = 1) -> None:
        self._rules.append(AlertRule(
            name, metric_name, threshold, agg, op,
            window_seconds, cooldown_seconds, min_samples))

    def add_handler(self, handler: Callable[[dict], Any]) -> None:
        self._handlers.append(handler)

    def record(self, metric_name: str, value: float = 1.0) -> None:
        """记录一个观测点 (ts, value)。请求路径调用, O(1)。"""
        now = self._time_fn()
        with self._lock:
            dq = self._windows.get(metric_name)
            if dq is None:
                dq = deque(maxlen=self._MAX_POINTS_PER_METRIC)
                self._windows[metric_name] = dq
            dq.append((now, float(value)))

    @staticmethod
    def _prune(dq: Deque[Tuple[float, float]], cutoff: float) -> None:
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _window_values(self, metric_name: str, window_seconds: int) -> List[float]:
        now = self._time_fn()
        with self._lock:
            dq = self._windows.get(metric_name)
            if not dq:
                return []
            self._prune(dq, now - window_seconds)
            return [v for (_, v) in dq]

    @staticmethod
    def _aggregate(values: List[float], agg: str, window_seconds: int) -> Optional[float]:
        if agg == "count":
            return float(len(values))
        if agg == "rate":
            return len(values) / window_seconds if window_seconds > 0 else None
        if not values:
            return None
        if agg == "avg":
            return sum(values) / len(values)
        if agg == "max":
            return max(values)
        if agg == "sum":
            return sum(values)
        return None

    def check_and_alert(self) -> List[dict]:
        """评估所有规则; 返回本次触发的告警 payload 列表。

        由后台任务周期调用 (见类 docstring), 不在请求内联。
        """
        now = self._time_fn()
        fired: List[dict] = []
        for rule in self._rules:
            last = self._last_fired.get(rule.name)
            if last is not None and (now - last) < rule.cooldown_seconds:
                continue
            values = self._window_values(rule.metric_name, rule.window_seconds)
            if rule.agg not in ("count", "rate") and len(values) < rule.min_samples:
                continue
            current = self._aggregate(values, rule.agg, rule.window_seconds)
            if current is None:
                continue
            breached = current > rule.threshold if rule.op == "gt" else current < rule.threshold
            if not breached:
                continue
            self._last_fired[rule.name] = now
            payload = {
                "alert": rule.name,
                "metric": rule.metric_name,
                "agg": rule.agg,
                "value": round(current, 4),
                "threshold": rule.threshold,
                "window_seconds": rule.window_seconds,
                "fired_at": now,
            }
            fired.append(payload)
            logger.warning("ALERT %s: %s(%s)=%.4f threshold=%s",
                           rule.name, rule.agg, rule.metric_name,
                           current, rule.threshold)
            for h in self._handlers:
                try:
                    h(payload)
                except Exception:
                    logger.error("alert handler failed", exc_info=True)
        return fired


# ── 模块级单例 (惰性 TraceService, 便于自定义 db 路径) ─────────
alert_service = AlertService()

_trace_service: Optional[TraceService] = None
_trace_lock = threading.Lock()


def get_trace_service(db_path: str = "postgresql") -> TraceService:
    """获取全局 TraceService 单例 (首次调用决定 db 路径)。"""
    global _trace_service
    if _trace_service is None:
        with _trace_lock:
            if _trace_service is None:
                _trace_service = TraceService(db_path)
    return _trace_service


__all__ = [
    "TraceEvent", "TraceSession", "TraceService",
    "PromptRecord", "RetrievalRecord", "RetrievalChunk", "MemoryHit",
    "ToolRecord", "ModelRecord", "LatencyRecord", "CostRecord", "ResultRecord",
    "AlertRule", "AlertService", "alert_service",
    "get_trace_service", "redact_pii",
]
>>>>>>> origin/master
