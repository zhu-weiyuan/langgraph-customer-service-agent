#!/usr/bin/env python3
"""
scripts/trace_tool.py — Trace 回放 CLI。

用法 (在 repo 根目录):
    python -m scripts.trace_tool list [--failed] [--low-score] [--user X] [--scene Y] [--limit N]
    python -m scripts.trace_tool show   <request_id>
    python -m scripts.trace_tool replay <request_id> [--rerun]
    python -m scripts.trace_tool diff   <id1> <id2>

db 路径通过 --db 或环境变量 TRACE_DB 指定 (默认 data/trace.db, 与 app 一致)。

说明:
  list   捞取符合条件的 trace 摘要 (支持 --failed / --low-score 过滤差评请求)。
  show   打印一次请求的结构化时间线 (inspect: 每个 span 的耗时/输入/输出)。
  replay 默认 inspect; --rerun 用记录的 query 重新走一遍检索并与当时召回对比
         (需要项目内可用的检索器; 缺失时降级为 recorded-only 回放)。
  diff   对比两个 trace 的关键字段 (延迟/成本/token/召回来源/答案 hash…)。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.observability import get_trace_service  # noqa: E402
from agent import trace_replay  # noqa: E402


def _load_retriever():
    """尝试装配一个注入式 retriever: fn(query)->list(dict)。装配失败返回 None。

    优先 rag_backend / hybrid_rag 中的检索入口; 任何导入/调用异常都降级为 None,
    replay --rerun 会退回 recorded-only 回放。
    """
    try:
        from agent.rag_backend import search as _search  # type: ignore

        def _r(q):
            res = _search(q) or []
            return res
        return _r
    except Exception:
        pass
    try:
        from agent.hybrid_rag import HybridRAG  # type: ignore
        rag = HybridRAG()

        def _r2(q):
            return rag.search(q)
        return _r2
    except Exception:
        return None


def cmd_list(args):
    svc = get_trace_service(args.db)
    filters = {"limit": args.limit}
    if args.failed:
        filters["failed"] = True
    if args.low_score:
        filters["low_score"] = True
    if args.user:
        filters["user_id"] = args.user
    if args.scene:
        filters["scene"] = args.scene
    rows = trace_replay.list_traces(filters, service=svc)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(no traces matched)")
        return 0
    print("%-34s %-10s %-8s %-6s %-6s %-9s %s"
          % ("request_id", "user", "total_ms", "fail", "low", "score", "created_at"))
    for r in rows:
        print("%-34s %-10s %-8s %-6s %-6s %-9s %s"
              % (r["request_id"], (r.get("user_id") or "")[:10],
                 r.get("total_ms"), r.get("failed"), r.get("low_score"),
                 r.get("eval_score"), r.get("created_at")))
    return 0


def cmd_show(args):
    svc = get_trace_service(args.db)
    trace = trace_replay.load_trace(args.request_id, service=svc)
    if trace is None:
        print("trace not found: %s" % args.request_id, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(trace, ensure_ascii=False, indent=2))
    else:
        print(trace_replay.format_timeline(trace))
    return 0


def cmd_replay(args):
    svc = get_trace_service(args.db)
    mode = "rerun" if args.rerun else "inspect"
    retriever = _load_retriever() if args.rerun else None
    try:
        trace_replay.replay(args.request_id, mode, service=svc,
                            retriever=retriever, echo=True)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def cmd_diff(args):
    svc = get_trace_service(args.db)
    try:
        trace_replay.replay(args.id1, "diff", service=svc, other=args.id2, echo=True)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="trace_tool", description="Trace 回放 CLI")
    p.add_argument("--db", default=os.getenv("TRACE_DB", "data/trace.db"),
                   help="trace SQLite 路径 (默认 $TRACE_DB 或 data/trace.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="列出 trace 摘要")
    pl.add_argument("--failed", action="store_true", help="只看失败请求")
    pl.add_argument("--low-score", dest="low_score", action="store_true",
                    help="只看低分/差评请求")
    pl.add_argument("--user", help="按 user_id 过滤")
    pl.add_argument("--scene", help="按 scene 过滤")
    pl.add_argument("--limit", type=int, default=50)
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="打印结构化时间线")
    ps.add_argument("request_id")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_show)

    pr = sub.add_parser("replay", help="回放 (inspect 或 --rerun)")
    pr.add_argument("request_id")
    pr.add_argument("--rerun", action="store_true",
                    help="用记录的 query 重新检索并对比当时召回")
    pr.set_defaults(func=cmd_replay)

    pd = sub.add_parser("diff", help="对比两个 trace")
    pd.add_argument("id1")
    pd.add_argument("id2")
    pd.set_defaults(func=cmd_diff)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
