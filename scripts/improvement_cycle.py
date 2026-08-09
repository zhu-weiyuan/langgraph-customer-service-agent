#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
improvement_cycle.py — P4 半自动自我改进闭环，一键跑整环。

流程：收集(FeedbackStore 统计) → 分析 → 生成候选 prompt(candidate) →
影子评测(golden set + 可选 LLM judge) → 打印待审批摘要。
审批/灰度/全量/回滚见 scripts/approve_prompt.py（人工环节，故称"半自动"）。

用法：
    python scripts/improvement_cycle.py                 # 真实 LLM(需 llm_client 可用)
    python scripts/improvement_cycle.py --dry-run       # 无 LLM:回显桩,只验证链路
    python scripts/improvement_cycle.py --judge         # 追加 LLM judge 成对评审
    python scripts/improvement_cycle.py --db data/p4.db --min-cases 5

定时任务(cron)建议 —— 每天凌晨 3 点跑一轮,产出的候选早晨人工审批:
    0 3 * * * cd /path/to/langgraph-customer-service-agent && \
        /usr/bin/python3 scripts/improvement_cycle.py >> logs/improvement_cycle.log 2>&1
若部署环境有 systemd,亦可用 systemd timer(OnCalendar=*-*-* 03:00:00)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.feedback_store import FeedbackStore                      # noqa: E402
from agent.prompt_registry import PromptRegistry, seed_default_prompts  # noqa: E402
from agent.self_improve import run_improvement_cycle_programmatic   # noqa: E402
from agent.shadow_eval import ShadowEvalRunner                      # noqa: E402


def _echo_llm(system: str, user: str) -> str:
    """--dry-run 桩:不调 LLM,返回确定性文本,仅用于验证链路本身。"""
    if any(k in user for k in ("忽略", "DAN", "骗子", "诋毁", "上一个用户", "炒股", "管理员")):
        return "抱歉,这个请求我无法协助,我们回到产品相关的问题上吧。"
    return f"您好,关于您的问题({user[:40]}),这里是参考回答:退货 保修 WiFi 快递 发票 退款 音量 蓝牙 云服务 订单 抱歉 安全 App 绑定 闹钟 语音 控制 网络 售后 物流 七天 流程 申请 范围 价格 优惠 智能家居 套装 音乐 平台 杂音 开机 电源 离线 连接 自提 天 退 换。"


def main() -> int:
    ap = argparse.ArgumentParser(description="P4 self-improvement cycle")
    ap.add_argument("--db", default=None, help="SQLite 路径(默认 data/p4_self_improve.db)")
    ap.add_argument("--prompt", default="system_prompt")
    ap.add_argument("--min-cases", type=int, default=3)
    ap.add_argument("--judge", action="store_true", help="启用 LLM judge 成对评审")
    ap.add_argument("--dry-run", action="store_true", help="不调真实 LLM,用回显桩")
    ap.add_argument("--golden", default=None, help="golden set 路径")
    args = ap.parse_args()

    store = FeedbackStore(db_path=args.db)
    registry = PromptRegistry(db_path=args.db)
    seed_default_prompts(registry)

    print("== [1/4] 收集:反馈信号统计 ==")
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))

    print("\n== [2/4] 分析 + 生成候选 ==")
    report = run_improvement_cycle_programmatic(
        store, registry, args.prompt, min_cases=args.min_cases)
    print(json.dumps({k: v for k, v in report.items() if k != "pattern_stats"},
                     ensure_ascii=False, indent=2, default=str))

    if not report.get("candidate"):
        print("\n无候选产生(信号不足或规则未触发),本轮结束。")
        return 0

    print("\n== [3/4] 影子评测(candidate vs baseline) ==")
    llm_fn = _echo_llm if args.dry_run else None
    runner = ShadowEvalRunner(registry, llm_fn=llm_fn, db_path=args.db,
                              use_judge=args.judge, golden_path=args.golden)
    eval_report = runner.run(args.prompt,
                             candidate_version_no=report["candidate"]["version_no"])

    print(json.dumps({k: v for k, v in eval_report.items() if k != "per_case"},
                     ensure_ascii=False, indent=2))

    print("\n== [4/4] 待审批摘要 ==")
    if eval_report["passed"]:
        v = eval_report["candidate_version"]
        print(f"候选 {args.prompt} v{v} 已过影子评测门槛 → 状态 pending_approval")
        print("下一步(人工):")
        print(f"  python scripts/approve_prompt.py approve {v} --percent 10   # 批准并 10% 灰度")
        print(f"  python scripts/approve_prompt.py promote                    # 观察后全量")
        print(f"  python scripts/approve_prompt.py rollback                   # 异常一键回滚")
    else:
        print(f"候选 v{eval_report['candidate_version']} 未过门槛 → 已标记 rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
