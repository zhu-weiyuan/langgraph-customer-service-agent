"""
agent/trace_replay.py — Trace 回放能力 ("没有回放就没有优化")。

在 observability.TraceSession 记录的完整可回放结构之上, 提供:

  - load_trace(request_id)          读回完整 trace (解析 trace_json)。
  - replay(request_id, mode)        三种回放:
        mode="inspect"  打印/返回结构化时间线 (每个 span 的耗时/输入/输出)。
        mode="rerun"    用记录的 query/prompt 重新走一遍检索 (注入式 retriever),
                        便于对比"当时召回 vs 现在召回" (RAG 迭代前后对比)。
        mode="diff"     对比两个 trace (需 other 参数)。
  - list_traces(filters)            按 user/scene/时间/是否失败/低分 过滤,
                                    供"捞出所有差评请求回放分析"。

依赖: 仅 observability.TraceService (纯 stdlib)。retriever 以**注入**方式传入,
本模块不直接依赖 RAG 实现 → 可在任意 RAG 版本上对比回放。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from agent.observability import TraceService, get_trace_service

logger = logging.getLogger("agent.trace_replay")

# 注入式 retriever 签名: fn(query: str) -> List[dict|str]
# 返回项建议为 {"text":..., "score":..., "source":...}; 也接受纯字符串。
RetrieverFn = Callable[[str], List[Any]]


def _service(service: Optional[TraceService]) -> TraceService:
    return service or get_trace_service()


def _parse(row: Optional[dict]) -> Optional[dict]:
    """把 DB 行的 trace_json 解析成完整 trace dict。"""
    if not row:
        return None
    raw = row.get("trace_json")
    if not raw:
        # 旧库无 trace_json: 退回行本身的业务列
        return dict(row)
    if isinstance(raw, dict):
        data = dict(raw)
    elif isinstance(raw, (bytes, bytearray)):
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError):
            logger.warning("trace_json parse failed for %s", row.get("request_id"))
            return dict(row)
    else:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("trace_json parse failed for %s", row.get("request_id"))
            return dict(row)
    # 用列值补齐 (列是权威的索引值)
    for k in ("request_id", "session_id", "user_id", "tenant", "scene"):
        if row.get(k):
            data.setdefault(k, row.get(k))
    return data


# ── load ──────────────────────────────────────────────────────
def load_trace(request_id: str, service: Optional[TraceService] = None) -> Optional[dict]:
    """读回完整 trace (含全部结构化分区)。找不到返回 None。"""
    svc = _service(service)
    return _parse(svc.get_trace_by_id(request_id))


# ── inspect: 结构化时间线 ─────────────────────────────────────
def build_timeline(trace: dict) -> List[dict]:
    """从 trace 结构组装有序 span 列表: name / kind / duration_ms / input / output。"""
    spans: List[dict] = []

    # 1) 事件流 (保留时序)
    for ev in trace.get("events") or []:
        spans.append({
            "name": ev.get("type", "event"),
            "kind": "event",
            "duration_ms": round(float(ev.get("duration_ms", 0) or 0), 2),
            "input": ev.get("data"),
            "output": None,
            "timestamp": ev.get("timestamp"),
        })

    # 2) 检索 span
    retr = trace.get("retrieval")
    if retr:
        spans.append({
            "name": "retrieval",
            "kind": "retrieval",
            "duration_ms": round(float((trace.get("latency") or {}).get("retrieval_ms") or 0), 2),
            "input": {"query": retr.get("query")},
            "output": {
                "num_recalled": retr.get("num_recalled"),
                "num_acl_filtered": retr.get("num_acl_filtered"),
                "top_sources": [c.get("source") for c in (retr.get("chunks") or [])[:5]],
            },
        })

    # 3) 模型 span (可多次)
    for m in trace.get("model") or []:
        spans.append({
            "name": "model:%s" % (m.get("stage") or m.get("model") or "call"),
            "kind": "model",
            "duration_ms": round(float(m.get("ttft_ms") or 0), 2),
            "input": {"provider": m.get("provider"), "model": m.get("model"),
                      "input_tokens": m.get("input_tokens"),
                      "params": m.get("params")},
            "output": {"output_tokens": m.get("output_tokens"),
                       "finish_reason": m.get("finish_reason")},
        })

    # 4) 工具 span (可多次)
    for t in trace.get("tools") or []:
        spans.append({
            "name": "tool:%s" % t.get("name", ""),
            "kind": "tool",
            "duration_ms": round(float(t.get("duration_ms") or 0), 2),
            "input": {"args": t.get("args"), "acl_result": t.get("acl_result")},
            "output": {"result_summary": t.get("result_summary"),
                       "error": t.get("error")},
        })

    return spans


def format_timeline(trace: dict) -> str:
    """把时间线渲染成可读文本 (供 CLI / inspect 打印)。"""
    lines: List[str] = []
    lines.append("=" * 68)
    lines.append("TRACE %s" % trace.get("request_id", "?"))
    lines.append("  user=%s session=%s tenant=%s scene=%s"
                 % (trace.get("user_id", ""), trace.get("session_id", ""),
                    trace.get("tenant", ""), trace.get("scene", "")))
    lat = trace.get("latency") or {}
    lines.append("  total_ms=%s  cost=%s  failed=%s  low_score=%s"
                 % (trace.get("total_ms"), trace.get("total_cost"),
                    trace.get("failed"), trace.get("low_score")))
    lines.append("  latency: entry=%s retrieval=%s ttft=%s tool=%s total=%s"
                 % (lat.get("entry_ms"), lat.get("retrieval_ms"),
                    lat.get("model_ttft_ms"), lat.get("tool_ms"), lat.get("total_ms")))
    pr = trace.get("prompt")
    if pr:
        lines.append("  prompt: %s v%s (%d vars, %d msgs)"
                     % (pr.get("template_name"), pr.get("version"),
                        len(pr.get("variables_summary") or {}),
                        len(pr.get("rendered_messages") or [])))
    lines.append("-" * 68)
    lines.append("TIMELINE (spans):")
    for i, span in enumerate(build_timeline(trace)):
        lines.append("  [%02d] %-22s %8.2fms  %s"
                     % (i, span["name"], span["duration_ms"], span["kind"]))
        if span.get("input"):
            lines.append("        in : %s" % _short(span["input"]))
        if span.get("output"):
            lines.append("        out: %s" % _short(span["output"]))
    res = trace.get("result")
    if res:
        lines.append("-" * 68)
        lines.append("RESULT: feedback=%s eval_score=%s"
                     % (res.get("feedback"), res.get("eval_score")))
        lines.append("  answer: %s" % _short(res.get("answer"), 300))
    lines.append("=" * 68)
    return "\n".join(lines)


def _short(value: Any, limit: int = 160) -> str:
    try:
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                            default=str)
    except Exception:
        s = str(value)
    return s if len(s) <= limit else s[:limit] + "…"


# ── rerun: 用记录的 query 重新检索并对比 ──────────────────────
def rerun_retrieval(trace: dict, retriever: Optional[RetrieverFn]) -> dict:
    """用 trace 记录的 query 重新走一遍检索, 返回 recorded vs current 对比。

    retriever 为注入式可调用: fn(query)->list(dict|str)。不传则只回放 recorded。
    """
    retr = trace.get("retrieval") or {}
    query = retr.get("query", "")
    recorded = [
        {"source": c.get("source"), "score": c.get("score"),
         "rerank_rank": c.get("rerank_rank"),
         "text_summary": c.get("text_summary")}
        for c in (retr.get("chunks") or [])
    ]
    out: Dict[str, Any] = {
        "query": query,
        "recorded": recorded,
        "recorded_sources": [c.get("source") for c in recorded],
    }
    if retriever is None:
        out["current"] = None
        out["note"] = "no retriever injected; recorded-only replay"
        return out

    current_raw = retriever(query) or []
    current: List[dict] = []
    for i, item in enumerate(current_raw):
        if isinstance(item, dict):
            current.append({"source": item.get("source", ""),
                            "score": item.get("score"),
                            "rank": i,
                            "text_summary": (item.get("text") or "")[:300]})
        else:
            current.append({"source": "", "score": None, "rank": i,
                            "text_summary": str(item)[:300]})
    out["current"] = current
    out["current_sources"] = [c["source"] for c in current]
    rec_set = {c.get("source") for c in recorded if c.get("source")}
    cur_set = {c.get("source") for c in current if c.get("source")}
    out["sources_added"] = sorted(cur_set - rec_set)
    out["sources_removed"] = sorted(rec_set - cur_set)
    out["sources_stable"] = sorted(rec_set & cur_set)
    return out


# ── diff: 对比两个 trace ──────────────────────────────────────
def diff_traces(a: dict, b: dict) -> dict:
    """对比两个 trace 的关键字段, 返回差异 dict。"""
    def _sum_tokens(t: dict) -> Dict[str, int]:
        ins = sum(int(m.get("input_tokens") or 0) for m in t.get("model") or [])
        outs = sum(int(m.get("output_tokens") or 0) for m in t.get("model") or [])
        return {"input_tokens": ins, "output_tokens": outs}

    def _retr_sources(t: dict) -> List[str]:
        return [c.get("source") for c in ((t.get("retrieval") or {}).get("chunks") or [])]

    diff: Dict[str, Any] = {"a": a.get("request_id"), "b": b.get("request_id"),
                            "fields": {}}
    fields = diff["fields"]

    for key, getter in (
        ("total_ms", lambda t: t.get("total_ms")),
        ("total_cost", lambda t: t.get("total_cost")),
        ("failed", lambda t: t.get("failed")),
        ("low_score", lambda t: t.get("low_score")),
        ("eval_score", lambda t: (t.get("result") or {}).get("eval_score")),
        ("feedback", lambda t: (t.get("result") or {}).get("feedback")),
        ("answer_hash", lambda t: (t.get("result") or {}).get("answer_hash")),
        ("prompt_version", lambda t: (t.get("prompt") or {}).get("version")),
        ("num_tools", lambda t: len(t.get("tools") or [])),
        ("num_model_calls", lambda t: len(t.get("model") or [])),
    ):
        va, vb = getter(a), getter(b)
        if va != vb:
            fields[key] = {"a": va, "b": vb}

    ta, tb = _sum_tokens(a), _sum_tokens(b)
    if ta != tb:
        fields["tokens"] = {"a": ta, "b": tb}

    sa, sb = _retr_sources(a), _retr_sources(b)
    if sa != sb:
        fields["retrieval_sources"] = {
            "a": sa, "b": sb,
            "added": sorted(set(sb) - set(sa)),
            "removed": sorted(set(sa) - set(sb)),
        }

    diff["changed"] = sorted(fields.keys())
    diff["identical"] = not fields
    return diff


# ── replay: 统一入口 ──────────────────────────────────────────
def replay(request_id: str, mode: str = "inspect", *,
           service: Optional[TraceService] = None,
           retriever: Optional[RetrieverFn] = None,
           other: Optional[str] = None,
           echo: bool = True) -> Any:
    """回放一次请求。

    mode="inspect" -> 返回 span 时间线 (list); echo=True 时打印可读文本。
    mode="rerun"   -> 返回 {query, recorded, current, sources_*} 对比 dict。
    mode="diff"    -> 需 other=另一 request_id; 返回 diff dict。
    """
    trace = load_trace(request_id, service)
    if trace is None:
        raise KeyError("trace not found: %s" % request_id)

    if mode == "inspect":
        if echo:
            print(format_timeline(trace))
        return build_timeline(trace)

    if mode == "rerun":
        result = rerun_retrieval(trace, retriever)
        if echo:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    if mode == "diff":
        if not other:
            raise ValueError("mode=diff requires 'other' request_id")
        other_trace = load_trace(other, service)
        if other_trace is None:
            raise KeyError("trace not found: %s" % other)
        result = diff_traces(trace, other_trace)
        if echo:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    raise ValueError("unknown replay mode: %s (want inspect|rerun|diff)" % mode)


# ── list_traces: 过滤检索 ─────────────────────────────────────
def list_traces(filters: Optional[Dict[str, Any]] = None,
                service: Optional[TraceService] = None) -> List[dict]:
    """按过滤条件列出 trace 摘要 (供"捞出所有差评/失败请求回放分析")。

    filters 支持:
      user_id / session_id / tenant / scene   精确匹配 (走索引列 SQL)
      start_time / end_time                   created_at ISO 区间 (走索引)
      failed=True                             只要失败请求 (列过滤)
      low_score=True                          只要低分/差评 (列过滤)
      score_threshold                         (仅用于文档说明, 已在记录时判定)
      limit                                   默认 200

    返回每条: request_id/user_id/session_id/tenant/scene/total_ms/cost/
             failed/low_score/created_at/feedback/eval_score。
    """
    filters = dict(filters or {})
    svc = _service(service)
    rows = svc.select_traces(
        user_id=filters.get("user_id"),
        session_id=filters.get("session_id"),
        tenant=filters.get("tenant"),
        scene=filters.get("scene"),
        start_time=filters.get("start_time"),
        end_time=filters.get("end_time"),
        limit=int(filters.get("limit", 200)),
    )
    want_failed = filters.get("failed")
    want_low = filters.get("low_score")

    out: List[dict] = []
    for row in rows:
        failed = bool(row.get("failed"))
        low = bool(row.get("low_score"))
        if want_failed and not failed:
            continue
        if want_low and not low:
            continue
        parsed = _parse(row) or {}
        res = parsed.get("result") or {}
        out.append({
            "request_id": row.get("request_id"),
            "user_id": row.get("user_id"),
            "session_id": row.get("session_id"),
            "tenant": row.get("tenant"),
            "scene": row.get("scene"),
            "total_ms": row.get("total_ms"),
            "cost": row.get("cost"),
            "failed": failed,
            "low_score": low,
            "created_at": row.get("created_at"),
            "feedback": res.get("feedback"),
            "eval_score": res.get("eval_score"),
        })
    return out


__all__ = [
    "load_trace", "replay", "list_traces",
    "build_timeline", "format_timeline", "rerun_retrieval", "diff_traces",
]
