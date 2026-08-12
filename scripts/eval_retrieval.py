#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检索评估脚本 — HitRate@5 / MRR（retriever 注入式）

用法：
    # mock 模式（无依赖、CI 可跑，验证评估管线本身）
    python scripts/eval_retrieval.py --backend mock

    # 现有 TF-IDF/BM25（需 jieba + knowledge/*.md）
    python scripts/eval_retrieval.py --backend tfidf

    # 混合检索（需相应后端就绪，见 RAG_UPGRADE.md）
    RAG_BACKEND=hybrid python scripts/eval_retrieval.py --backend hybrid
    RAG_BACKEND=pgvector python scripts/eval_retrieval.py --backend pgvector

判定：top-5 结果中任一条的 title+content 包含 expected_doc_keywords
之一（不区分大小写）即视为命中；MRR 取首个命中位置的倒数。

也可被其他脚本 import：evaluate(retriever_fn, cases) 完全注入式。
"""

from __future__ import annotations

<<<<<<< HEAD
# 注意：load_dotenv() 不在模块级别调用，避免污染测试进程环境。
# 仅在 CLI 入口（main）和需要环境变量的后端（make_hybrid_retriever）中加载。
=======
# .env 加载（脚本独立运行也生效；python-dotenv 缺失时静默跳过）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
>>>>>>> origin/master

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Dict, List

ROOT = Path(__file__).resolve().parent.parent
EVAL_FILE = ROOT / "eval" / "retrieval_eval.jsonl"
sys.path.insert(0, str(ROOT))

TOP_K = 5


# ---------------------------------------------------------------------------
# 核心评估逻辑（纯函数，retriever 注入）
# ---------------------------------------------------------------------------

def load_cases(path: Path = EVAL_FILE) -> List[Dict]:
    cases = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def judge_hit(results: List[Dict], keywords: List[str]) -> int:
    """返回首个命中位置（1-based），未命中返回 0。"""
    lowered = [k.lower() for k in keywords]
    for rank, r in enumerate(results[:TOP_K], start=1):
        haystack = (str(r.get("title", "")) + " "
                    + str(r.get("content", r.get("text", "")))).lower()
        if any(k in haystack for k in lowered):
            return rank
    return 0


def evaluate(retriever_fn: Callable[[str], List[Dict]],
             cases: List[Dict]) -> Dict:
    """retriever_fn(query) -> [{title, content/text, ...}]。

    Returns: {"hit_rate_at_5", "mrr", "n", "per_category", "failures"}
    """
    hits, rr_sum = 0, 0.0
    per_cat: Dict[str, Dict[str, float]] = {}
    failures = []
    for case in cases:
        try:
            results = retriever_fn(case["query"]) or []
        except Exception as e:
            results = []
            failures.append({"id": case.get("id"), "error": str(e)})
        rank = judge_hit(results, case["expected_doc_keywords"])
        cat = case.get("category", "unknown")
        stat = per_cat.setdefault(cat, {"n": 0, "hits": 0, "rr": 0.0})
        stat["n"] += 1
        if rank:
            hits += 1
            rr_sum += 1.0 / rank
            stat["hits"] += 1
            stat["rr"] += 1.0 / rank
        else:
            failures.append({"id": case.get("id"), "query": case["query"],
                             "category": cat})
    n = len(cases)
    report = {
        "n": n,
        "hit_rate_at_5": round(hits / n, 4) if n else 0.0,
        "mrr": round(rr_sum / n, 4) if n else 0.0,
        "per_category": {
            c: {"n": s["n"],
                "hit_rate_at_5": round(s["hits"] / s["n"], 4),
                "mrr": round(s["rr"] / s["n"], 4)}
            for c, s in sorted(per_cat.items())
        },
        "failures": failures,
    }
    return report


# ---------------------------------------------------------------------------
# 内置 retriever 后端
# ---------------------------------------------------------------------------

def make_mock_retriever(cases: List[Dict]) -> Callable[[str], List[Dict]]:
    """mock：用评估集自身关键词造一个"完美+噪声"知识库，验证评估管线可跑。

    故意让部分 case（id 尾数为 0）只在 rank 3 命中，检验 MRR 计算。
    """
    kb = []
    for case in cases:
        kb.append({
            "query": case["query"],
            "title": " ".join(case["expected_doc_keywords"][:1]),
            "content": "，".join(case["expected_doc_keywords"]) + " 的处理说明。",
            "id": case["id"],
        })

    def retriever(query: str) -> List[Dict]:
        target = next((d for d in kb if d["query"] == query), None)
        noise = [{"title": f"无关文档{i}", "content": "与主题无关的内容占位。",
                  "score": 0.1, "source": "noise", "parent_id": None}
                 for i in range(4)]
        if target is None:
            return noise
        hit = {"title": target["title"], "content": target["content"],
               "score": 0.9, "source": "mock_kb", "parent_id": None}
        if target["id"].endswith("0"):
            return noise[:2] + [hit] + noise[2:]   # rank 3 命中
        return [hit] + noise                        # rank 1 命中
    return retriever


def make_tfidf_retriever() -> Callable[[str], List[Dict]]:
    from agent.rag import retrieve  # 需要 jieba + knowledge/

    def retriever(query: str) -> List[Dict]:
        hits = retrieve(query, top_k=TOP_K, use_vector=False)
        for h in hits:
            h.setdefault("content", h.get("text", ""))
        return hits
    return retriever


def make_hybrid_retriever(backend: str) -> Callable[[str], List[Dict]]:
    import os
<<<<<<< HEAD
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
=======
>>>>>>> origin/master
    os.environ["RAG_BACKEND"] = backend
    from agent.hybrid_rag import build_retriever_from_env
    retriever = build_retriever_from_env()
    return lambda q: retriever.search(q)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
<<<<<<< HEAD
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
=======
>>>>>>> origin/master
    ap = argparse.ArgumentParser(description="Retrieval eval: HitRate@5 / MRR")
    ap.add_argument("--backend", default="mock",
                    choices=["mock", "tfidf", "hybrid", "pgvector"])
    ap.add_argument("--eval-file", default=str(EVAL_FILE))
    ap.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    args = ap.parse_args()

    cases = load_cases(Path(args.eval_file))

    if args.backend == "mock":
        retriever = make_mock_retriever(cases)
    elif args.backend == "tfidf":
        retriever = make_tfidf_retriever()
    else:
        retriever = make_hybrid_retriever(args.backend)

    report = evaluate(retriever, cases)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"backend={args.backend}  n={report['n']}")
        print(f"HitRate@5 = {report['hit_rate_at_5']:.4f}")
        print(f"MRR       = {report['mrr']:.4f}")
        for cat, s in report["per_category"].items():
            print(f"  [{cat}] n={s['n']} hit@5={s['hit_rate_at_5']:.4f} "
                  f"mrr={s['mrr']:.4f}")
        if report["failures"]:
            print(f"failures: {len(report['failures'])}")
            for f in report["failures"][:10]:
                print(f"  - {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
