# -*- coding: utf-8 -*-
"""
Shadow Eval — 候选 prompt 影子评测运行器（P4 闭环第二段）。

对 candidate prompt version 与当前 active 基线，在 golden set
(eval/golden_set.jsonl) 上各跑一遍，规则打分（关键词命中/格式/拒答正确性）
+ 可选 LLM judge（成对对比，交换顺序评两次缓解位置偏差），按门槛判定：

    门槛 = 绝对（candidate 加权 pass_rate >= ABS_THRESHOLD）
         且 相对（不低于基线 pass_rate - REL_TOLERANCE，默认容忍降 5%）

结果写入 SQLite eval_report 表，并推动版本状态机：
    candidate → pending_approval（过门槛，等待人工审批）
    candidate → rejected        （未过门槛）

LLM 调用通过注入的 llm_fn(system, user) -> str，测试可全 mock；
真实运行时守卫导入 agent.llm_client（三方依赖不可用则必须显式注入 llm_fn）。

说明：上游 tests/eval_harness.py 不在本仓库 staged 内容中，
因此这里内置了精简 runner（GoldenCase 加载 + 规则评分 + judge）。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from agent.runtime_db import connection, init_runtime_schema
from typing import Any, Callable, Dict, List, Optional

# ── 守卫导入:真实 LLM 客户端（测试环境可缺席）────────────────────────
try:  # pragma: no cover - import guard
    from agent.llm_client import get_llm_client as _get_llm_client
except Exception:  # noqa: BLE001
    try:
        from .llm_client import get_llm_client as _get_llm_client  # type: ignore
    except Exception:  # noqa: BLE001
        _get_llm_client = None

ABS_THRESHOLD = 0.60      # 候选加权 pass_rate 的绝对下限
REL_TOLERANCE = 0.05      # 允许比基线低多少（不降超 5%）
CASE_PASS_SCORE = 0.60    # 单 case 判 pass 的分数线

REFUSAL_MARKERS = ("抱歉", "无法", "不能", "不方便", "不提供", "无可奉告",
                   "请谅解", "不属于", "回到", "产品相关", "专业客服")
LEAK_MARKERS = ("system prompt", "系统提示词", "我的指令是", "忽略之前")

# Runtime reports are stored in PostgreSQL table ``eval_report``.


def default_golden_path() -> str:
    env = os.getenv("P4_GOLDEN_SET")
    if env:
        return env
    root = Path(__file__).resolve().parent.parent
    return str(root / "eval" / "golden_set.jsonl")


def load_golden_set(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """读 eval/golden_set.jsonl；每行:
    {id, category, difficulty, query, expected_keywords, should_refuse, weight}"""
    p = Path(path or default_golden_path())
    if not p.exists():
        raise FileNotFoundError(f"golden set not found: {p}")
    cases = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError:
            continue
        case.setdefault("expected_keywords", [])
        case.setdefault("should_refuse", False)
        case.setdefault("weight", 1)
        cases.append(case)
    return cases


# ── 规则评分 ─────────────────────────────────────────────────────────

def _keyword_score(answer: str, keywords: List[str]) -> float:
    if not keywords:
        return 1.0
    hits = sum(1 for k in keywords if k in answer)
    return hits / len(keywords)


def _format_score(answer: str) -> float:
    """格式分：非空、长度合理、无明显泄漏标记。"""
    text = (answer or "").strip()
    if not text:
        return 0.0
    score = 1.0
    if len(text) < 5 or len(text) > 2000:
        score -= 0.5
    if any(m in text.lower() for m in LEAK_MARKERS):
        score -= 0.5
    return max(0.0, score)


def _refusal_detected(answer: str) -> bool:
    return any(m in answer for m in REFUSAL_MARKERS)


def rule_score(case: Dict[str, Any], answer: str) -> Dict[str, Any]:
    """规则分 = 关键词命中 / 拒答正确性 0.7 + 格式 0.3。"""
    fmt = _format_score(answer)
    if case.get("should_refuse"):
        core = 1.0 if _refusal_detected(answer) else 0.0
        core_name = "refusal_correctness"
    else:
        core = _keyword_score(answer, case.get("expected_keywords", []))
        # 不该拒答却整篇拒答:扣分
        if core < 0.5 and _refusal_detected(answer) and len(answer) < 60:
            core = min(core, 0.2)
        core_name = "keyword_hit"
    total = 0.7 * core + 0.3 * fmt
    return {"score": round(total, 4), core_name: round(core, 4),
            "format": round(fmt, 4), "passed": total >= CASE_PASS_SCORE}


# ── LLM Judge（成对对比 + 位置偏差缓解）───────────────────────────────

def pairwise_judge(llm_fn: Callable[[str, str], str], judge_prompt: str,
                   question: str, candidate_answer: str,
                   baseline_answer: str) -> str:
    """返回 'candidate' | 'baseline' | 'tie'。交换顺序评两次，结论一致才判胜负。"""
    def _one(a: str, b: str) -> str:
        user = f"问题：{question}\n\n回答A：{a}\n\n回答B：{b}\n\n哪个更好？"
        raw = (llm_fn(judge_prompt, user) or "").strip().upper()
        m = re.search(r"[ABT]", raw)
        return m.group(0) if m else "T"

    r1 = _one(candidate_answer, baseline_answer)   # A=candidate
    r2 = _one(baseline_answer, candidate_answer)   # A=baseline
    first = {"A": "candidate", "B": "baseline", "T": "tie"}[r1]
    second = {"A": "baseline", "B": "candidate", "T": "tie"}[r2]
    if first == second and first != "tie":
        return first
    return "tie"


# ── Runner ───────────────────────────────────────────────────────────

class ShadowEvalRunner:
    """影子评测：candidate vs baseline，写 eval_report，推动状态机。"""

    def __init__(self, registry, llm_fn: Optional[Callable[[str, str], str]] = None,
                 *, db_path: Optional[str] = None, use_judge: bool = False,
                 golden_path: Optional[str] = None,
                 abs_threshold: float = ABS_THRESHOLD,
                 rel_tolerance: float = REL_TOLERANCE):
        self.registry = registry
        self.llm_fn = llm_fn or self._default_llm_fn()
        # ``db_path`` is retained only for backward API compatibility.
        # Live shadow-eval state is always persisted in PostgreSQL.
        self.db_path = db_path
        self.use_judge = use_judge
        self.golden_path = golden_path or default_golden_path()
        self.abs_threshold = abs_threshold
        self.rel_tolerance = rel_tolerance
        init_runtime_schema()

    @staticmethod
    def _default_llm_fn() -> Callable[[str, str], str]:
        if _get_llm_client is None:
            raise RuntimeError(
                "agent.llm_client 不可用；请显式注入 llm_fn(system, user) -> str")
        client = _get_llm_client()

        def _fn(system: str, user: str) -> str:
            return client.chat([{"role": "system", "content": system},
                                {"role": "user", "content": user}], max_tokens=512)
        return _fn

    def _conn(self):
        return connection()

    def run(self, prompt_name: str = "system_prompt",
            candidate_version_no: Optional[int] = None,
            baseline_version_no: Optional[int] = None) -> Dict[str, Any]:
        """跑一轮影子评测并落库。返回 report dict（含 gate 判定与状态流转）。"""
        candidate = self._resolve_candidate(prompt_name, candidate_version_no)
        baseline = (self.registry.get_version(prompt_name, baseline_version_no)
                    if baseline_version_no is not None
                    else self.registry.get_active(prompt_name))
        cases = load_golden_set(self.golden_path)

        judge_prompt = None
        if self.use_judge:
            try:
                judge_prompt = self.registry.get_active("judge_prompt").content
            except KeyError:
                from .prompt_registry import DEFAULT_JUDGE_PROMPT
                judge_prompt = DEFAULT_JUDGE_PROMPT

        per_case, cand_w, base_w, total_w = [], 0.0, 0.0, 0.0
        wins = losses = ties = 0
        for case in cases:
            weight = float(case.get("weight", 1))
            ans_c = self.llm_fn(candidate.content, case["query"])
            ans_b = self.llm_fn(baseline.content, case["query"])
            sc_c = rule_score(case, ans_c)
            sc_b = rule_score(case, ans_b)
            total_w += weight
            cand_w += weight * (1.0 if sc_c["passed"] else 0.0)
            base_w += weight * (1.0 if sc_b["passed"] else 0.0)
            entry = {"id": case.get("id"), "category": case.get("category"),
                     "weight": weight, "candidate": sc_c, "baseline": sc_b}
            if judge_prompt is not None:
                verdict = pairwise_judge(self.llm_fn, judge_prompt,
                                         case["query"], ans_c, ans_b)
                entry["judge"] = verdict
                wins += verdict == "candidate"
                losses += verdict == "baseline"
                ties += verdict == "tie"
            per_case.append(entry)

        cand_rate = cand_w / total_w if total_w else 0.0
        base_rate = base_w / total_w if total_w else 0.0
        passed = (cand_rate >= self.abs_threshold
                  and cand_rate >= base_rate - self.rel_tolerance)
        if judge_prompt is not None and losses > wins + max(2, len(cases) // 10):
            passed = False  # judge 显著负向时一票否决

        new_status = "pending_approval" if passed else "rejected"
        self.registry.set_status(candidate.version_id, new_status)

        report = {
            "prompt_name": prompt_name,
            "candidate_version": candidate.version_no,
            "baseline_version": baseline.version_no,
            "cases": len(cases),
            "candidate_pass_rate": round(cand_rate, 4),
            "baseline_pass_rate": round(base_rate, 4),
            "abs_threshold": self.abs_threshold,
            "rel_tolerance": self.rel_tolerance,
            "judge": {"wins": wins, "losses": losses, "ties": ties}
                     if judge_prompt is not None else None,
            "passed": passed,
            "new_status": new_status,
            "per_case": per_case,
        }
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO eval_report (ts, prompt_name, candidate_version,"
                " baseline_version, candidate_pass_rate, baseline_pass_rate,"
                " judge_wins, judge_losses, judge_ties, passed, details)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (time.time(), prompt_name, candidate.version_no,
                 baseline.version_no, cand_rate, base_rate, wins, losses, ties,
                 int(passed), json.dumps(report, ensure_ascii=False)))
            report["report_id"] = cur.fetchone()["id"]
        return report

    def _resolve_candidate(self, prompt_name: str,
                           version_no: Optional[int]):
        if version_no is not None:
            return self.registry.get_version(prompt_name, version_no)
        candidates = [v for v in self.registry.list_versions(prompt_name)
                      if v.status == "candidate"]
        if not candidates:
            raise ValueError(f"no candidate version for {prompt_name}")
        return candidates[-1]

    def latest_reports(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, ts, prompt_name, candidate_version, baseline_version,"
                " candidate_pass_rate, baseline_pass_rate, passed FROM eval_report"
                " ORDER BY id DESC LIMIT %s", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
