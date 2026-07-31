# -*- coding: utf-8 -*-
"""
分层评测 Harness —— 统一编排四层指标、跑一批 EvalCase、按层出报告、落 SQLite。

设计要点
────────
* 依赖注入：agent_fn / judge_fn / embed_fn 全部外部传入，harness 自身不 import
  任何三方库（stdlib only），因此 --mock 可无 LLM 纯规则跑通、单测可全 mock。
* agent_fn 契约：agent_fn(case: EvalCase) -> dict，字段（缺省安全）：
      answer:        str            生成回答
      retrieved:     list[str]      检索返回的 doc_id（按 rank 高→低）
      contexts:      list[str]      检索到的上下文文本块
      trajectory:    list[dict]     实际工具调用 [{"tool","args","ok"}]
      output_raw:    str            结构化原始输出串（工程层 JSON/schema 用）
      parsed:        dict|None      已解析对象（可选，schema/enum 用）
      enum_value:    Any            枚举字段实际取值（可选）
      turns:         int            对话轮次
      retries:       int            LLM/工具重试次数
      success:       bool           任务是否最终完成
      run_successes: list[bool]     同一 case 跑 N 次的成功标志（稳定性用，可选）
      noisy_answer:  str            注入无关上下文后的回答（抗噪对比，可选）
      ttft_ms/latency_ms/input_tokens/output_tokens: 工程层数值（可选）
* 落库：eval_runs（run 级 + 归因字段）+ eval_case_results（case 级明细）。
  归因字段：prompt_version / model_id / dataset_version / input_hash / git_commit。

用法见 scripts/run_eval.py。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import metrics as M

# ─────────────────────────────────────────────────────────────────────
# EvalCase
# ─────────────────────────────────────────────────────────────────────


@dataclass
class EvalCase:
    """一条分层评测用例。不同层用到不同字段，缺省安全。"""
    id: str
    query: str = ""
    layer: str = "generation"          # retrieval|generation|agent|engineering
    category: str = "general"
    difficulty: str = "medium"         # easy|medium|hard
    weight: float = 1.0

    # 参考答案 / 要点
    golden_answer: str = ""
    key_points: List[str] = field(default_factory=list)
    expected_keywords: List[str] = field(default_factory=list)

    # 检索层
    golden_context_ids: List[str] = field(default_factory=list)
    reference_points: List[str] = field(default_factory=list)

    # Agent 层：期望轨迹
    trajectory: List[Dict[str, Any]] = field(default_factory=list)  # 期望工具序列
    expected_tools: List[str] = field(default_factory=list)

    # 工程层约束
    schema_required: List[str] = field(default_factory=list)
    enum_field: str = ""
    enum_valid: List[Any] = field(default_factory=list)
    format_pattern: str = ""

    # 对抗 / 拒答
    should_refuse: bool = False

    extra: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "EvalCase":
        known = {f for f in EvalCase.__dataclass_fields__}          # noqa
        base = {k: v for k, v in d.items() if k in known}
        extra = {k: v for k, v in d.items() if k not in known}
        case = EvalCase(**base)
        if extra:
            case.extra.update(extra)
        return case


def load_golden_set(path: str) -> List[EvalCase]:
    """读 eval/golden_set.jsonl → List[EvalCase]（跳过空行 / 坏行）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"golden set not found: {p}")
    cases: List[EvalCase] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            cases.append(EvalCase.from_dict(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return cases


# ─────────────────────────────────────────────────────────────────────
# 目标阈值（面试可背 / 用于“不达标项”判定）—— 方向 higher/lower
# ─────────────────────────────────────────────────────────────────────
TARGETS: Dict[str, Dict[str, Any]] = {
    # retrieval
    "recall_at_k":            {"target": 0.85, "dir": "higher"},
    "hit_rate_at_k":          {"target": 0.90, "dir": "higher"},
    "mrr":                    {"target": 0.80, "dir": "higher"},
    "precision_at_k":         {"target": 0.30, "dir": "higher"},
    "context_precision":      {"target": 0.80, "dir": "higher"},
    "context_recall":         {"target": 0.85, "dir": "higher"},
    # generation
    "faithfulness":           {"target": 0.90, "dir": "higher"},
    "answer_relevance":       {"target": 0.80, "dir": "higher"},
    "completeness":           {"target": 0.80, "dir": "higher"},
    "context_usage":          {"target": 0.50, "dir": "higher"},
    "noise_sensitivity":      {"target": 0.15, "dir": "lower"},
    "refusal_correctness":    {"target": 0.90, "dir": "higher"},
    # agent
    "tool_selection_accuracy": {"target": 0.90, "dir": "higher"},
    "parameter_accuracy":      {"target": 0.85, "dir": "higher"},
    "unnecessary_call_rate":   {"target": 0.10, "dir": "lower"},
    "task_completion_rate":    {"target": 0.85, "dir": "higher"},
    "error_recovery_rate":     {"target": 0.70, "dir": "higher"},
    "agent_stability":         {"target": 0.95, "dir": "higher"},
    "consecutive_success_rate": {"target": 0.80, "dir": "higher"},
    # engineering
    "json_validity_rate":     {"target": 0.98, "dir": "higher"},
    "schema_pass_rate":       {"target": 0.95, "dir": "higher"},
    "enum_accuracy":          {"target": 0.95, "dir": "higher"},
    "retry_rate":             {"target": 0.20, "dir": "lower"},
    "hallucination_rate":     {"target": 0.10, "dir": "lower"},
    "format_following_rate":  {"target": 0.95, "dir": "higher"},
}


def meets_target(name: str, value: float) -> Optional[bool]:
    """按方向判定是否达标；无阈值定义返回 None。"""
    t = TARGETS.get(name)
    if not t or not isinstance(value, (int, float)):
        return None
    if t["dir"] == "higher":
        return value >= t["target"]
    return value <= t["target"]


# ─────────────────────────────────────────────────────────────────────
# 持久化
# ─────────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    layer          TEXT NOT NULL,
    num_cases      INTEGER NOT NULL,
    prompt_version TEXT,
    model_id       TEXT,
    dataset_version TEXT,
    input_hash     TEXT,
    git_commit     TEXT,
    metrics_json   TEXT NOT NULL,
    failing_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_case_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL,
    case_id    TEXT NOT NULL,
    layer      TEXT NOT NULL,
    category   TEXT,
    difficulty TEXT,
    detail_json TEXT NOT NULL
);
"""


class _ClosingConnection:
    """Context-managed SQLite connection that closes after transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self):
        self._conn.__enter__()
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._conn.__exit__(exc_type, exc, tb)
        finally:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)
class ResultStore:
    """SQLite 结果存储（含归因字段，供回归对比 / 归因）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return _ClosingConnection(conn)

    def save_run(self, layer: str, num_cases: int, metrics: Dict[str, Any],
                 failing: List[Dict[str, Any]], case_details: List[Dict[str, Any]],
                 *, prompt_version: str = "", model_id: str = "",
                 dataset_version: str = "", input_hash: str = "",
                 git_commit: str = "") -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO eval_runs (ts, layer, num_cases, prompt_version,"
                " model_id, dataset_version, input_hash, git_commit, metrics_json,"
                " failing_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), layer, num_cases, prompt_version, model_id,
                 dataset_version, input_hash, git_commit,
                 json.dumps(metrics, ensure_ascii=False),
                 json.dumps(failing, ensure_ascii=False)))
            run_id = cur.lastrowid
            for d in case_details:
                c.execute(
                    "INSERT INTO eval_case_results (run_id, case_id, layer,"
                    " category, difficulty, detail_json) VALUES (?,?,?,?,?,?)",
                    (run_id, d.get("id"), layer, d.get("category"),
                     d.get("difficulty"), json.dumps(d, ensure_ascii=False)))
            return run_id

    def latest_metrics(self, layer: str) -> Optional[Dict[str, Any]]:
        """取某层最近一次 run 的指标（作为基线对比）。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT metrics_json FROM eval_runs WHERE layer=? "
                "ORDER BY run_id DESC LIMIT 1", (layer,)).fetchone()
        return json.loads(row["metrics_json"]) if row else None


def dataset_hash(cases: List[EvalCase]) -> str:
    """对用例集合做稳定 hash → input_hash 归因字段。"""
    blob = json.dumps([asdict(c) for c in cases], sort_keys=True,
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────
# EvalRunner
# ─────────────────────────────────────────────────────────────────────
LAYERS = ("retrieval", "generation", "agent", "engineering")


class EvalRunner:
    """跑一批 case，按层聚合指标，出报告并可落库。

    judge_fn(system, user) -> str：LLM judge（可选）。
    embed_fn(text) -> list[float]：embedding（可选）。
    top_k：检索层默认评测截断。
    """

    def __init__(self, agent_fn: Callable[[EvalCase], Dict[str, Any]],
                 judge_fn: Optional[Callable[[str, str], str]] = None,
                 embed_fn: Optional[Callable[[str], List[float]]] = None,
                 *, store: Optional[ResultStore] = None, top_k: int = 3,
                 use_judge: bool = False):
        self.agent_fn = agent_fn
        self.judge_fn = judge_fn
        self.embed_fn = embed_fn
        self.store = store
        self.top_k = top_k
        self.use_judge = use_judge and judge_fn is not None

    # ── judge 包装（句子级 bool / 点分数），带位置偏差治理的 pairwise 在 metrics ──
    def _sentence_judge(self):
        if not self.use_judge:
            return None
        jf = self.judge_fn

        def _bool_judge(sentence: str, context: str) -> bool:
            sys = "你是严格的事实核查员。只回答 YES 或 NO。"
            user = f"上下文：\n{context}\n\n断言：{sentence}\n\n该断言是否被上下文支撑？"
            raw = (jf(sys, user) or "").strip().upper()
            return raw.startswith("Y")
        return _bool_judge

    def _relevance_judge(self):
        if not self.use_judge:
            return None
        jf = self.judge_fn

        def _score(query: str, answer: str) -> float:
            return M.pointwise_judge(jf, "你给答案与问题的相关性打 0-5 分。",
                                     query, answer)
        return _score

    # ── 单 case 各层评测 ──
    def _eval_retrieval(self, case: EvalCase, out: Dict[str, Any]) -> Dict[str, Any]:
        retrieved = out.get("retrieved", [])
        relevant = case.golden_context_ids
        contexts = out.get("contexts", [])
        k = self.top_k
        ctx_recall_judge = None  # 检索层默认不接 judge，保持确定
        return {
            "recall_at_k": M.recall_at_k(retrieved, relevant, k),
            "hit_rate_at_k": M.hit_rate_at_k(retrieved, relevant, k),
            "mrr": M.mrr(retrieved, relevant),
            "precision_at_k": M.precision_at_k(retrieved, relevant, k),
            "context_precision": M.context_precision(retrieved, relevant),
            "context_recall": M.context_recall(
                case.reference_points or case.key_points, contexts,
                judge_fn=ctx_recall_judge),
        }

    def _eval_generation(self, case: EvalCase, out: Dict[str, Any]) -> Dict[str, Any]:
        answer = out.get("answer", "")
        contexts = out.get("contexts", [])
        sj = self._sentence_judge()
        rj = self._relevance_judge()
        res: Dict[str, Any] = {"should_refuse": case.should_refuse}
        if case.should_refuse:
            # 拒答题按拒答正确性衡量，不参与质量指标聚合
            res["refusal_correct"] = M.is_refusal(answer)
            return res
        res.update({
            "faithfulness": M.faithfulness(answer, contexts, judge_fn=sj),
            "answer_relevance": M.answer_relevance(
                case.query, answer, judge_fn=rj, embed_fn=self.embed_fn),
            "completeness": M.completeness(answer, case.key_points),
            "context_usage": M.context_usage(answer, contexts),
        })
        if out.get("noisy_answer") is not None:
            res["noise_sensitivity"] = M.noise_sensitivity(
                answer, out.get("noisy_answer", ""), case.key_points)
        return res

    def _eval_agent(self, case: EvalCase, out: Dict[str, Any]) -> Dict[str, Any]:
        actual = out.get("trajectory", [])
        expected = case.trajectory
        return {
            "tool_selection_accuracy": M.tool_selection_accuracy(actual, expected),
            "parameter_accuracy": M.parameter_accuracy(actual, expected),
            "unnecessary_call_rate": M.unnecessary_call_rate(actual, expected),
            "success": bool(out.get("success", False)),
            "turns": int(out.get("turns", 0)),
            "num_tool_calls": len(actual),
            "had_failure": any(s.get("ok") is False for s in actual),
            "run_successes": list(out.get("run_successes", [out.get("success", False)])),
        }

    def _eval_engineering(self, case: EvalCase, out: Dict[str, Any]) -> Dict[str, Any]:
        raw = out.get("output_raw", "")
        parsed = out.get("parsed")
        if parsed is None:
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
        return {
            "output_raw": raw,
            "parsed": parsed,
            "schema_required": case.schema_required,
            "enum_field": case.enum_field,
            "enum_valid": case.enum_valid,
            "enum_value": out.get("enum_value",
                                  (parsed or {}).get(case.enum_field) if case.enum_field else None),
            "format_pattern": case.format_pattern,
            "answer": out.get("answer", ""),
            "contexts": out.get("contexts", []),
            "retries": int(out.get("retries", 0)),
            "ttft_ms": float(out.get("ttft_ms", 0.0)),
            "latency_ms": float(out.get("latency_ms", 0.0)),
            "input_tokens": int(out.get("input_tokens", 0)),
            "output_tokens": int(out.get("output_tokens", 0)),
            "should_refuse": case.should_refuse,
        }

    # ── 层聚合 ──
    def _aggregate(self, layer: str, per_case: List[Dict[str, Any]]) -> Dict[str, Any]:
        if layer == "retrieval":
            keys = ["recall_at_k", "hit_rate_at_k", "mrr", "precision_at_k",
                    "context_precision", "context_recall"]
            return {k: round(M.mean([c[k] for c in per_case]), 4) for k in keys}
        if layer == "generation":
            quality = [c for c in per_case if not c.get("should_refuse")]
            refuse = [c for c in per_case if c.get("should_refuse")]
            keys = ["faithfulness", "answer_relevance", "completeness", "context_usage"]
            agg = {k: round(M.mean([c[k] for c in quality if k in c]), 4)
                   for k in keys} if quality else {}
            ns = [c["noise_sensitivity"] for c in quality if "noise_sensitivity" in c]
            if ns:
                agg["noise_sensitivity"] = round(M.mean(ns), 4)
            if refuse:
                agg["refusal_correctness"] = round(
                    M.mean([1.0 if c.get("refusal_correct") else 0.0 for c in refuse]), 4)
            return agg
        if layer == "agent":
            records = [{"trajectory": [], "success": c["success"],
                        "turns": c["turns"]} for c in per_case]
            # 复原 trajectory 供 error_recovery / avg_tool_calls
            for rec, c in zip(records, per_case):
                rec["trajectory"] = [{"ok": not c["had_failure"]}] * c["num_tool_calls"] \
                    if c["num_tool_calls"] else []
                if c["had_failure"] and rec["trajectory"]:
                    rec["trajectory"][0] = {"ok": False}
            return {
                "tool_selection_accuracy": round(M.mean([c["tool_selection_accuracy"] for c in per_case]), 4),
                "parameter_accuracy": round(M.mean([c["parameter_accuracy"] for c in per_case]), 4),
                "unnecessary_call_rate": round(M.mean([c["unnecessary_call_rate"] for c in per_case]), 4),
                "task_completion_rate": round(M.task_completion_rate([c["success"] for c in per_case]), 4),
                "error_recovery_rate": round(M.error_recovery_rate(records), 4),
                "avg_turns": round(M.mean([c["turns"] for c in per_case]), 4),
                "avg_tool_calls": round(M.mean([c["num_tool_calls"] for c in per_case]), 4),
                "agent_stability": round(M.mean([M.agent_stability(c["run_successes"]) for c in per_case]), 4),
                "consecutive_success_rate": round(M.mean([M.consecutive_success_rate(c["run_successes"]) for c in per_case]), 4),
            }
        if layer == "engineering":
            outputs = [c["output_raw"] for c in per_case]
            # schema：每条 case 用它自己的 required（不同 case schema 不同）
            schema_cases = [c for c in per_case if c["schema_required"]]
            schema_pass = [
                1.0 if M.schema_pass_rate([c["parsed"]] if c["parsed"] is not None
                                          else [c["output_raw"]], c["schema_required"]) == 1.0
                else 0.0
                for c in schema_cases]
            # enum：每条用自己的 enum_valid
            enum_cases = [c for c in per_case
                          if c["enum_field"] and c["enum_valid"] and c["enum_value"] is not None]
            enum_hit = [1.0 if M.enum_accuracy([c["enum_value"]], c["enum_valid"]) == 1.0
                        else 0.0 for c in enum_cases]
            fmt_cases = [c for c in per_case if c["format_pattern"]]
            # 幻觉：只在“有答案且有上下文、且非拒答题”上算
            hallu_recs = [{"answer": c["answer"], "contexts": c["contexts"]}
                          for c in per_case
                          if c["answer"] and c["contexts"] and not c["should_refuse"]]
            agg = {
                "json_validity_rate": round(M.json_validity_rate(outputs), 4),
                "schema_pass_rate": round(M.mean(schema_pass), 4) if schema_pass else 1.0,
                "enum_accuracy": round(M.mean(enum_hit), 4) if enum_hit else 1.0,
                "retry_rate": round(M.retry_rate(per_case), 4),
                "refusal_rate": round(M.refusal_rate([c["answer"] for c in per_case]), 4),
                "hallucination_rate": round(M.hallucination_rate(hallu_recs), 4) if hallu_recs else 0.0,
                "latency_ttft_ms": M.latency_stats([c["ttft_ms"] for c in per_case]),
                "latency_e2e_ms": M.latency_stats([c["latency_ms"] for c in per_case]),
                "tokens": M.token_stats([c["input_tokens"] for c in per_case],
                                        [c["output_tokens"] for c in per_case]),
            }
            if fmt_cases:
                # 每个 format_pattern 分组求遵循率，再平均
                from collections import defaultdict
                groups: Dict[str, List[str]] = defaultdict(list)
                for c in fmt_cases:
                    groups[c["format_pattern"]].append(c["output_raw"] or c["answer"])
                rates = [M.format_following_rate(v, k) for k, v in groups.items()]
                agg["format_following_rate"] = round(M.mean(rates), 4)
            return agg
        return {}

    _EVAL_FN = {
        "retrieval": "_eval_retrieval",
        "generation": "_eval_generation",
        "agent": "_eval_agent",
        "engineering": "_eval_engineering",
    }

    def run_layer(self, cases: List[EvalCase], layer: str) -> Dict[str, Any]:
        """跑单层：筛选该层 case → 逐 case 评 → 聚合 → 标不达标项。"""
        subset = [c for c in cases if c.layer == layer]
        if not subset:
            return {"layer": layer, "num_cases": 0, "metrics": {},
                    "failing": [], "case_details": []}
        eval_fn = getattr(self, self._EVAL_FN[layer])
        per_case: List[Dict[str, Any]] = []
        details: List[Dict[str, Any]] = []
        for case in subset:
            out = self.agent_fn(case)
            scored = eval_fn(case, out)
            per_case.append(scored)
            details.append({"id": case.id, "category": case.category,
                            "difficulty": case.difficulty, "scores": _slim(scored)})
        agg = self._aggregate(layer, per_case)
        failing = []
        for name, val in agg.items():
            ok = meets_target(name, val) if isinstance(val, (int, float)) else None
            if ok is False:
                t = TARGETS[name]
                failing.append({"metric": name, "value": val,
                                "target": t["target"], "dir": t["dir"]})
        return {"layer": layer, "num_cases": len(subset), "metrics": agg,
                "failing": failing, "case_details": details}

    def run_all(self, cases: List[EvalCase],
                layers: Optional[List[str]] = None) -> Dict[str, Any]:
        """跑多层，返回 {layer: 报告}。"""
        layers = layers or list(LAYERS)
        return {ly: self.run_layer(cases, ly) for ly in layers}

    def run_and_persist(self, cases: List[EvalCase], layer: str, *,
                        prompt_version: str = "", model_id: str = "",
                        dataset_version: str = "", git_commit: str = "",
                        compare_baseline: bool = True) -> Dict[str, Any]:
        """跑单层 + 落库 + 可选与上一次基线对比。"""
        report = self.run_layer(cases, layer)
        baseline = None
        if compare_baseline and self.store is not None:
            baseline = self.store.latest_metrics(layer)
        if self.store is not None and report["num_cases"] > 0:
            run_id = self.store.save_run(
                layer, report["num_cases"], report["metrics"], report["failing"],
                report["case_details"], prompt_version=prompt_version,
                model_id=model_id, dataset_version=dataset_version,
                input_hash=dataset_hash([c for c in cases if c.layer == layer]),
                git_commit=git_commit)
            report["run_id"] = run_id
        if baseline:
            report["baseline_delta"] = _delta(baseline, report["metrics"])
        return report


def _slim(scored: Dict[str, Any]) -> Dict[str, Any]:
    """case 明细瘦身：去掉大文本字段，只留分数类。"""
    drop = {"output_raw", "parsed", "contexts", "answer", "schema_required",
            "enum_valid", "run_successes"}
    return {k: v for k, v in scored.items() if k not in drop}


def _delta(base: Dict[str, Any], cur: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in cur.items():
        if isinstance(v, (int, float)) and isinstance(base.get(k), (int, float)):
            out[k] = round(v - base[k], 4)
    return out


# ─────────────────────────────────────────────────────────────────────
# Mock Agent —— --mock / 单测用；从 case 派生确定性输出（无 LLM）
# ─────────────────────────────────────────────────────────────────────

def build_mock_agent_fn(*, degrade: bool = False) -> Callable[[EvalCase], Dict[str, Any]]:
    """返回一个纯规则 mock agent_fn。

    degrade=False：产出与 golden 高度一致的“理想”输出（多数指标达标）。
    degrade=True ：故意制造检索缺漏 / 漏答 / 多余工具调用，用于验证不达标路径。
    """
    def agent_fn(case: EvalCase) -> Dict[str, Any]:
        # 检索：理想时返回全部 golden id；degrade 时打乱且丢一个
        retrieved = list(case.golden_context_ids)
        if degrade and len(retrieved) > 1:
            retrieved = retrieved[1:] + ["noise-doc"]
        # 上下文既覆盖要点、也包含 golden_answer 文本，模拟“理想 agent 检索到支撑证据”
        ctx_body = " ".join(case.key_points or case.reference_points or [case.query])
        if case.golden_answer:
            ctx_body = case.golden_answer + " " + ctx_body
        contexts = [f"[{cid}] {ctx_body}"
                    for cid in (case.golden_context_ids or ["ctx"])]

        # 生成：拒答题产出拒答；否则用 golden_answer 或 key_points 拼答案
        if case.should_refuse:
            answer = "抱歉，这个问题不属于我能协助的范围，请回到产品相关咨询。"
        elif case.golden_answer:
            answer = case.golden_answer
        else:
            answer = "。".join(case.key_points) + "。" if case.key_points else case.query
        if degrade and not case.should_refuse and case.key_points:
            answer = case.key_points[0] + "。"   # 只答一个要点 → completeness 掉

        noisy_answer = None
        if case.extra.get("noise_probe"):
            noisy_answer = (case.key_points[0] + "。") if (degrade and case.key_points) else answer

        # Agent 轨迹：理想时照抄期望；degrade 时加一个多余调用 + 首步失败
        traj = [dict(step, ok=True) for step in case.trajectory]
        success = True
        if degrade and traj:
            traj = traj + [{"tool": "extra_tool", "args": {}, "ok": True}]
            traj[0] = dict(traj[0], ok=False)
            success = case.extra.get("recover", True)

        # 工程：产出满足 schema 的 JSON
        obj: Dict[str, Any] = {}
        for k in case.schema_required:
            obj[k] = case.extra.get("field_values", {}).get(k, "ok")
        if case.enum_field:
            obj[case.enum_field] = (case.enum_valid[0] if case.enum_valid else "consult")
            if degrade and case.enum_valid:
                obj[case.enum_field] = "INVALID_ENUM"
        output_raw = json.dumps(obj, ensure_ascii=False) if obj else answer
        if degrade and case.layer == "engineering":
            output_raw = output_raw[:-1]   # 破坏 JSON

        run_successes = case.extra.get("run_successes", [success])

        return {
            "answer": answer,
            "noisy_answer": noisy_answer,
            "retrieved": retrieved,
            "contexts": contexts,
            "trajectory": traj,
            "success": success,
            "run_successes": run_successes,
            "turns": case.extra.get("turns", 1 + len(traj)),
            "retries": case.extra.get("retries", 1 if degrade else 0),
            "output_raw": output_raw,
            "parsed": obj if obj else None,
            "enum_value": obj.get(case.enum_field) if case.enum_field else None,
            "ttft_ms": case.extra.get("ttft_ms", 120.0),
            "latency_ms": case.extra.get("latency_ms", 850.0),
            "input_tokens": case.extra.get("input_tokens", 320),
            "output_tokens": case.extra.get("output_tokens", 90),
        }
    return agent_fn
