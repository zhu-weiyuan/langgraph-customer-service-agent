#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
approve_prompt.py — P4 候选 prompt 的人工审批 / 灰度 / 全量 / 回滚 CLI。

子命令：
    list                          列出版本、状态与当前发布(含灰度百分比)
    approve <version> [--percent 10] [--tenant T] [--env prod]
                                  审批 pending_approval 候选并按百分比灰度发布
    promote  [--version N]        把当前灰度(或指定版本)提为 100% 全量
    rollback                      一步回滚到上一个全量版本

示例：
    python scripts/approve_prompt.py list
    python scripts/approve_prompt.py approve 3 --percent 10
    python scripts/approve_prompt.py promote
    python scripts/approve_prompt.py rollback
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.prompt_registry import PromptRegistry  # noqa: E402


def cmd_list(reg: PromptRegistry, args) -> int:
    for name, versions in sorted(reg._versions.items()):
        print(f"\n[{name}]")
        for v in versions:
            marks = []
            if v.diff:
                marks.append(f"diff:{len(v.diff.get('segment_changes', []))}seg")
            print(f"  v{v.version_no:<3} status={v.status:<17} "
                  f"reason={v.change_reason[:60]!r} {' '.join(marks)}")
        try:
            active = reg.get_active(name, env=args.env)
            rel = reg._active_releases(name, args.env, args.tenant)
            canary = rel["canary"]
            canary_str = (f" canary=v{reg.get_version_by_id(canary['version_id']).version_no}"
                          f"@{canary['percent']}%" if canary else "")
            print(f"  active(env={args.env}): v{active.version_no}{canary_str}")
        except KeyError:
            pass
    return 0


def cmd_approve(reg: PromptRegistry, args) -> int:
    pv = reg.get_version(args.prompt, args.version)
    if pv.status not in ("pending_approval", "candidate", "approved"):
        print(f"拒绝:v{pv.version_no} 状态为 {pv.status},不可审批发布", file=sys.stderr)
        return 1
    if pv.status != "pending_approval":
        print(f"警告:v{pv.version_no} 状态为 {pv.status}(未过影子评测门槛流程)")
    reg.set_status(pv.version_id, "approved")
    result = reg.release(args.prompt, pv.version_no, percent=args.percent,
                         env=args.env, tenant=args.tenant)
    print("已审批并发布:", json.dumps(result, ensure_ascii=False))
    return 0


def cmd_promote(reg: PromptRegistry, args) -> int:
    result = reg.promote_full(args.prompt, args.version,
                              env=args.env, tenant=args.tenant)
    print("已全量:", json.dumps(result, ensure_ascii=False))
    return 0


def cmd_rollback(reg: PromptRegistry, args) -> int:
    result = reg.rollback(args.prompt, env=args.env, tenant=args.tenant)
    print("已回滚:", json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="P4 prompt 审批/灰度/回滚")
    ap.add_argument("--db", default=None)
    ap.add_argument("--prompt", default="system_prompt")
    ap.add_argument("--env", default="prod")
    ap.add_argument("--tenant", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")
    p_approve = sub.add_parser("approve")
    p_approve.add_argument("version", type=int)
    p_approve.add_argument("--percent", type=int, default=10)
    p_promote = sub.add_parser("promote")
    p_promote.add_argument("--version", type=int, default=None)
    sub.add_parser("rollback")

    args = ap.parse_args()
    reg = PromptRegistry(db_path=args.db)
    return {"list": cmd_list, "approve": cmd_approve,
            "promote": cmd_promote, "rollback": cmd_rollback}[args.cmd](reg, args)


if __name__ == "__main__":
    raise SystemExit(main())
