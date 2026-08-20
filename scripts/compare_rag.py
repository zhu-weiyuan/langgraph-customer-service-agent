#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/compare_rag.py — 普通 RAG vs Agentic RAG 一键对比评测

同一批 query、同一知识库,分别跑两条检索管线并列出指标,量化
"改写+多轮重检+门控"到底带来多少召回提升、又多花多少 LLM 调用。

    普通 RAG (plain)   : 单次检索,无 Query Rewrite、无多轮、无 LLM 评估
    Agentic RAG        : 改写(可多变体) → 多路召回合并 → 启发式/LLM 门控 →
                         不足则再改写重检(最多 N 轮),逻辑镜像 agent/agentic_rag.py

指标(逐条判定相关性后聚合):
    HitRate@K   Top-K 是否至少命中 1 条相关文档(命中率)
    Recall@K    相关文档被 Top-K 覆盖的比例
    MRR         第一条相关结果的排名倒数
    Precision@K Top-K 里相关文档占比
    另报 平均 LLM 调用数 / 平均检索次数(成本侧),体现质量-成本权衡

相关性判定两种(自动按数据集字段选择):
    by-source : 检索命中的 source 文件 ∈ golden_context_ids(golden_set.jsonl)
    by-keyword: 命中片段 title+正文 含 expected_doc_keywords(retrieval_eval.jsonl)

用法(Windows cmd):
    :: 容器/无依赖:纯 mock 跑通链路,验证脚本本身
    python scripts\\compare_rag.py --mock

    :: 真实后端(需 .env 配好 LLM/embedding;RAG_BACKEND 决定 tfidf|hybrid|pgvector)
    python scripts\\compare_rag.py --dataset eval\\golden_set.jsonl --k 5
    python scripts\\compare_rag.py --dataset eval\\retrieval_eval.jsonl --k 5 --rounds 2

    :: 只看某类别 / 导出 CSV
    python scripts\\compare_rag.py --category 精确码
    python scripts\\compare_rag.py --csv compare_result.csv

退出码:0 正常;2 Agentic 在关键指标上不优于 plain(可接 CI 观测)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 让 `python scripts\compare_rag.py` 从任意目录运行都能找到 agent 包
_ROOT = Path(__file__).resolve().parent.parent
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))

# .env 加载(守卫)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ════════════════════════════════════════════════════════════════════
# 指标(纯函数,手算可验证)
# ════════════════════════════════════════════════════════════════════
def hit_rate_at_k(rel_flags: Sequence[bool], k: int) -> float:
    """Top-K 是否至少命中 1 条相关 → 1.0 / 0.0。"""
    return 1.0 if any(rel_flags[:k]) else 0.0


def precision_at_k(rel_flags: Sequence[bool], k: int) -> float:
    topk = rel_flags[:k]
    return (sum(1 for r in topk if r) / len(topk)) if topk else 0.0


def recall_at_k(rel_flags: Sequence[bool], k: int, total_relevant: int) -> float:
    if total_relevant <= 0:
        return 0.0
    hit = sum(1 for r in rel_flags[:k] if r)
    return min(1.0, hit / total_relevant)


def mrr(rel_flags: Sequence[bool]) -> float:
    for i, r in enumerate(rel_flags, start=1):
        if r:
            return 1.0 / i
    return 0.0


# ════════════════════════════════════════════════════════════════════
# 相关性判定
# ════════════════════════════════════════════════════════════════════
def _rel_by_source(hit: Dict[str, Any], golden_ids: Sequence[str]) -> bool:
    src = str(hit.get("source", "")).lower()
    # source 可能是 "product-manual.md" / "product-manual";做前缀/包含匹配
    for gid in golden_ids:
        g = str(gid).lower().replace(".md", "")
        if g and (g in src or src.replace(".md", "") == g):
            return True
    return False


def _rel_by_keyword(hit: Dict[str, Any], keywords: Sequence[str]) -> bool:
    blob = (str(hit.get("title", "")) + " " +
            str(hit.get("text", "") or hit.get("content", ""))).lower()
    # 全部关键词命中才算相关(严格);可按需改 any
    return all(str(kw).lower() in blob for kw in keywords) if keywords else False


def make_relevance(case: Dict[str, Any]) -> Tuple[Callable[[Dict[str, Any]], bool], int, str]:
    """按 case 字段选相关性判定函数,并给出该 query 的相关文档总数估计。"""
    golden = case.get("golden_context_ids")
    if golden:
        total = max(1, len(golden))
        return (lambda h: _rel_by_source(h, golden)), total, "by-source"
    kws = case.get("expected_doc_keywords") or case.get("expected_keywords")
    # 关键词模式无法得知语料中相关文档总数,Recall 分母取"该 query 至少 1 条"→ 1
    return (lambda h: _rel_by_keyword(h, kws or [])), 1, "by-keyword"


# ════════════════════════════════════════════════════════════════════
# 两条检索管线(retrieve_fn 注入;agentic 逻辑镜像 agent/agentic_rag.py)
# ════════════════════════════════════════════════════════════════════
class Counters:
    __slots__ = ("llm_calls", "retrievals")

    def __init__(self) -> None:
        self.llm_calls = 0
        self.retrievals = 0


def run_plain(query: str, retrieve_fn: Callable, k: int,
              counters: Counters) -> List[Dict[str, Any]]:
    """普通 RAG:单次检索,无改写、无多轮、无 LLM。"""
    counters.retrievals += 1
    hits = retrieve_fn(query, top_k=k) or []
    return list(hits)[:k]


def run_agentic(query: str, retrieve_fn: Callable, rewrite_fn: Callable,
                eval_fn: Callable, k: int, max_rounds: int,
                min_hits: int, counters: Counters) -> List[Dict[str, Any]]:
    """Agentic RAG:改写 → 多路召回合并去重 → 启发式门控 → 不足则再改写重检。

    与 agent/agentic_rag.py 一致:命中数达 min_hits 直接采用(省 LLM 评估);
    偏弱且有下一轮预算才用 eval_fn(LLM)决定是否再改写。
    """
    current = query
    best: List[Dict[str, Any]] = []
    for rnd in range(1, max_rounds + 1):
        counters.llm_calls += 1                      # 一次 Query Rewrite
        queries = rewrite_fn(current) or [current]
        merged: List[Dict[str, Any]] = []
        seen = set()
        for q in queries:
            counters.retrievals += 1
            for h in (retrieve_fn(q, top_k=k) or []):
                key = (h.get("title"), h.get("source"))
                if key not in seen:
                    seen.add(key)
                    merged.append(h)
        merged.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        merged = merged[:k]
        best = merged or best
        if not merged:
            break
        if len(merged) >= min_hits or rnd >= max_rounds:
            break                                    # 检索良好或最后一轮 → 采用
        counters.llm_calls += 1                      # 一次充分性评估(LLM)
        ev = eval_fn(query, merged)
        if ev.get("sufficient", True):
            break
        nq = ev.get("new_queries") or []
        if nq:
            current = " ".join(nq)
        else:
            break
    return best


# ════════════════════════════════════════════════════════════════════
# 后端接线:真实 vs mock
# ════════════════════════════════════════════════════════════════════
def build_real_backend():
    """真实检索 + 真实 LLM 改写/评估(需依赖与 .env)。"""
    from agent.rag_backend import retrieve as backend_retrieve
    from agent.agentic_rag import _rewrite_query, _evaluate, _format_results
    from agent.llm_client import get_llm_client
    llm = get_llm_client()

    def retrieve_fn(q, top_k=5):
        return backend_retrieve(q, top_k=top_k)

    def rewrite_fn(q):
        return _rewrite_query(llm, q)

    def eval_fn(q, hits):
        return _evaluate(llm, q, _format_results(hits))

    return retrieve_fn, rewrite_fn, eval_fn


def build_mock_backend(dataset: List[Dict[str, Any]]):
    """确定性 mock:无 LLM/embedding 也能跑通,用于验证脚本与 CI 冒烟。

    构造一个"伪知识库":每条 case 造若干候选片段,相关片段含其关键词/来源,
    再掺入噪声片段。plain 直接返回;agentic 的 rewrite 拆词以提升召回。
    """
    corpus: List[Dict[str, Any]] = []
    for c in dataset:
        src = (c.get("golden_context_ids") or ["kb"])[0]
        kws = c.get("expected_doc_keywords") or c.get("expected_keywords") or []
        # 相关片段
        corpus.append({"title": c["query"][:12], "text": " ".join(kws),
                       "source": src, "_kws": [w.lower() for w in kws]})
    # 干扰片段:各含"某一个"关键词但来源错误 —— 单次检索易被它顶上来
    all_kws = [w for c in dataset
               for w in (c.get("expected_doc_keywords")
                         or c.get("expected_keywords") or [])]
    for i, kw in enumerate(all_kws[:12]):
        corpus.append({"title": f"干扰{i}", "text": str(kw), "source": "distractor",
                       "_kws": [str(kw).lower()]})
    for i in range(4):
        corpus.append({"title": f"噪声{i}", "text": "无关内容", "source": "noise",
                       "_kws": []})

    def _score(seg, terms, broaden):
        """broaden=False(plain):整段作为一个查询串,词序敏感、易被单关键词干扰;
        broaden=True(agentic 逐词多路合并):对每个词独立计分,相关片段(含多词)得分更高。"""
        blob = (seg["title"] + " " + seg["text"]).lower()
        if broaden:
            return sum(1 for t in terms if t and t in blob)
        # plain:命中数相同则不区分,导致只含 1 个关键词的干扰片段与真片段并列/顶替
        hit = sum(1 for t in terms if t and t in blob)
        return 1 if hit > 0 else 0

    def _retrieve(q, top_k, broaden):
        terms = [w for w in q.lower().split() if w]
        scored = [(seg, _score(seg, terms, broaden)) for seg in corpus]
        scored = [(s, sc) for s, sc in scored if sc > 0] or \
                 [(seg, 0.1) for seg in corpus[:top_k]]
        # 稳定排序:分数降序,同分按干扰在前(模拟 plain 被带偏)
        scored.sort(key=lambda x: (x[1], x[0]["source"] == "distractor"), reverse=True)
        return [{"title": s["title"], "text": s["text"], "source": s["source"],
                 "score": float(sc)} for s, sc in scored[:top_k]]

    def retrieve_fn(q, top_k=5):
        # 默认按 plain 语义(单次、词序敏感);agentic 通过 rewrite 拆词走 broaden 路径
        broaden = "\x00multi" in q
        return _retrieve(q.replace("\x00multi", "").strip(), top_k, broaden)

    def rewrite_fn(q):
        # 模拟改写:把 query 拆成关键词多变体,并打标记走"广召回"路径
        words = [w for w in q.replace("?", " ").replace("？", " ").split()
                 if len(w) >= 2][:3]
        return [("\x00multi" + q)] + ["\x00multi" + w for w in words]

    def eval_fn(q, hits):
        return {"sufficient": len(hits) >= 2, "reason": "mock", "new_queries": []}

    return retrieve_fn, rewrite_fn, eval_fn


# ════════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════════
def evaluate(dataset: List[Dict[str, Any]], retrieve_fn, rewrite_fn, eval_fn,
             k: int, rounds: int, min_hits: int) -> Dict[str, Any]:
    agg = {m: {"plain": 0.0, "agentic": 0.0}
           for m in ("HitRate", "Recall", "MRR", "Precision")}
    cost = {"plain": Counters(), "agentic": Counters()}
    n = 0
    per_case = []
    for case in dataset:
        rel_fn, total_rel, mode = make_relevance(case)
        q = case["query"]

        plain_hits = run_plain(q, retrieve_fn, k, cost["plain"])
        ag_hits = run_agentic(q, retrieve_fn, rewrite_fn, eval_fn, k, rounds,
                              min_hits, cost["agentic"])

        pf = [rel_fn(h) for h in plain_hits]
        af = [rel_fn(h) for h in ag_hits]

        row = {"id": case.get("id"), "query": q, "mode": mode}
        for name, flags in (("plain", pf), ("agentic", af)):
            row[f"{name}_hit"] = hit_rate_at_k(flags, k)
            row[f"{name}_recall"] = recall_at_k(flags, k, total_rel)
            row[f"{name}_mrr"] = mrr(flags)
            row[f"{name}_prec"] = precision_at_k(flags, k)
        agg["HitRate"]["plain"] += row["plain_hit"]
        agg["HitRate"]["agentic"] += row["agentic_hit"]
        agg["Recall"]["plain"] += row["plain_recall"]
        agg["Recall"]["agentic"] += row["agentic_recall"]
        agg["MRR"]["plain"] += row["plain_mrr"]
        agg["MRR"]["agentic"] += row["agentic_mrr"]
        agg["Precision"]["plain"] += row["plain_prec"]
        agg["Precision"]["agentic"] += row["agentic_prec"]
        per_case.append(row)
        n += 1

    for m in agg:
        for meth in agg[m]:
            agg[m][meth] = agg[m][meth] / n if n else 0.0
    return {"n": n, "k": k, "metrics": agg, "cost": cost, "per_case": per_case}


def _fmt_row(name: str, plain: float, agentic: float) -> str:
    delta = agentic - plain
    arrow = "↑" if delta > 1e-9 else ("↓" if delta < -1e-9 else "＝")
    return (f"  {name:<12} plain={plain:6.3f}   agentic={agentic:6.3f}   "
            f"Δ={delta:+.3f} {arrow}")


def main() -> int:
    ap = argparse.ArgumentParser(description="普通 RAG vs Agentic RAG 对比评测")
    ap.add_argument("--dataset", default="eval/golden_set.jsonl",
                    help="评测集 jsonl(默认 golden_set;也可用 retrieval_eval.jsonl)")
    ap.add_argument("--k", type=int, default=5, help="Top-K(默认 5)")
    ap.add_argument("--rounds", type=int, default=2, help="Agentic 最大轮数")
    ap.add_argument("--min-hits", type=int, default=3,
                    help="Agentic 门控:命中数达此值即停(默认 3)")
    ap.add_argument("--category", default="", help="只评某类别")
    ap.add_argument("--mock", action="store_true",
                    help="纯 mock(无 LLM/embedding,验证脚本/CI 冒烟)")
    ap.add_argument("--csv", default="", help="导出逐条结果 CSV")
    args = ap.parse_args()

    ds_path = Path(args.dataset)
    if not ds_path.exists():
        print(f"[错误] 找不到评测集: {ds_path}", file=sys.stderr)
        return 1
    rows = [json.loads(l) for l in ds_path.open(encoding="utf-8") if l.strip()]
    # golden_set 混了多层 case,只取检索类;retrieval_eval 全是检索类
    rows = [r for r in rows if r.get("query") and
            (r.get("golden_context_ids") or r.get("expected_doc_keywords")
             or r.get("expected_keywords"))]
    if args.category:
        rows = [r for r in rows if r.get("category") == args.category]
    if not rows:
        print("[错误] 评测集为空(或过滤后无数据)", file=sys.stderr)
        return 1

    print(f"评测集: {ds_path.name} · {len(rows)} 条 · Top-{args.k} · "
          f"Agentic 最多 {args.rounds} 轮 / 门控 {args.min_hits} 命中")
    mode_note = "MOCK(无 LLM)" if args.mock else \
        f"真实后端 RAG_BACKEND={os.environ.get('RAG_BACKEND','tfidf')}"
    print(f"模式: {mode_note}\n" + "─" * 60)

    if args.mock:
        retrieve_fn, rewrite_fn, eval_fn = build_mock_backend(rows)
    else:
        try:
            retrieve_fn, rewrite_fn, eval_fn = build_real_backend()
        except Exception as exc:
            print(f"[错误] 真实后端不可用({type(exc).__name__}: {exc})。"
                  f"可先用 --mock 验证脚本,或检查依赖/.env。", file=sys.stderr)
            return 1

    t0 = time.perf_counter()
    res = evaluate(rows, retrieve_fn, rewrite_fn, eval_fn,
                   args.k, args.rounds, args.min_hits)
    dt = time.perf_counter() - t0

    m = res["metrics"]
    print("检索质量(越高越好):")
    print(_fmt_row("HitRate@%d" % args.k, m["HitRate"]["plain"], m["HitRate"]["agentic"]))
    print(_fmt_row("Recall@%d" % args.k, m["Recall"]["plain"], m["Recall"]["agentic"]))
    print(_fmt_row("MRR", m["MRR"]["plain"], m["MRR"]["agentic"]))
    print(_fmt_row("Precision@%d" % args.k, m["Precision"]["plain"], m["Precision"]["agentic"]))

    cp, ca = res["cost"]["plain"], res["cost"]["agentic"]
    n = res["n"]
    print("\n成本(越低越省):")
    print(f"  平均 LLM 调用   plain={cp.llm_calls/n:6.2f}   "
          f"agentic={ca.llm_calls/n:6.2f}   (改写+评估)")
    print(f"  平均检索次数    plain={cp.retrievals/n:6.2f}   "
          f"agentic={ca.retrievals/n:6.2f}")
    print(f"\n耗时 {dt:.2f}s · 每条约 {dt/n*1000:.0f}ms")

    # 结论
    hit_gain = m["HitRate"]["agentic"] - m["HitRate"]["plain"]
    mrr_gain = m["MRR"]["agentic"] - m["MRR"]["plain"]
    extra_llm = (ca.llm_calls - cp.llm_calls) / n
    print("─" * 60)
    if hit_gain > 1e-9 or mrr_gain > 1e-9:
        print(f"结论:Agentic 提升 HitRate {hit_gain:+.3f} / MRR {mrr_gain:+.3f},"
              f"代价 +{extra_llm:.2f} 次 LLM/查询。")
        rc = 0
    else:
        print(f"结论:本数据集上 Agentic 未见检索提升(可能 plain 已足够好),"
              f"却多花 +{extra_llm:.2f} 次 LLM/查询 —— 建议对这类 query 关闭改写。")
        rc = 2

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(res["per_case"][0].keys()))
            w.writeheader()
            w.writerows(res["per_case"])
        print(f"逐条结果已导出: {args.csv}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
