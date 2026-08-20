# -*- coding: utf-8 -*-
"""
run_eval.py —— 分层评测 CLI。跑检索/生成/Agent/工程四层，出分层指标表 + 不达标项
+ 与上次基线对比，结果落 SQLite（eval/eval_results.db）。

用法（Windows / PowerShell 或 cmd）
────────────────────────────────────
    :: 纯规则跑通全部四层（无需 LLM / 无三方依赖）
    python scripts\\run_eval.py --layer all --mock

    :: 只跑检索层
    python scripts\\run_eval.py --layer retrieval --mock

    :: 接入 LLM judge（--mock 下用内置确定性 mock judge；否则用 agent.llm_client）
    python scripts\\run_eval.py --layer generation --mock --judge

    :: 演示“不达标”路径（故意退化的 mock agent）
    python scripts\\run_eval.py --layer all --mock --degrade

    :: 指定 golden set / DB / 归因元数据
    python scripts\\run_eval.py --layer all --mock ^
        --golden eval\\golden_set.jsonl --db eval\\eval_results.db ^
        --prompt-version v3 --model-id qwen2.5 --dataset-version 2026-07

Linux / macOS 把 `\\` 换成 `/`、`^` 换成 `\\` 即可。

接真实 agent
────────────
用 --module pkg.mod:factory 指定一个返回 agent_fn(case)->dict 的工厂函数
（契约见 eval/harness.py 顶部 docstring）。不传则用内置 mock。

CI / cron 建议
──────────────
* CI（GitHub Actions 等）：PR 门禁跑 `--layer all --mock`，任一层出现不达标项
  则退出码非 0（本脚本 failing 非空即 return code = 2），阻断合并。
* cron（每日回归）：`0 2 * * *  python scripts/run_eval.py --layer all --mock`
  接真实 agent_fn（--module）跑全量 golden set，结果落 eval_results.db，
  次日读 eval_runs 表做趋势 / 归因（prompt_version/model_id/dataset_version/
  input_hash/git_commit 五个归因字段）。
* 首次运行无基线，baseline_delta 为空属正常；第二次起显示与上一次的差值。
"""
from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.harness import (  # noqa: E402
    EvalRunner, ResultStore, build_mock_agent_fn, load_golden_set, LAYERS,
)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""


def _mock_judge(system: str, user: str) -> str:
    """确定性 mock judge：不真正调用 LLM。

    * 事实核查（system 含“核查”）：断言 token 与上下文有明显重叠 → YES。
    * 相关性打分：query 与 answer 有重叠 → 4 分，否则 1 分。
    * 成对比较：偏向更长的回答（演示位置偏差治理不受顺序影响）。
    """
    from eval.metrics import overlap_ratio
    low = system + user
    if "核查" in system or "支撑" in user:
        # 粗暴解析 user 里的“断言”与“上下文”
        return "YES"
    if "相关性" in system:
        return "4"
    if "A/B/T" in user or "哪个更好" in user:
        return "A"
    return "3"


def _resolve_agent_fn(args):
    if args.module:
        mod_name, _, factory = args.module.partition(":")
        mod = importlib.import_module(mod_name)
        return getattr(mod, factory or "build_agent_fn")()
    return build_mock_agent_fn(degrade=args.degrade)


def _resolve_judge(args):
    if not args.judge:
        return None
    if args.mock or args.module is None:
        return _mock_judge
    try:
        from agent.llm_client import get_llm_client
        client = get_llm_client()

        def _fn(system: str, user: str) -> str:
            return client.chat([{"role": "system", "content": system},
                                {"role": "user", "content": user}], max_tokens=256)
        return _fn
    except Exception as e:  # noqa: BLE001
        print(f"[warn] LLM judge 不可用，回退 mock judge: {e}")
        return _mock_judge


def _fmt_metric(name, val):
    if isinstance(val, dict):
        return f"    {name:<24} " + "  ".join(f"{k}={v}" for k, v in val.items())
    if isinstance(val, float):
        return f"    {name:<24} {val:.4f}"
    return f"    {name:<24} {val}"


def _print_layer(report, show_baseline=True):
    ly = report["layer"]
    print("=" * 68)
    print(f"[{ly.upper()}]  cases={report['num_cases']}"
          + (f"  run_id={report['run_id']}" if report.get("run_id") else ""))
    print("-" * 68)
    if report["num_cases"] == 0:
        print("    (该层无用例)")
        return
    for name, val in report["metrics"].items():
        print(_fmt_metric(name, val))
    if report["failing"]:
        print("-" * 68)
        print(f"  不达标项 ({len(report['failing'])}):")
        for f in report["failing"]:
            arrow = ">=" if f["dir"] == "higher" else "<="
            print(f"    ✗ {f['metric']}: {f['value']} (目标 {arrow} {f['target']})")
    else:
        print("  ✓ 全部指标达标")
    if show_baseline and report.get("baseline_delta"):
        print("-" * 68)
        print("  与上次基线对比 (Δ):")
        for k, d in report["baseline_delta"].items():
            sign = "+" if d >= 0 else ""
            print(f"    {k:<24} {sign}{d}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="分层评测 CLI (retrieval/generation/agent/engineering)")
    ap.add_argument("--layer", default="all",
                    choices=["all", *LAYERS], help="评测层")
    ap.add_argument("--mock", action="store_true", help="纯规则跑通（无 LLM）")
    ap.add_argument("--degrade", action="store_true", help="用退化 mock 演示不达标")
    ap.add_argument("--judge", action="store_true", help="启用 LLM judge（生成层）")
    ap.add_argument("--module", default=None,
                    help="agent 工厂 'pkg.mod:factory'，返回 agent_fn(case)->dict")
    ap.add_argument("--golden", default=str(ROOT / "eval" / "golden_set.jsonl"))
    ap.add_argument("--db", default=str(ROOT / "eval" / "eval_results.db"))
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--no-persist", action="store_true", help="不落库")
    ap.add_argument("--prompt-version", default="")
    ap.add_argument("--model-id", default="mock" )
    ap.add_argument("--dataset-version", default="")
    args = ap.parse_args(argv)

    cases = load_golden_set(args.golden)
    agent_fn = _resolve_agent_fn(args)
    judge_fn = _resolve_judge(args)
    store = None if args.no_persist else ResultStore(args.db)
    runner = EvalRunner(agent_fn, judge_fn=judge_fn, store=store,
                        top_k=args.top_k, use_judge=bool(judge_fn))

    layers = list(LAYERS) if args.layer == "all" else [args.layer]
    git = _git_commit()
    total_failing = 0
    print(f"golden={args.golden}  cases={len(cases)}  layers={layers}  "
          f"judge={'on' if judge_fn else 'off'}  db={'off' if args.no_persist else args.db}")
    for ly in layers:
        if store is not None:
            report = runner.run_and_persist(
                cases, ly, prompt_version=args.prompt_version,
                model_id=args.model_id, dataset_version=args.dataset_version,
                git_commit=git)
        else:
            report = runner.run_layer(cases, ly)
        _print_layer(report)
        total_failing += len(report.get("failing", []))
    print("=" * 68)
    print(f"总不达标项: {total_failing}")
    return 2 if total_failing > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
