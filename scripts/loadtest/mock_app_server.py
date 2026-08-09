#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
压测器自测靶子 —— 纯标准库 ThreadingHTTPServer 模拟的"应用替身"。

用途（只有一个）：**验证 run_loadtest.py 本身**。它按已知常量行为响应：
  * /api/chat 非流式：固定 sleep(CHAT_DELAY_MS) 后返回 JSON
  * /api/chat stream=true：SSE，逐 token 吐出，总时长同样是 CHAT_DELAY_MS
  * /api/sessions：sleep(LIGHT_DELAY_MS) 后返回列表
  * /healthz：立即 200
  * /api/metrics：Prometheus 文本（给 --profile 采样用）
  * 全局并发上限 --max-concurrency：超了直接 429 + Retry-After
    （用来验证压测器把 429 单列、不计失败）

因为延迟是已知常量，可以反推压测器算出来的 P50/P95/QPS 对不对：
    理论 P50 ≈ CHAT_DELAY_MS，理论 QPS ≈ users / 平均延迟。

**它不是被测应用**：用它跑出来的数字只能证明压测工具可信，
不能当作 langgraph-customer-service-agent 的性能结论。

用法：
    python scripts/loadtest/mock_app_server.py --port 7899 \
        --chat-delay-ms 200 --max-concurrency 64
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = {
    "chat_delay_ms": 200.0,
    "light_delay_ms": 5.0,
    "tokens": 24,
    "max_concurrency": 0,      # 0 = 不限
}

_STATE = {"inflight": 0, "max_inflight": 0, "requests": 0, "rejected_429": 0}
_LOCK = threading.Lock()


class _Slot:
    """并发槽：超过 max_concurrency 时 acquire 失败（模拟应用层限流）。"""

    def __enter__(self):
        with _LOCK:
            limit = CONFIG["max_concurrency"]
            if limit and _STATE["inflight"] >= limit:
                _STATE["rejected_429"] += 1
                self.ok = False
                return self
            _STATE["inflight"] += 1
            _STATE["requests"] += 1
            _STATE["max_inflight"] = max(_STATE["max_inflight"],
                                         _STATE["inflight"])
            self.ok = True
            return self

    def __exit__(self, *exc):
        if getattr(self, "ok", False):
            with _LOCK:
                _STATE["inflight"] -= 1
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockApp/1.0"

    def log_message(self, fmt, *args):      # 静音：压测时日志会成为瓶颈
        pass

    # -- helpers --

    def _json(self, code: int, payload: dict, extra_headers=None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _429(self) -> None:
        self._json(429, {"error": "too many requests", "reason": "concurrency"},
                   {"Retry-After": "1"})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # -- routes --

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if path == "/api/sessions":
            with _Slot() as slot:
                if not slot.ok:
                    self._429()
                    return
                time.sleep(CONFIG["light_delay_ms"] / 1000.0)
                self._json(200, {"sessions": [{"session_id": "mock-1",
                                               "messages": 3}]})
            return
        if path == "/api/metrics":
            with _LOCK:
                snap = dict(_STATE)
            text = (
                "# HELP http_requests_total Total requests\n"
                "# TYPE http_requests_total counter\n"
                f"http_requests_total {snap['requests']}\n"
                "# HELP rate_limit_events_total 429 responses\n"
                "# TYPE rate_limit_events_total counter\n"
                f"rate_limit_events_total {snap['rejected_429']}\n"
                "# HELP active_sessions In-flight requests\n"
                "# TYPE active_sessions gauge\n"
                f"active_sessions {snap['inflight']}\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/api/chat":
            self._json(404, {"error": "not found"})
            return
        payload = self._read_body()
        with _Slot() as slot:
            if not slot.ok:
                self._429()
                return
            if payload.get("stream"):
                self._sse(payload)
            else:
                time.sleep(CONFIG["chat_delay_ms"] / 1000.0)
                self._json(200, {
                    "replies": ["mock reply: " + str(payload.get("message", ""))[:40]],
                    "session_id": payload.get("session_id", "mock"),
                    "intent": "consult"})

    def _sse(self, payload: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        n = max(1, int(CONFIG["tokens"]))
        per = (CONFIG["chat_delay_ms"] / 1000.0) / n

        def frame(obj: dict) -> None:
            chunk = ("data:" + json.dumps(obj, ensure_ascii=False) +
                     "\n\n").encode("utf-8")
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()

        frame({"progress": "retrieve"})
        for i in range(n):
            time.sleep(per)
            frame({"token": f"tok{i} "})
        frame({"done": True, "session_id": payload.get("session_id", "mock"),
               "intent": "consult"})
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="压测器自测用的模拟应用服务端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7899)
    ap.add_argument("--chat-delay-ms", type=float, default=200.0,
                    help="模拟一次 chat 的固定处理时间（= mock LLM 延迟）")
    ap.add_argument("--light-delay-ms", type=float, default=5.0)
    ap.add_argument("--tokens", type=int, default=24, help="SSE token 数")
    ap.add_argument("--max-concurrency", type=int, default=0,
                    help="并发上限，超出返回 429（0=不限）")
    args = ap.parse_args()

    CONFIG.update({"chat_delay_ms": args.chat_delay_ms,
                   "light_delay_ms": args.light_delay_ms,
                   "tokens": args.tokens,
                   "max_concurrency": args.max_concurrency})

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"[mock-app] listening on http://{args.host}:{args.port} "
          f"chat_delay={args.chat_delay_ms}ms "
          f"max_concurrency={args.max_concurrency or 'unlimited'}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with _LOCK:
            print(f"[mock-app] served={_STATE['requests']} "
                  f"max_inflight={_STATE['max_inflight']} "
                  f"rejected_429={_STATE['rejected_429']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
