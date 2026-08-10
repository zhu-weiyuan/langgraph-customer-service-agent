# -*- coding: utf-8 -*-
"""ab_rerank.py — 同题 A/B：rerank on(remote) vs off(rule)，量化 rerank 真实增益。

用法：
    python eval/ab_rerank.py --ids exact-match-02,online-failure-01,... [--repeat 3]

对同一组题分别以 RAG_RERANKER=remote / rule 各跑一遍（严格模式），
输出对比 markdown 到 eval/reports/ab_rerank_{ts}.md。

注意：跑的是完整 generation 链路（含 judge），每题 remote+rule 各 ~2 分钟。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "eval" / "reports"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
EVAL = ROOT / "eval" / "run_real_eval.py"

DEFAULT_IDS = (
    "exact-match-02,online-failure-01,online-failure-09,business-critical-02,"
    "business-critical-07,multi-hop-03,multi-hop-06,comparison-01,"
    "high-frequency-06,privilege-expiry-06,exact-match-05,privilege-expiry-04"
)

GEN_KEYS = ("citation_accuracy", "faithfulness", "answer_relevancy",
            "context_recall", "context_precision")


def run_eval(ids: str, reranker: str, repeat: int, out: Path) -> Dict:
    env = dict(os.environ)
    env["RAG_RERANKER"] = reranker
    env["RAG_STRICT"] = "1"
    env["RAG_SEARCH_CACHE_TTL"] = "0"
    cmd = [str(PY), "-u", "-X", "utf8", str(EVAL), "--ids", ids,
           "--repeat", str(repeat), "--multi-turn"]
    print(f"  [run] reranker={reranker} repeat={repeat} ...")
    with open(out, "w", encoding="utf-8") as f:
        r = subprocess.run(cmd, env=env, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT)
    print(f"  [run] exit={r.returncode} log={out}")
    json_path = _latest_json(out)
    if not json_path:
        return {}
    return json.load(open(json_path, encoding="utf-8"))


def _latest_json(log: Path) -> Path:
    """从日志里解析最后保存的 JSON 报告路径。"""
    txt = log.read_text(encoding="utf-8", errors="replace")
    for line in reversed(txt.splitlines()):
        if "数据已保存:" in line:
            p = line.split("数据已保存:", 1)[1].strip()
            return Path(p)
    return None


def fmt(v) -> str:
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default=DEFAULT_IDS)
    ap.add_argument("--repeat", type=int, default=1,
                    help="每题重复次数（量化波动，耗时 ×repeat）")
    args = ap.parse_args()

    ids = args.ids
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_remote = REPORTS / f"ab_rerank_remote_{ts}.log"
    log_rule = REPORTS / f"ab_rerank_rule_{ts}.log"

    print(f"=== A/B rerank 对比: {ids} (repeat={args.repeat}) ===")
    print("[1/2] remote rerank ...")
    remote = run_eval(ids, "remote", args.repeat, log_remote)
    print("[2/2] rule (no rerank) ...")
    rule = run_eval(ids, "rule", args.repeat, log_rule)

    if not remote or not rule:
        print("[ab] 失败：至少一次运行没有产出 JSON 报告")
        return 1

    lines = [
        "# Rerank A/B 对比（remote vs rule，同题同集）",
        "",
        f"- 时间: {ts}",
        f"- 题集: {ids} (repeat={args.repeat})",
        f"- remote meta: reranker={remote.get('meta', {}).get('reranker_mode')} "
        f"git={remote.get('meta', {}).get('git_commit')}",
        f"- rule meta: reranker={rule.get('meta', {}).get('reranker_mode')} "
        f"git={rule.get('meta', {}).get('git_commit')}",
        "",
        "## 总体对比",
        "",
        "| 指标 | remote | rule | Δ |",
        "|------|--------|------|---|",
    ]
    m_r = remote["metrics"]
    m_l = rule["metrics"]
    for key in ("hit_rate_at_k", "mrr", "section_hit_rate_at_k",
                "chunk_hit_rate_at_k", "context_recall", "context_precision",
                "faithfulness", "answer_relevancy", "citation_accuracy"):
        a, b = m_r.get(key), m_l.get(key)
        delta = "" if a is None or b is None else f"{a - b:+.3f}"
        lines.append(f"| {key} | {fmt(a)} | {fmt(b)} | {delta} |")
    lines.append("")

    # 逐题对比
    lines += ["## 逐题对比", "", "| ID | 指标 | remote | rule | Δ |",
              "|----|------|--------|------|---|"]
    by_id_r = {r["id"]: r for r in remote["items"] if "error" not in r}
    by_id_l = {r["id"]: r for r in rule["items"] if "error" not in r}
    for iid in sorted(set(by_id_r) | set(by_id_l)):
        r, l = by_id_r.get(iid), by_id_l.get(iid)
        if r is None or l is None:
            lines.append(f"| {iid} | (missing in one run) | | | |")
            continue
        for key in ("hit_rate_at_k", "mrr", "section_hit_at_k",
                    "citation_accuracy", "faithfulness",
                    "answer_relevancy", "context_recall", "context_precision"):
            a = r.get(key) if key in ("hit_rate_at_k", "mrr", "section_hit_at_k") \
                else r.get("generation", {}).get(key)
            b = l.get(key) if key in ("hit_rate_at_k", "mrr", "section_hit_at_k") \
                else l.get("generation", {}).get(key)
            delta = "" if a is None or b is None else f"{a - b:+.3f}"
            lines.append(f"| {iid} | {key} | {fmt(a)} | {fmt(b)} | {delta} |")
        lines.append(f"| {iid} | retrieved_sources | {r.get('retrieved_sources')} | {l.get('retrieved_sources')} | |")
        lines.append("")

    md_path = REPORTS / f"ab_rerank_{ts}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nA/B 报告: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
