# -*- coding: utf-8 -*-
"""
Locust 压测脚本 — langgraph-customer-service-agent (P5)。

依赖（不在 requirements.txt 内，压测机单独安装）：
    pip install locust

运行（100 并发梯度，10 分钟）：
    locust -f scripts/loadtest/locustfile.py --host http://localhost:7860 \
           --users 100 --spawn-rate 10 --run-time 10m --headless \
           --stop-timeout 60

场景混合（权重）：
    - 非流式 chat        weight=6
    - 流式 chat (SSE)    weight=3
    - rating             约 5%（chat 完成后按概率触发）
    - sessions/analytics weight=1（只读面板轮询）

验收断言（test_stop 时评估，失败进程退码非 0，可接 CI）：
    - POST /api/chat (non-stream) P95 < CHAT_P95_MS（默认 15000ms，LLM 场景）
    - 全局失败率 < 2%（429 限流响应不计失败——是预期保护行为）

环境变量：
    LOADTEST_API_KEY   （API_KEYS 配置时必填，走 X-API-Key 头）
    CHAT_P95_MS        P95 阈值毫秒（默认 15000）
    MAX_FAIL_RATIO     失败率阈值（默认 0.02）
"""
from __future__ import annotations

import json
import os
import random
import time

# ── locust import 守卫：仅提示，不做兜底（压测脚本必须在装了 locust 的
#    压测机上运行；应用容器/CI 单测环境不需要 import 本文件）──────────
try:
    from locust import HttpUser, task, between, events
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "locust is not installed. Run:  pip install locust\n"
        "(this file is only meant to be executed by the locust CLI)") from exc

CHAT_P95_MS = float(os.getenv("CHAT_P95_MS", "15000"))
MAX_FAIL_RATIO = float(os.getenv("MAX_FAIL_RATIO", "0.02"))
API_KEY = os.getenv("LOADTEST_API_KEY", "")

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


def _headers(extra=None):
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    if extra:
        h.update(extra)
    return h


class ChatUser(HttpUser):
    """模拟一个客服会话用户：多轮提问，偶尔结束会话/打分。"""

    wait_time = between(1, 4)

    def on_start(self):
        self.session_id = f"loadtest-{random.getrandbits(48):012x}"
        self.turns = 0

    def _pick_message(self) -> str:
        self.turns += 1
        if self.turns > 1 and random.random() < 0.15:
            return random.choice(ENDINGS)          # 触发 ending → 满意度分支
        return random.choice(QUESTIONS)

    # ── 非流式 chat（权重 6）─────────────────────────────
    @task(6)
    def chat_json(self):
        msg = self._pick_message()
        with self.client.post(
                "/api/chat",
                data=json.dumps({"message": msg, "session_id": self.session_id},
                                ensure_ascii=False).encode("utf-8"),
                headers=_headers(
                    {"X-Idempotency-Key": f"lt-{random.getrandbits(64):016x}"}),
                name="POST /api/chat [json]",
                catch_response=True) as resp:
            if resp.status_code == 429:
                resp.success()  # 限流是预期保护行为，不计失败
                return
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            try:
                body = resp.json()
            except Exception:
                resp.failure("non-JSON body")
                return
            if "error" in body and not body.get("replies"):
                resp.failure(f"app error: {body['error'][:80]}")
                return
            resp.success()
            if random.random() < 0.05:      # rating ≈ 5% of chats
                self._rate()

    # ── 流式 chat（权重 3）───────────────────────────────
    @task(3)
    def chat_stream(self):
        msg = self._pick_message()
        start = time.perf_counter()
        frames = 0
        got_done = False
        with self.client.post(
                "/api/chat",
                data=json.dumps({"message": msg, "session_id": self.session_id,
                                 "stream": True},
                                ensure_ascii=False).encode("utf-8"),
                headers=_headers(),
                name="POST /api/chat [sse]",
                stream=True,
                catch_response=True) as resp:
            if resp.status_code == 429:
                resp.success()
                return
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            try:
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    frames += 1
                    payload = json.loads(line[5:].strip())
                    if payload.get("done"):
                        got_done = True
                        break
                    if "error" in payload:
                        resp.failure(f"sse error: {payload['error'][:80]}")
                        return
                    if time.perf_counter() - start > 90:
                        resp.failure("sse stream exceeded 90s")
                        return
            except Exception as exc:
                resp.failure(f"sse parse: {exc}")
                return
            if got_done and frames > 0:
                resp.success()
            else:
                resp.failure(f"sse incomplete (frames={frames}, done={got_done})")

    # ── 只读面板（权重 1）───────────────────────────────
    @task(1)
    def dashboards(self):
        self.client.get("/api/sessions", headers=_headers(),
                        name="GET /api/sessions")
        self.client.get("/api/analytics", headers=_headers(),
                        name="GET /api/analytics")

    def _rate(self):
        self.client.post(
            "/api/rating",
            data=json.dumps({"session_id": self.session_id,
                             "message_index": self.turns,
                             "stars": random.choice([1, 3, 4, 5, 5])}),
            headers=_headers(),
            name="POST /api/rating")


# ── 验收断言：P95 与失败率 ───────────────────────────────
@events.test_stop.add_listener
def _assert_slo(environment, **kwargs):
    stats = environment.stats
    entry = stats.get("POST /api/chat [json]", "POST")
    failures = []
    if entry.num_requests:
        p95 = entry.get_response_time_percentile(0.95)
        print(f"[SLO] chat json: n={entry.num_requests} "
              f"p50={entry.median_response_time}ms p95={p95}ms")
        if p95 > CHAT_P95_MS:
            failures.append(f"chat P95 {p95}ms > {CHAT_P95_MS}ms")
    total = stats.total
    if total.num_requests:
        ratio = total.num_failures / total.num_requests
        print(f"[SLO] total: n={total.num_requests} failures={total.num_failures} "
              f"ratio={ratio:.3f}")
        if ratio > MAX_FAIL_RATIO:
            failures.append(f"failure ratio {ratio:.3f} > {MAX_FAIL_RATIO}")
    if failures:
        print("[SLO] FAILED: " + "; ".join(failures))
        environment.process_exit_code = 1
    else:
        print("[SLO] PASSED")
