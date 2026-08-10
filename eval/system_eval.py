# -*- coding: utf-8 -*-
"""system_eval.py — 系统级评测（步骤 5，走真实 HTTP 后端）。

覆盖审计 P1-6 未覆盖的系统层：
  1. 流式 TTFT        — /api/chat stream=true，首 token 延迟 + 流式完成耗时
  2. 多轮上下文记忆    — 同 session 第二轮引用第一轮信息
  3. JWT 隔离         — 用户 A 的长期记忆对用户 B 不可见
  4. 并发/延迟/成功率 — N 并发 chat，p50/p95 + 失败率
  5. 成本估算          — 若响应带 usage 直接取，否则按字符数估算 token

用法：
    python eval/system_eval.py                    # 连 http://127.0.0.1:7860
    python eval/system_eval.py --base http://127.0.0.1:7860 --concurrency 6
    python eval/system_eval.py --skip-chat        # 跳过耗时的并发 chat 测试

输出：eval/reports/system_eval_{ts}.md + .json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "eval" / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)

DEFAULT_BASE = "http://127.0.0.1:7860"

SIMPLE_QUERY = "X-100 音箱保修多久？"
MEMORY_SEED_MSG = "帮我记一下：我的音箱型号是 X-200，发票日期是 2026 年 3 月。"
MEMORY_PROBE_MSG = "我上次说的音箱发票日期是什么时候？"


def _post(base: str, path: str, payload: Dict, token: str = "",
          timeout: float = 120) -> tuple:
    """POST JSON；返回 (status, body_json_or_none, headers, raw_bytes, elapsed)。"""
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            elapsed = time.perf_counter() - start
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = None
            return resp.status, body, dict(resp.headers), raw, elapsed
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None
        return exc.code, body, dict(exc.headers), raw, time.perf_counter() - start
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}, {}, b"", time.perf_counter() - start


def _get(base: str, path: str, token: str = "", timeout: float = 30) -> tuple:
    req = urllib.request.Request(base + path, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            body = json.loads(raw.decode("utf-8"))
            return resp.status, body, time.perf_counter() - start
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = None
        return exc.code, body, time.perf_counter() - start
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}, time.perf_counter() - start


def sse_ttft(base: str, message: str, timeout: float = 180) -> Dict:
    """POST stream=true，解析 SSE 帧，测首 token 延迟。"""
    payload = {"message": message, "stream": True}
    req = urllib.request.Request(base + "/api/chat",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "text/event-stream"})
    start = time.perf_counter()
    first_token_at: Optional[float] = None
    token_count = 0
    done_ok = False
    reply_chars = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    frame = json.loads(line[5:].strip())
                except Exception:
                    continue
                if "token" in frame:
                    if first_token_at is None:
                        first_token_at = time.perf_counter() - start
                    token_count += 1
                    reply_chars += len(str(frame.get("token", "")))
                if frame.get("done"):
                    done_ok = True
                    break
        total = time.perf_counter() - start
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "first_token_s": first_token_at,
                "tokens": token_count, "done": False}
    return {"first_token_s": round(first_token_at, 3) if first_token_at else None,
            "total_s": round(total, 3), "tokens": token_count,
            "reply_chars": reply_chars, "done": done_ok}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--skip-chat", action="store_true",
                    help="跳过并发 chat 测试（耗时较长）")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    out: Dict[str, Any] = {"base": base,
                           "timestamp": datetime.now().isoformat(timespec="seconds")}

    # 0. 健康检查
    st, body, _ = _get(base, "/healthz", timeout=10)
    out["healthz"] = {"status": st, "body": body}
    print(f"[healthz] {st}")

    # 1. 流式 TTFT（3 次取中位）
    ttft_runs = []
    for i in range(3):
        r = sse_ttft(base, SIMPLE_QUERY)
        ttft_runs.append(r)
        print(f"[ttft {i + 1}] first_token={r.get('first_token_s')}s "
              f"total={r.get('total_s')}s tokens={r.get('tokens')} done={r.get('done')}")
        time.sleep(1)
    ok_ttft = [r for r in ttft_runs if r.get("first_token_s") is not None]
    out["ttft"] = {"runs": ttft_runs,
                   "first_token_median_s": round(
                       sorted(r["first_token_s"] for r in ok_ttft)[len(ok_ttft) // 2], 3)
                   if ok_ttft else None,
                   "success": len(ok_ttft)}

    # 2. 多轮上下文记忆（guest session）
    mem = {"steps": []}
    sid = f"sys-eval-{int(time.time())}"
    for i, msg in enumerate([MEMORY_SEED_MSG, MEMORY_PROBE_MSG]):
        st, body, headers, _, el = _post(base, "/api/chat",
                                         {"message": msg, "session_id": sid,
                                          "stream": False}, timeout=180)
        reply = ""
        if isinstance(body, dict):
            replies = body.get("replies") or []
            reply = replies[-1].get("content", "") if replies else (body.get("reply") or "")
            if not reply and body.get("messages"):
                reply = str(body["messages"][-1])
        mem["steps"].append({"msg": msg[:20], "status": st, "elapsed_s": round(el, 2),
                             "reply_head": reply[:80]})
        print(f"[memory {i + 1}] status={st} reply={reply[:50]!r}")
        time.sleep(0.5)
    probe_reply = mem["steps"][-1].get("reply_head", "")
    mem["recall_hit"] = any(k in probe_reply for k in ("2026", "3 月", "3月", "三月", "发票"))
    out["multi_turn_memory"] = mem
    print(f"[memory] recall_hit={mem['recall_hit']}")

    # 3. JWT 隔离
    iso = {}
    ts = int(time.time())
    for who in ("a", "b"):
        st, body, _, _, _ = _post(base, "/api/auth/register",
                                  {"username": f"sys_eval_{who}_{ts}",
                                   "password": "Passw0rd!x", "display_name": who},
                                  timeout=30)
        iso[who] = {"status": st, "user_id": (body or {}).get("user_id"),
                    "token": (body or {}).get("access_token")}
        print(f"[auth {who}] register status={st}")
    tok_a = iso["a"].get("token") or ""
    tok_b = iso["b"].get("token") or ""
    if tok_a and tok_b:
        # A 存一条记忆
        _post(base, "/api/chat", {"message": "我的用户名是 Alice，最喜欢的颜色是蓝色。",
                                  "stream": False}, token=tok_a, timeout=120)
        st_a, body_a, _ = _get(base, "/api/memory", token=tok_a)
        st_b, body_b, _ = _get(base, "/api/memory", token=tok_b)
        mems_a = (body_a or {}).get("memories") or []
        mems_b = (body_b or {}).get("memories") or []
        leak = any("蓝色" in str(m.get("content", "")) or "Alice" in str(m.get("content", ""))
                   for m in mems_b)
        iso["isolation"] = {"a_status": st_a, "b_status": st_b,
                            "a_memory_count": len(mems_a),
                            "b_memory_count": len(mems_b),
                            "leak_to_b": leak}
        print(f"[iso] a={st_a}({len(mems_a)}) b={st_b}({len(mems_b)}) leak={leak}")
    else:
        iso["isolation"] = {"note": "JWT 未配置或签发失败，跳过隔离测试"}
        print("[iso] no JWT tokens (JWT_SECRET 未配置?) — skipped")
    out["jwt_isolation"] = iso

    # 4. 并发 chat（默认 6 并发，每路 2 次）
    if not args.skip_chat:
        qs = [SIMPLE_QUERY,
              "怎么申请保修？",
              "退货流程是什么？",
              "发票开错了怎么办？",
              "X-200 和 X-100 有什么区别？",
              "我的网关离线了怎么办？"]
        def one(q):
            st, body, _, _, el = _post(base, "/api/chat", {"message": q, "stream": False},
                                       timeout=240)
            reply = ""
            if isinstance(body, dict):
                replies = body.get("replies") or []
                reply = replies[-1].get("content", "") if replies else ""
            return {"q": q[:12], "status": st, "elapsed_s": el,
                    "reply_len": len(reply)}
        results: List[Dict] = []
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            for r in ex.map(one, qs * 2):
                results.append(r)
        ok_conc = [r for r in results if r["status"] == 200]
        lat = sorted(r["elapsed_s"] for r in ok_conc)
        def pct(p):
            if not lat:
                return None
            return round(lat[min(len(lat) - 1, int(len(lat) * p))], 2)
        out["concurrency"] = {"requests": len(results), "ok": len(ok_conc),
                              "success_rate": round(len(ok_conc) / len(results), 3)
                              if results else None,
                              "p50_s": pct(0.5), "p95_s": pct(0.95),
                              "max_s": round(max(lat), 2) if lat else None,
                              "details": results}
        print(f"[conc] ok={len(ok_conc)}/{len(results)} p50={pct(0.5)}s "
              f"p95={pct(0.95)}s max={round(max(lat), 2) if lat else None}s")
    else:
        out["concurrency"] = {"skipped": True}

    # 5. 成本估算（按回复字符数，中文 ≈1.5 char/token；响应带 usage 则直接用）
    total_chars = sum(len(r.get("reply_head", "")) for r in mem["steps"]) \
        + sum(len(r.get("reply_len", 0)) for r in out.get("concurrency", {}).get("details", [])
              if isinstance(r, dict))
    out["cost_estimate"] = {
        "note": "本地 llama.cpp 无计费；若接 API 按 ~1.5 char/token 估算",
        "approx_gen_tokens": int(total_chars / 1.5),
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS / f"system_eval_{ts}.json"
    json_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 系统级评测报告", "",
        f"- 时间: {out['timestamp']}",
        f"- Base: {base}",
        f"- healthz: {out['healthz']}",
        "",
        "## 1. 流式 TTFT", "",
        f"- 首 token 中位延迟: **{out['ttft']['first_token_median_s']}s** "
        f"（成功 {out['ttft']['success']}/3）",
        "",
        "## 2. 多轮上下文记忆", "",
        f"- 召回命中: **{out['multi_turn_memory'].get('recall_hit')}**",
        "",
        "## 3. JWT 隔离", "",
        f"- A 记忆数: {out['jwt_isolation']['isolation'].get('a_memory_count')} | "
        f"B 记忆数: {out['jwt_isolation']['isolation'].get('b_memory_count')} | "
        f"泄漏到 B: **{out['jwt_isolation']['isolation'].get('leak_to_b')}**",
        "",
        "## 4. 并发/延迟", "",
    ]
    conc = out.get("concurrency", {})
    if conc.get("skipped"):
        lines.append("- 跳过")
    else:
        lines += [f"- 请求: {conc.get('requests')} | 成功: {conc.get('ok')} "
                  f"| 成功率: {conc.get('success_rate')}",
                  f"- p50: {conc.get('p50_s')}s | p95: {conc.get('p95_s')}s "
                  f"| max: {conc.get('max_s')}s", ""]
    lines += ["## 5. 成本估算", "", f"- 本次评测生成 token 约: {out['cost_estimate']['approx_gen_tokens']}", ""]
    md_path = REPORTS / f"system_eval_{ts}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告: {md_path}")
    print(f"数据: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
