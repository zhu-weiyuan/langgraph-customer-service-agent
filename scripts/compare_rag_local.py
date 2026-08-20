#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/compare_rag_local.py — 容器内【真实检索】的普通 RAG vs Agentic RAG 对比

与 scripts/compare_rag.py 的区别:检索/改写不走 LLM/embedding,而是用
scripts/_local_retriever.py 的纯 Python BM25 + 规则改写,在真实知识库
(knowledge/*.md,~264 段)上跑出可信数字。分层评测集见
eval/rag_eval_layered.jsonl(normal / edge / adversarial / high_weight)。

    普通 RAG (plain)   : 单次 BM25 检索 top_k
    Agentic RAG        : rewrite 多变体 → 多路 BM25 召回合并去重 →
                         命中门控(≥min_hits 停)→ 不足再改写 1 轮

指标(逐条判相关性后按 tier 分组 + 总体):
    HitRate@K  Recall@K  MRR  Precision@K
    relevance = 命中片段 source ∈ golden_context_ids
    加权:high_weight case weight=3 计入加权总分;成本 = 改写/检索次数

用法:
    python3 scripts/compare_rag_local.py
    python3 scripts/compare_rag_local.py --k 5 --rounds 2 --min-hits 3
    python3 scripts/compare_rag_local.py --tier high_weight
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence

_ROOT = Path(__file__).resolve().parent.parent
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))

from scripts._local_retriever import LocalRetriever, rewrite  # noqa: E402

_TIERS = ("normal", "edge", "adversarial", "high_weight")


# ── 指标(float 判定,纯函数)───────────────────────────────────────
def hit_rate_at_k(rel: Sequence[bool], k: int) -> float:
    return 1.0 if any(rel[:k]) else 0.0


def precision_at_k(rel: Sequence[bool], k: int) -> float:
    topk = rel[:k]
    return (sum(1 for r in topk if r) / len(topk)) if topk else 0.0


def recall_at_k(rel: Sequence[bool], k: int, total_relevant: int) -> float:
    if total_relevant <= 0:
        return 0.0
    hit = sum(1 for r in rel[:k] if r)
    return min(1.0, hit / total_relevant)


def mrr(rel: Sequence[bool]) -> float:
    for i, r in enumerate(rel, start=1):
        if r:
            return 1.0 / i
    return 0.0


# ── 相关性判定:source ∈ golden_context_ids ────────────────────────
def rel_by_source(hit: Dict[str, Any], golden_ids: Sequence[str]) -> bool:
    src = str(hit.get("source", "")).lower().replace(".md", "")
    for gid in golden_ids:
        if src == str(gid).lower().replace(".md", ""):
            return True
    return False


# ── 两条管线 ───────────────────────────────────────────────────────
class Counters:
    __slots__ = ("rewrites", "retrievals")

    def __init__(self) -> None:
        self.rewrites = 0
        self.retrievals = 0


def run_plain(query: str, retriever: LocalRetriever, k: int,
              c: Counters) -> List[Dict[str, Any]]:
    c.retrievals += 1
    return retriever.retrieve(query, top_k=k)[:k]


def run_agentic(query: str, retriever: LocalRetriever, k: int, max_rounds: int,
                min_hits: int, c: Counters) -> List[Dict[str, Any]]:
    """改写多变体 → 多路合并去重 → 命中门控 → 不足再改写 1 轮。"""
    current = query
    best: List[Dict[str, Any]] = []
    for rnd in range(1, max_rounds + 1):
        c.rewrites += 1
        queries = rewrite(current) or [current]
        merged: List[Dict[str, Any]] = []
        seen = set()
        for q in queries:
            c.retrievals += 1
            for h in retriever.retrieve(q, top_k=k):
                key = (h.get("title"), h.get("source"))
                if key not in seen:
                    seen.add(key)
                    merged.append(h)
        merged.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        merged = merged[:k]
        best = merged or best
        if not merged:
            break
        # 命中门控:合并后达 min_hits 或已到最后一轮 → 停
        if len(merged) >= min_hits or rnd >= max_rounds:
            break
        # 不足 → 换一个更"提纯"的查询再来一轮(关键词串)
        variants = rewrite(query)
        current = variants[-1] if len(variants) > 1 else query
    return best


# ── 评测 ───────────────────────────────────────────────────────────
def evaluate(dataset: List[Dict[str, Any]], retriever: LocalRetriever,
             k: int, rounds: int, min_hits: int) -> Dict[str, Any]:
    metric_names = ("HitRate", "Recall", "MRR", "Precision")
    # 分层累加:tier -> metric -> {plain, agentic}
    tier_sum = {t: {m: {"plain": 0.0, "agentic": 0.0} for m in metric_names}
                for t in _TIERS}
    tier_n = defaultdict(int)
    # 加权累加(weight 计入):HitRate 与 MRR
    w_sum = {"plain": 0.0, "agentic": 0.0}
    w_total = 0.0
    cost = {"plain": Counters(), "agentic": Counters()}
    per_case = []

    for case in dataset:
        q = case["query"]
        tier = case.get("tier", "normal")
        weight = float(case.get("weight", 1))
        golden = case.get("golden_context_ids") or []
        total_rel = max(1, len(golden))

        plain_hits = run_plain(q, retriever, k, cost["plain"])
        ag_hits = run_agentic(q, retriever, k, rounds, min_hits, cost["agentic"])

        pf = [rel_by_source(h, golden) for h in plain_hits]
        af = [rel_by_source(h, golden) for h in ag_hits]

        vals = {}
        for name, flags in (("plain", pf), ("agentic", af)):
            vals[(name, "HitRate")] = hit_rate_at_k(flags, k)
            vals[(name, "Recall")] = recall_at_k(flags, k, total_rel)
            vals[(name, "MRR")] = mrr(flags)
            vals[(name, "Precision")] = precision_at_k(flags, k)

        for name in ("plain", "agentic"):
            for m in metric_names:
                tier_sum[tier][m][name] += vals[(name, m)]
        tier_n[tier] += 1

        # 加权得分(以 HitRate 为主要业务命中信号)
        w_sum["plain"] += vals[("plain", "HitRate")] * weight
        w_sum["agentic"] += vals[("agentic", "HitRate")] * weight
        w_total += weight

        per_case.append({
            "id": case.get("id"), "tier": tier, "weight": weight, "query": q,
            "golden": golden,
            "plain_sources": [h["source"] for h in plain_hits],
            "agentic_sources": [h["source"] for h in ag_hits],
            "plain_hit": vals[("plain", "HitRate")],
            "agentic_hit": vals[("agentic", "HitRate")],
            "plain_mrr": vals[("plain", "MRR")],
            "agentic_mrr": vals[("agentic", "MRR")],
        })

    # 求均值
    tier_metrics = {}
    for t in _TIERS:
        n = tier_n[t]
        tier_metrics[t] = {
            m: {name: (tier_sum[t][m][name] / n if n else 0.0)
                for name in ("plain", "agentic")}
            for m in metric_names
        }
    # 总体 = 各 case 平均(对 case 数加权)
    overall = {m: {"plain": 0.0, "agentic": 0.0} for m in metric_names}
    total_n = sum(tier_n.values())
    for m in metric_names:
        for name in ("plain", "agentic"):
            overall[m][name] = sum(
                tier_sum[t][m][name] for t in _TIERS) / total_n if total_n else 0.0

    return {
        "k": k, "n": total_n, "tier_n": dict(tier_n),
        "tier_metrics": tier_metrics, "overall": overall,
        "weighted": {"plain": w_sum["plain"] / w_total if w_total else 0.0,
                     "agentic": w_sum["agentic"] / w_total if w_total else 0.0,
                     "w_total": w_total},
        "cost": cost, "per_case": per_case,
    }


# ── 输出 ───────────────────────────────────────────────────────────
def _arrow(delta: float) -> str:
    return "UP" if delta > 1e-9 else ("DOWN" if delta < -1e-9 else "==")


def _fmt(plain: float, agentic: float) -> str:
    d = agentic - plain
    return f"{plain:6.3f} | {agentic:6.3f} | {d:+.3f} {_arrow(d)}"


def report(res: Dict[str, Any], k: int) -> None:
    print("=" * 78)
    print(f"真实检索对比(BM25 本地检索器 · 知识库真实段落) · Top-{k} · N={res['n']}")
    print("=" * 78)
    metric_names = ("HitRate", "Recall", "MRR", "Precision")

    for t in _TIERS:
        n = res["tier_n"].get(t, 0)
        print(f"\n[{t}]  ({n} 条)   指标: plain | agentic |  Δ")
        print("-" * 70)
        tm = res["tier_metrics"][t]
        for m in metric_names:
            label = f"{m}@{k}" if m in ("HitRate", "Recall", "Precision") else m
            print(f"  {label:<12} {_fmt(tm[m]['plain'], tm[m]['agentic'])}")

    print("\n" + "=" * 78)
    print("总体(全部 case 平均):   plain | agentic |  Δ")
    print("-" * 70)
    ov = res["overall"]
    for m in metric_names:
        label = f"{m}@{k}" if m in ("HitRate", "Recall", "Precision") else m
        print(f"  {label:<12} {_fmt(ov[m]['plain'], ov[m]['agentic'])}")

    w = res["weighted"]
    print(f"\n加权 HitRate(high_weight×3, 权重总和={w['w_total']:.0f}):"
          f"  plain={w['plain']:.3f} | agentic={w['agentic']:.3f} | "
          f"Δ={w['agentic'] - w['plain']:+.3f} {_arrow(w['agentic'] - w['plain'])}")

    cp, ca = res["cost"]["plain"], res["cost"]["agentic"]
    n = res["n"]
    print("\n成本(越低越省):")
    print(f"  平均改写次数    plain={cp.rewrites / n:5.2f} | agentic={ca.rewrites / n:5.2f}")
    print(f"  平均检索次数    plain={cp.retrievals / n:5.2f} | agentic={ca.retrievals / n:5.2f}")

    # 结论
    print("\n" + "=" * 78)
    print("分层结论:")
    for t in _TIERS:
        tm = res["tier_metrics"][t]
        dh = tm["HitRate"]["agentic"] - tm["HitRate"]["plain"]
        dm = tm["MRR"]["agentic"] - tm["MRR"]["plain"]
        if dh > 1e-9 or dm > 1e-9:
            verdict = f"Agentic 提升 HitRate {dh:+.3f} / MRR {dm:+.3f} → 有效"
        elif dh < -1e-9 or dm < -1e-9:
            verdict = f"Agentic 反而下降 HitRate {dh:+.3f} / MRR {dm:+.3f} → 需收敛门控"
        else:
            verdict = "两者持平 → 改写为过度设计(plain 已够)"
        print(f"  {t:<12}: {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser(description="容器内真实 RAG 对比(BM25)")
    ap.add_argument("--dataset", default="eval/rag_eval_layered.jsonl")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--min-hits", type=int, default=3)
    ap.add_argument("--tier", default="", help="只评某一 tier")
    ap.add_argument("--csv", default="")
    args = ap.parse_args()

    ds_path = Path(args.dataset)
    if not ds_path.exists():
        print(f"[错误] 找不到评测集: {ds_path}", file=sys.stderr)
        return 1
    rows = [json.loads(l) for l in ds_path.open(encoding="utf-8") if l.strip()]
    rows = [r for r in rows if r.get("query") and r.get("golden_context_ids")]
    if args.tier:
        rows = [r for r in rows if r.get("tier") == args.tier]
    if not rows:
        print("[错误] 评测集为空", file=sys.stderr)
        return 1

    print(f"加载知识库并构建 BM25 索引 ...")
    t0 = time.perf_counter()
    retriever = LocalRetriever()
    print(f"语料段数={len(retriever.corpus)} · 评测集={len(rows)} 条 · "
          f"门控 min_hits={args.min_hits} · 最多 {args.rounds} 轮")

    res = evaluate(rows, retriever, args.k, args.rounds, args.min_hits)
    dt = time.perf_counter() - t0
    report(res, args.k)
    print(f"\n耗时 {dt:.2f}s · 每条约 {dt / res['n'] * 1000:.0f}ms")

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(res["per_case"][0].keys()))
            w.writeheader()
            for r in res["per_case"]:
                w.writerow({kk: (json.dumps(vv, ensure_ascii=False)
                                 if isinstance(vv, list) else vv)
                            for kk, vv in r.items()})
        print(f"逐条结果已导出: {args.csv}")

    ov = res["overall"]
    gain = ov["HitRate"]["agentic"] - ov["HitRate"]["plain"]
    return 0 if gain >= -1e-9 else 2


if __name__ == "__main__":
    raise SystemExit(main())
