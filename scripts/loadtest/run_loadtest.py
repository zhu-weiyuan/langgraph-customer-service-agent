#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
零依赖压测器 —— langgraph-customer-service-agent 应用层压测。

设计目标：证明的是**应用层**能力（异步化 / 限流 / 会话管理 / SSE），不是模型吞吐。
配合 MOCK_LLM=1 使用（见 agent/mock_llm.py 与 LOADTEST_README.md）。

特点：
  * 优先 httpx.AsyncClient（真并发、连接池、原生 SSE）；缺 httpx 时自动降级到
    标准库 http.client + 线程池 —— **零三方依赖也能跑**。
  * 场景按权重混合：/api/chat 非流式、/api/chat SSE、/api/sessions、/healthz。
  * 统计：总数 / 成功 / 失败 / 429（限流不算失败）/ QPS / P50 P90 P95 P99 /
    错误按状态码分布 / SSE 首 token 延迟（TTFT）/ 实际达到的并发（in-flight 采样）。
  * --profile：周期采样服务端 /api/metrics 与本机进程 CPU/RSS（psutil 可选）。
  * 输出：控制台表格 + 可选 --json / --csv。

用法：
    # 100 并发、跑 60s、30s 内爬坡
    python scripts/loadtest/run_loadtest.py --host http://127.0.0.1:7860 \
        --users 100 --duration 60 --ramp 30 --json reports/load_100.json

    # 只压健康检查（测纯 HTTP 栈上限，排除业务）
    python scripts/loadtest/run_loadtest.py --weights healthz=1 --users 200 --duration 20

    # 自测：先起内置模拟服务端，再打它（验证压测器本身）
    python scripts/loadtest/mock_app_server.py --port 7899 &
    python scripts/loadtest/run_loadtest.py --host http://127.0.0.1:7899 --users 50 --duration 10

退出码：0 正常；1 超过 --max-fail-ratio 或 P95 超 --slo-p95-ms（可接 CI gate）。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── 可选依赖守卫 ────────────────────────────────────────────
try:
    import httpx                                   # type: ignore
    HAVE_HTTPX = True
except ImportError:                                # pragma: no cover
    httpx = None                                   # type: ignore
    HAVE_HTTPX = False

try:
    import psutil                                  # type: ignore
    HAVE_PSUTIL = True
except ImportError:                                # pragma: no cover
    psutil = None                                  # type: ignore
    HAVE_PSUTIL = False


QUESTIONS = [
    "智能音箱怎么连接WiFi？",
    "蓝牙连不上手机怎么办？",
    "我要退货，昨天买的智能音箱有质量问题",
    "云服务怎么收费？",
    "智能家居套装都包含什么设备？",
    "发票怎么开？",
    "物流几天能到？",
    "音箱没有声音了，怎么排查？",
    "保修期是多久？",
    "你们最近有什么优惠活动吗？",
]
ENDINGS = ["谢谢", "好的，再见", "没问题了，谢谢"]

DEFAULT_WEIGHTS = {"chat": 6, "chat_sse": 3, "sessions": 1, "healthz": 1}


# ════════════════════════════════════════════════════════════
# 统计
# ════════════════════════════════════════════════════════════

@dataclass
class Sample:
    name: str            # 场景名
    t_start: float       # 相对开始时间（秒）
    latency_ms: float
    status: int          # HTTP 状态码；0 = 连接层异常
    ok: bool
    rate_limited: bool = False
    ttft_ms: Optional[float] = None     # SSE 首 token
    error: str = ""


def percentile(sorted_values: List[float], pct: float) -> float:
    """最近秩法（nearest-rank）分位数。空列表返回 0。"""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    rank = max(1, math.ceil(pct / 100.0 * n))       # nearest-rank: ceil(p·N)
    return sorted_values[min(rank, n) - 1]


class Collector:
    """线程/协程安全的样本收集器 + 并发量采样。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.samples: List[Sample] = []
        self.inflight = 0
        self.inflight_samples: List[int] = []
        self.max_inflight = 0
        self.t0 = time.perf_counter()

    def now(self) -> float:
        return time.perf_counter() - self.t0

    def add(self, s: Sample) -> None:
        with self._lock:
            self.samples.append(s)

    def enter(self) -> None:
        with self._lock:
            self.inflight += 1
            if self.inflight > self.max_inflight:
                self.max_inflight = self.inflight

    def exit(self) -> None:
        with self._lock:
            self.inflight -= 1

    def sample_inflight(self) -> None:
        with self._lock:
            self.inflight_samples.append(self.inflight)

    # -- 汇总 --

    def summarize(self, wall_seconds: float) -> Dict[str, Any]:
        with self._lock:
            samples = list(self.samples)
            inflight = list(self.inflight_samples)
            max_inflight = self.max_inflight

        def block(rows: List[Sample]) -> Dict[str, Any]:
            lat = sorted(s.latency_ms for s in rows)
            ok = sum(1 for s in rows if s.ok)
            rl = sum(1 for s in rows if s.rate_limited)
            fail = len(rows) - ok - rl
            ttft = sorted(s.ttft_ms for s in rows if s.ttft_ms is not None)
            return {
                "requests": len(rows),
                "ok": ok,
                "rate_limited_429": rl,
                "failed": fail,
                "fail_ratio": round(fail / len(rows), 4) if rows else 0.0,
                "qps": round(len(rows) / wall_seconds, 2) if wall_seconds else 0.0,
                "avg_ms": round(statistics.fmean(lat), 1) if lat else 0.0,
                "p50_ms": round(percentile(lat, 50), 1),
                "p90_ms": round(percentile(lat, 90), 1),
                "p95_ms": round(percentile(lat, 95), 1),
                "p99_ms": round(percentile(lat, 99), 1),
                "max_ms": round(lat[-1], 1) if lat else 0.0,
                "ttft_p50_ms": round(percentile(ttft, 50), 1) if ttft else None,
                "ttft_p95_ms": round(percentile(ttft, 95), 1) if ttft else None,
            }

        by_scenario: Dict[str, Any] = {}
        for name in sorted({s.name for s in samples}):
            by_scenario[name] = block([s for s in samples if s.name == name])

        status_dist: Dict[str, int] = {}
        errors: Dict[str, int] = {}
        for s in samples:
            key = str(s.status) if s.status else "conn_error"
            status_dist[key] = status_dist.get(key, 0) + 1
            if not s.ok and s.error:
                errors[s.error[:80]] = errors.get(s.error[:80], 0) + 1

        return {
            "wall_seconds": round(wall_seconds, 2),
            "overall": block(samples),
            "by_scenario": by_scenario,
            "status_distribution": status_dist,
            "error_distribution": errors,
            "concurrency": {
                "max_inflight_observed": max_inflight,
                "avg_inflight_observed": (round(statistics.fmean(inflight), 1)
                                          if inflight else 0.0),
                "inflight_samples": len(inflight),
            },
        }


# ════════════════════════════════════════════════════════════
# 场景选择
# ════════════════════════════════════════════════════════════

class ScenarioPicker:
    def __init__(self, weights: Dict[str, int]) -> None:
        self.names = [n for n, w in weights.items() if w > 0]
        self.weights = [weights[n] for n in self.names]
        if not self.names:
            raise ValueError("all scenario weights are zero")

    def pick(self, rng: random.Random) -> str:
        return rng.choices(self.names, weights=self.weights, k=1)[0]


def pick_message(rng: random.Random, turns: int) -> str:
    if turns > 1 and rng.random() < 0.15:
        return rng.choice(ENDINGS)      # 触发 ending → 满意度/收尾分支
    return rng.choice(QUESTIONS)


def build_headers(api_key: str, idem: bool = False) -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["X-API-Key"] = api_key
    if idem:
        h["X-Idempotency-Key"] = f"lt-{random.getrandbits(64):016x}"
    return h


# ════════════════════════════════════════════════════════════
# 后端 A：asyncio + httpx
# ════════════════════════════════════════════════════════════

async def _run_async(cfg: argparse.Namespace, col: Collector) -> None:
    import asyncio

    picker = ScenarioPicker(cfg.weights)
    stop_at = time.perf_counter() + cfg.duration
    limits = httpx.Limits(max_connections=cfg.users * 2,
                          max_keepalive_connections=cfg.users * 2)
    timeout = httpx.Timeout(cfg.timeout)

    async def sampler() -> None:
        while time.perf_counter() < stop_at:
            col.sample_inflight()
            await asyncio.sleep(0.1)

    async def one_request(client: "httpx.AsyncClient", name: str,
                          session_id: str, turns: int) -> None:
        started = time.perf_counter()
        rel = col.now()
        col.enter()
        status, ok, rl, ttft, err = 0, False, False, None, ""
        try:
            if name == "healthz":
                r = await client.get("/healthz")
                status = r.status_code
                ok = status == 200
            elif name == "sessions":
                r = await client.get("/api/sessions",
                                     headers=build_headers(cfg.api_key))
                status = r.status_code
                ok = status == 200
                rl = status == 429
            elif name == "chat":
                payload = {"message": pick_message(random, turns),
                           "session_id": session_id}
                r = await client.post("/api/chat", json=payload,
                                      headers=build_headers(cfg.api_key, True))
                status = r.status_code
                rl = status == 429
                ok = status == 200
                if ok:
                    try:
                        body = r.json()
                        if "error" in body and not body.get("replies"):
                            ok, err = False, f"app error: {str(body['error'])[:60]}"
                    except Exception:
                        ok, err = False, "non-JSON body"
            else:   # chat_sse
                payload = {"message": pick_message(random, turns),
                           "session_id": session_id, "stream": True}
                async with client.stream(
                        "POST", "/api/chat", json=payload,
                        headers=build_headers(cfg.api_key, True)) as r:
                    status = r.status_code
                    rl = status == 429
                    if status == 200:
                        got_done, got_err = False, ""
                        async for line in r.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            if ttft is None:
                                ttft = (time.perf_counter() - started) * 1000
                            try:
                                frame = json.loads(line[5:].strip())
                            except Exception:
                                continue
                            if frame.get("error"):
                                got_err = str(frame["error"])[:60]
                            if frame.get("done"):
                                got_done = True
                        ok = got_done and not got_err
                        err = got_err or ("" if got_done else "SSE without done frame")
                    else:
                        await r.aread()
        except Exception as exc:                       # 连接层失败
            status, ok, err = 0, False, f"{type(exc).__name__}: {exc}"[:80]
        finally:
            col.exit()
        if rl:
            ok = False                                 # 429 单独统计，不计成功也不计失败
        col.add(Sample(name=name, t_start=rel,
                       latency_ms=(time.perf_counter() - started) * 1000,
                       status=status, ok=ok, rate_limited=rl, ttft_ms=ttft,
                       error=err if not ok and not rl else ""))

    async def user(uid: int, delay: float) -> None:
        await asyncio.sleep(delay)
        rng = random.Random(uid * 7919 + 13)
        session_id = f"loadtest-{uid}-{rng.getrandbits(32):08x}"
        turns = 0
        async with httpx.AsyncClient(base_url=cfg.host, timeout=timeout,
                                     limits=limits,
                                     follow_redirects=True) as client:
            while time.perf_counter() < stop_at:
                turns += 1
                await one_request(client, picker.pick(rng), session_id, turns)
                think = rng.uniform(cfg.think_min, cfg.think_max)
                if think > 0:
                    await asyncio.sleep(think)

    ramp_step = (cfg.ramp / cfg.users) if cfg.users else 0.0
    tasks = [asyncio.create_task(user(i, i * ramp_step))
             for i in range(cfg.users)]
    tasks.append(asyncio.create_task(sampler()))
    if cfg.profile:
        tasks.append(asyncio.create_task(_profile_loop_async(cfg, stop_at)))
    await asyncio.gather(*tasks, return_exceptions=True)


# ════════════════════════════════════════════════════════════
# 后端 B：标准库 http.client + 线程（零三方依赖）
# ════════════════════════════════════════════════════════════

def _stdlib_request(conn_factory, method: str, path: str,
                    body: Optional[bytes], headers: Dict[str, str],
                    sse: bool) -> Tuple[int, bool, Optional[float], str, bool]:
    """返回 (status, ok, ttft_ms, error, done_seen)。"""
    started = time.perf_counter()
    conn = conn_factory()
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        if status != 200:
            resp.read()
            return status, False, None, f"HTTP {status}", False
        if not sse:
            raw = resp.read()
            return status, True, None, "", False
        ttft, done, err = None, False, ""
        for raw_line in resp:
            line = raw_line.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            if ttft is None:
                ttft = (time.perf_counter() - started) * 1000
            try:
                frame = json.loads(line[5:].strip())
            except Exception:
                continue
            if frame.get("error"):
                err = str(frame["error"])[:60]
            if frame.get("done"):
                done = True
        return status, (done and not err), ttft, err, done
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _run_threads(cfg: argparse.Namespace, col: Collector) -> None:
    import http.client
    from urllib.parse import urlparse

    parsed = urlparse(cfg.host)
    host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https"
                                                  else 80)
    https = parsed.scheme == "https"
    picker = ScenarioPicker(cfg.weights)
    stop_at = time.perf_counter() + cfg.duration

    def conn_factory():
        if https:
            return http.client.HTTPSConnection(host, port, timeout=cfg.timeout)
        return http.client.HTTPConnection(host, port, timeout=cfg.timeout)

    def user(uid: int) -> None:
        time.sleep((cfg.ramp / cfg.users) * uid if cfg.users else 0)
        rng = random.Random(uid * 7919 + 13)
        session_id = f"loadtest-{uid}-{rng.getrandbits(32):08x}"
        turns = 0
        while time.perf_counter() < stop_at:
            turns += 1
            name = picker.pick(rng)
            started = time.perf_counter()
            rel = col.now()
            col.enter()
            status, ok, ttft, err = 0, False, None, ""
            try:
                if name == "healthz":
                    status, ok, ttft, err, _ = _stdlib_request(
                        conn_factory, "GET", "/healthz", None,
                        build_headers(cfg.api_key), False)
                elif name == "sessions":
                    status, ok, ttft, err, _ = _stdlib_request(
                        conn_factory, "GET", "/api/sessions", None,
                        build_headers(cfg.api_key), False)
                else:
                    sse = name == "chat_sse"
                    payload = {"message": pick_message(rng, turns),
                               "session_id": session_id}
                    if sse:
                        payload["stream"] = True
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    status, ok, ttft, err, _ = _stdlib_request(
                        conn_factory, "POST", "/api/chat", body,
                        build_headers(cfg.api_key, True), sse)
            except Exception as exc:
                status, ok, err = 0, False, f"{type(exc).__name__}: {exc}"[:80]
            finally:
                col.exit()
            rl = status == 429
            col.add(Sample(name=name, t_start=rel,
                           latency_ms=(time.perf_counter() - started) * 1000,
                           status=status, ok=ok and not rl, rate_limited=rl,
                           ttft_ms=ttft, error=err if not ok and not rl else ""))
            think = rng.uniform(cfg.think_min, cfg.think_max)
            if think > 0:
                time.sleep(think)

    def sampler() -> None:
        while time.perf_counter() < stop_at:
            col.sample_inflight()
            time.sleep(0.1)

    threads = [threading.Thread(target=user, args=(i,), daemon=True)
               for i in range(cfg.users)]
    threads.append(threading.Thread(target=sampler, daemon=True))
    if cfg.profile:
        threads.append(threading.Thread(target=_profile_loop_sync,
                                        args=(cfg, stop_at), daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=cfg.duration + cfg.timeout + 10)


# ════════════════════════════════════════════════════════════
# --profile：服务端 metrics + 本机进程资源
# ════════════════════════════════════════════════════════════

PROFILE_SAMPLES: List[Dict[str, Any]] = []


def _proc_snapshot(name_filter: str) -> Dict[str, Any]:
    if not HAVE_PSUTIL:
        return {"psutil": False}
    rows = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            if name_filter and name_filter not in cmd:
                continue
            rows.append({"pid": p.info["pid"],
                         "cpu_percent": p.cpu_percent(interval=None),
                         "rss_mb": round(p.memory_info().rss / 1048576, 1)})
        except Exception:
            continue
    return {"psutil": True, "matched": len(rows),
            "cpu_percent_total": round(sum(r["cpu_percent"] for r in rows), 1),
            "rss_mb_total": round(sum(r["rss_mb"] for r in rows), 1),
            "procs": rows[:16]}


def _parse_prom(text: str, keys: Tuple[str, ...]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        for key in keys:
            if line.startswith(key):
                parts = line.rsplit(" ", 1)
                if len(parts) == 2:
                    try:
                        out[parts[0]] = float(parts[1])
                    except ValueError:
                        pass
    return out


METRIC_KEYS = ("http_requests_total", "rate_limit_events_total",
               "llm_requests_total", "circuit_breaker_state",
               "active_sessions")


def _profile_loop_sync(cfg: argparse.Namespace, stop_at: float) -> None:
    import urllib.request
    while time.perf_counter() < stop_at:
        snap: Dict[str, Any] = {"t": round(time.perf_counter(), 2),
                                "proc": _proc_snapshot(cfg.proc_filter)}
        try:
            with urllib.request.urlopen(cfg.host.rstrip("/") + "/api/metrics",
                                        timeout=5) as r:
                snap["metrics"] = _parse_prom(
                    r.read().decode("utf-8", "ignore"), METRIC_KEYS)
        except Exception as exc:
            snap["metrics_error"] = str(exc)[:80]
        PROFILE_SAMPLES.append(snap)
        time.sleep(cfg.profile_interval)


async def _profile_loop_async(cfg: argparse.Namespace, stop_at: float) -> None:
    import asyncio
    while time.perf_counter() < stop_at:
        snap: Dict[str, Any] = {"t": round(time.perf_counter(), 2),
                                "proc": _proc_snapshot(cfg.proc_filter)}
        try:
            async with httpx.AsyncClient(base_url=cfg.host, timeout=5) as c:
                r = await c.get("/api/metrics")
                snap["metrics"] = _parse_prom(r.text, METRIC_KEYS)
        except Exception as exc:
            snap["metrics_error"] = str(exc)[:80]
        PROFILE_SAMPLES.append(snap)
        await asyncio.sleep(cfg.profile_interval)


# ════════════════════════════════════════════════════════════
# 报告输出
# ════════════════════════════════════════════════════════════

def _fmt_row(cells: List[str], widths: List[int]) -> str:
    return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()


def print_report(report: Dict[str, Any], cfg: argparse.Namespace) -> None:
    o = report["overall"]
    print("")
    print("=" * 78)
    print("LOAD TEST REPORT")
    print("=" * 78)
    print(f"target        : {cfg.host}")
    print(f"backend       : {report['meta']['backend']}")
    print(f"users         : {cfg.users}   ramp: {cfg.ramp}s   "
          f"duration: {cfg.duration}s   think: "
          f"{cfg.think_min}-{cfg.think_max}s")
    print(f"weights       : " + ", ".join(f"{k}={v}" for k, v in
                                          cfg.weights.items() if v))
    print(f"wall time     : {report['wall_seconds']}s")
    print(f"mock_llm env  : MOCK_LLM={os.environ.get('MOCK_LLM', '(unset)')} "
          f"MOCK_LLM_DELAY_MS={os.environ.get('MOCK_LLM_DELAY_MS', '(unset)')}")
    print("-" * 78)
    print(f"requests      : {o['requests']}   ok: {o['ok']}   "
          f"failed: {o['failed']}   429(rate-limited): {o['rate_limited_429']}")
    print(f"throughput    : {o['qps']} req/s")
    print(f"fail ratio    : {o['fail_ratio'] * 100:.2f}%  "
          f"(429 excluded — 限流是预期保护行为)")
    print(f"concurrency   : max in-flight {report['concurrency']['max_inflight_observed']}"
          f" / avg {report['concurrency']['avg_inflight_observed']}"
          f" (configured users={cfg.users})")
    print("-" * 78)

    headers = ["scenario", "reqs", "ok", "fail", "429", "qps",
               "p50", "p90", "p95", "p99", "max", "ttft_p95"]
    rows = []
    for name, b in list(report["by_scenario"].items()) + [("ALL", o)]:
        rows.append([name, str(b["requests"]), str(b["ok"]), str(b["failed"]),
                     str(b["rate_limited_429"]), f"{b['qps']:.1f}",
                     f"{b['p50_ms']:.0f}", f"{b['p90_ms']:.0f}",
                     f"{b['p95_ms']:.0f}", f"{b['p99_ms']:.0f}",
                     f"{b['max_ms']:.0f}",
                     "-" if b["ttft_p95_ms"] is None else f"{b['ttft_p95_ms']:.0f}"])
    widths = [max(len(headers[i]), max((len(r[i]) for r in rows), default=0))
              for i in range(len(headers))]
    print(_fmt_row(headers, widths))
    print(_fmt_row(["-" * w for w in widths], widths))
    for r in rows:
        print(_fmt_row(r, widths))
    print("(latency in ms)")
    print("-" * 78)
    print("status distribution: " + (", ".join(
        f"{k}={v}" for k, v in sorted(report["status_distribution"].items()))
        or "(none)"))
    if report["error_distribution"]:
        print("errors:")
        for k, v in sorted(report["error_distribution"].items(),
                           key=lambda kv: -kv[1])[:10]:
            print(f"  {v:6d}  {k}")
    if report.get("profile"):
        last = report["profile"][-1]
        print("-" * 78)
        print(f"profile samples: {len(report['profile'])}  "
              f"(last) proc={last.get('proc', {}).get('rss_mb_total')}MB RSS / "
              f"{last.get('proc', {}).get('cpu_percent_total')}% CPU")
        if last.get("metrics"):
            for k, v in list(last["metrics"].items())[:8]:
                print(f"  {k} = {v}")
    print("=" * 78)


def write_csv(path: str, report: Dict[str, Any]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["scenario", "requests", "ok", "failed", "rate_limited_429",
                    "fail_ratio", "qps", "avg_ms", "p50_ms", "p90_ms",
                    "p95_ms", "p99_ms", "max_ms", "ttft_p50_ms", "ttft_p95_ms"])
        for name, b in list(report["by_scenario"].items()) + \
                [("ALL", report["overall"])]:
            w.writerow([name, b["requests"], b["ok"], b["failed"],
                        b["rate_limited_429"], b["fail_ratio"], b["qps"],
                        b["avg_ms"], b["p50_ms"], b["p90_ms"], b["p95_ms"],
                        b["p99_ms"], b["max_ms"], b["ttft_p50_ms"],
                        b["ttft_p95_ms"]])


# ════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════

def parse_weights(raw: List[str]) -> Dict[str, int]:
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    out = {k: 0 for k in DEFAULT_WEIGHTS}
    for item in raw:
        for pair in item.split(","):
            if not pair.strip():
                continue
            name, _, value = pair.partition("=")
            name = name.strip()
            if name not in DEFAULT_WEIGHTS:
                raise SystemExit(f"unknown scenario '{name}'; "
                                 f"valid: {', '.join(DEFAULT_WEIGHTS)}")
            out[name] = int(value or 1)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="零依赖应用层压测器（配合 MOCK_LLM=1 使用）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--host", default=os.getenv("LOADTEST_HOST",
                                               "http://127.0.0.1:7860"))
    p.add_argument("--users", type=int, default=100, help="并发虚拟用户数")
    p.add_argument("--duration", type=float, default=60.0, help="压测时长（秒）")
    p.add_argument("--ramp", type=float, default=10.0, help="爬坡时长（秒）")
    p.add_argument("--think-min", type=float, default=0.0,
                   help="每次请求后的思考时间下限（秒）")
    p.add_argument("--think-max", type=float, default=0.0,
                   help="每次请求后的思考时间上限（秒）")
    p.add_argument("--timeout", type=float, default=60.0, help="单请求超时（秒）")
    p.add_argument("--weights", action="append", default=[],
                   metavar="chat=6,chat_sse=3,sessions=1,healthz=1",
                   help="场景权重，可重复传")
    p.add_argument("--api-key", default=os.getenv("LOADTEST_API_KEY", ""),
                   help="X-API-Key（应用配置了 API_KEYS 时必填）")
    p.add_argument("--backend", choices=["auto", "httpx", "threads"],
                   default="auto", help="httpx=asyncio；threads=纯标准库")
    p.add_argument("--profile", action="store_true",
                   help="周期采样 /api/metrics + 进程 CPU/RSS（psutil 可选）")
    p.add_argument("--profile-interval", type=float, default=2.0)
    p.add_argument("--proc-filter", default="uvicorn",
                   help="--profile 时匹配进程命令行的关键字")
    p.add_argument("--json", dest="json_out", default="",
                   help="报告写入 JSON 文件")
    p.add_argument("--csv", dest="csv_out", default="",
                   help="分场景汇总写入 CSV 文件")
    p.add_argument("--slo-p95-ms", type=float, default=0.0,
                   help="非 0 时：ALL 的 P95 超过它则退码 1")
    p.add_argument("--max-fail-ratio", type=float, default=1.0,
                   help="失败率超过它则退码 1（429 不计失败）")
    p.add_argument("--label", default="", help="写进报告的备注标签")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    cfg = build_parser().parse_args(argv)
    cfg.weights = parse_weights(cfg.weights)
    if cfg.users < 1:
        raise SystemExit("--users must be >= 1")
    if cfg.think_max < cfg.think_min:
        cfg.think_max = cfg.think_min

    backend = cfg.backend
    if backend == "auto":
        backend = "httpx" if HAVE_HTTPX else "threads"
    if backend == "httpx" and not HAVE_HTTPX:
        raise SystemExit("httpx not installed; use --backend threads")

    print(f"[loadtest] backend={backend} users={cfg.users} "
          f"duration={cfg.duration}s ramp={cfg.ramp}s host={cfg.host}")
    if not os.environ.get("MOCK_LLM"):
        print("[loadtest] WARNING: MOCK_LLM is not set in this shell. If the "
              "server is running against a real LLM, the numbers below measure "
              "MODEL throughput, not application capacity.")

    col = Collector()
    t0 = time.perf_counter()
    if backend == "httpx":
        import asyncio
        asyncio.run(_run_async(cfg, col))
    else:
        _run_threads(cfg, col)
    wall = time.perf_counter() - t0

    report = col.summarize(wall)
    report["meta"] = {
        "backend": backend,
        "host": cfg.host,
        "users": cfg.users,
        "ramp_s": cfg.ramp,
        "duration_s": cfg.duration,
        "weights": cfg.weights,
        "label": cfg.label,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mock_llm": os.environ.get("MOCK_LLM", ""),
        "mock_llm_delay_ms": os.environ.get("MOCK_LLM_DELAY_MS", ""),
        "mock_embedding": os.environ.get("MOCK_EMBEDDING", ""),
        "psutil": HAVE_PSUTIL,
    }
    if cfg.profile:
        report["profile"] = PROFILE_SAMPLES

    print_report(report, cfg)

    if cfg.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(cfg.json_out)),
                    exist_ok=True)
        with open(cfg.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"[loadtest] JSON report -> {cfg.json_out}")
    if cfg.csv_out:
        os.makedirs(os.path.dirname(os.path.abspath(cfg.csv_out)),
                    exist_ok=True)
        write_csv(cfg.csv_out, report)
        print(f"[loadtest] CSV report  -> {cfg.csv_out}")

    o = report["overall"]
    failed_gate = False
    if o["fail_ratio"] > cfg.max_fail_ratio:
        print(f"[loadtest] SLO FAIL: fail_ratio {o['fail_ratio']} > "
              f"{cfg.max_fail_ratio}")
        failed_gate = True
    if cfg.slo_p95_ms and o["p95_ms"] > cfg.slo_p95_ms:
        print(f"[loadtest] SLO FAIL: p95 {o['p95_ms']}ms > {cfg.slo_p95_ms}ms")
        failed_gate = True
    if o["requests"] == 0:
        print("[loadtest] SLO FAIL: zero requests issued")
        failed_gate = True
    return 1 if failed_gate else 0


if __name__ == "__main__":
    sys.exit(main())
