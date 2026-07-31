# -*- coding: utf-8 -*-
"""P1-B 纯 stdlib 单测：LLM 网关、限流、确定性 bug 修复。

不依赖 redis / requests 服务；Redis 用内存 Fake 注入，httpx 响应用 Fake 对象注入。
运行：python -m unittest tests.test_p1b_pure -v
"""

import asyncio
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent import llm_gateway as gw
from agent import rate_limiter as rl
from agent.circuit_breaker import CircuitBreaker, CircuitOpenError
from agent.llm_client import LLMClient


# ─────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────

class FakeResponse:
    """模拟 httpx.Response 的最小接口。"""

    def __init__(self, status_code, json_data=None, headers=None, text=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._json)

    def json(self):
        return self._json


def ok_response(content="你好", prompt_tokens=10, completion_tokens=5,
                finish_reason="stop"):
    return FakeResponse(200, {
        "choices": [{"message": {"content": content},
                     "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens},
    })


class FakeHTTPClient:
    """脚本化的 AsyncClient 替身：按顺序弹出预置响应/异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "payload": json, "timeout": timeout})
        if not self.script:
            raise AssertionError("FakeHTTPClient script exhausted")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeAsyncRedis:
    """内存 Fake：实现 rate_limiter Lua 脚本的语义契约。"""

    def __init__(self):
        self.buckets = {}
        self.fail = False
        self.eval_count = 0

    async def eval(self, script, numkeys, *args):
        if self.fail:
            raise ConnectionError("redis down (fake)")
        self.eval_count += 1
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        now = float(argv[0])
        cost = float(argv[1])
        tokens = []
        for i, key in enumerate(keys):
            rate = float(argv[2 + 2 * i])
            cap = float(argv[3 + 2 * i])
            cur, ts = self.buckets.get(key, (cap, now))
            cur = min(cap, cur + max(0.0, now - ts) * rate)
            if cur < cost:
                retry = (cost - cur) / rate if rate > 0 else 0.0
                return [0, i + 1, math.ceil(retry * 1000)]
            tokens.append(cur)
        for i, key in enumerate(keys):
            self.buckets[key] = (tokens[i] - cost, now)
        return [1]


def fast_policy(**kw):
    defaults = dict(max_attempts=3, base_delay=0.0, max_delay=0.0,
                    attempt_timeout=2.0, total_deadline=5.0)
    defaults.update(kw)
    return gw.RetryPolicy(**defaults)


def two_models():
    return [
        gw.ModelProfile(name="m-flag", provider="p1", base_url="http://a/v1",
                        api_key="k1", model_id="deepseek-chat",
                        context_window=64000, max_output=1024, tier="flagship"),
        gw.ModelProfile(name="m-bal", provider="p2", base_url="http://b/v1",
                        api_key="k2", model_id="qwen-plus",
                        context_window=32000, max_output=1024, tier="balanced"),
    ]


def make_gateway(script, **kw):
    client = FakeHTTPClient(script)
    gateway = gw.LLMGateway(models=two_models(), http_client=client,
                            retry_policy=fast_policy(), **kw)
    return gateway, client


MSGS = [{"role": "user", "content": "退款政策是什么？"}]


# ─────────────────────────────────────────────────────
# 1. 限流：多桶扣减 + fail-closed 降级
# ─────────────────────────────────────────────────────

class TestRateLimiter(unittest.IsolatedAsyncioTestCase):

    def _limiter(self, fake, **kw):
        buckets = {
            "global":  rl.BucketConfig(rate=100.0, capacity=100),
            "ip":      rl.BucketConfig(rate=10.0, capacity=10),
            "user":    rl.BucketConfig(rate=2.0, capacity=4),
            "session": rl.BucketConfig(rate=1.0, capacity=2),
        }
        self.now = 1000.0
        return rl.RedisTokenBucketLimiter(
            redis_client=fake, buckets=buckets, clock=lambda: self.now, **kw)

    async def test_multi_bucket_deduction_and_layer_report(self):
        fake = FakeAsyncRedis()
        limiter = self._limiter(fake)
        # session 桶容量 2：前两次通过并同时扣减全部四层
        await limiter.acquire(user_id="u1", ip="1.2.3.4", session_id="s1")
        await limiter.acquire(user_id="u1", ip="1.2.3.4", session_id="s1")
        self.assertEqual(fake.eval_count, 2)
        # 每层桶都被扣减
        self.assertAlmostEqual(fake.buckets["rl:global:all"][0], 98.0)
        self.assertAlmostEqual(fake.buckets["rl:user:u1"][0], 2.0)
        self.assertAlmostEqual(fake.buckets["rl:session:s1"][0], 0.0)
        # 第三次：session 层不足 → 拒绝且不扣减其他层
        with self.assertRaises(rl.RateLimitExceeded) as ctx:
            await limiter.acquire(user_id="u1", ip="1.2.3.4", session_id="s1")
        self.assertEqual(ctx.exception.layer, "session")
        self.assertGreater(ctx.exception.retry_after, 0)
        self.assertAlmostEqual(fake.buckets["rl:user:u1"][0], 2.0)  # 未被扣减
        # 换会话：session 桶独立，通过
        await limiter.acquire(user_id="u1", ip="1.2.3.4", session_id="s2")
        # 令牌随时间恢复
        self.now += 5.0
        await limiter.acquire(user_id="u1", ip="1.2.3.4", session_id="s1")

    async def test_fail_closed_degradation(self):
        fake = FakeAsyncRedis()
        degrade_events = []
        local = rl.LocalConservativeLimiter(
            buckets={"user": rl.BucketConfig(rate=0.4, capacity=4),
                     "global": rl.BucketConfig(rate=100.0, capacity=100)},
            window_seconds=10.0, conservative_factor=0.5,
            clock=lambda: 0.0)
        limiter = self._limiter(fake, degrade_callback=degrade_events.append,
                                local_limiter=local)
        fake.fail = True
        # user 层本地保守限额 = 0.4*10*0.5 = 2：两次通过后第三次拒绝（而非放行）
        await limiter.acquire(user_id="u1")
        await limiter.acquire(user_id="u1")
        with self.assertRaises(rl.RateLimitExceeded) as ctx:
            await limiter.acquire(user_id="u1")
        self.assertEqual(ctx.exception.layer, "user")
        self.assertEqual(len(degrade_events), 3)          # 每次降级都上报指标
        stats = limiter.get_stats()
        self.assertTrue(stats["degraded"])
        self.assertEqual(stats["degraded_count"], 3)
        # Redis 恢复后回主路径
        fake.fail = False
        await limiter.acquire(user_id="u1")
        self.assertFalse(limiter.get_stats()["degraded"])

    async def test_local_limiter_cleanup_prevents_leak(self):
        clock = {"t": 0.0}
        local = rl.LocalConservativeLimiter(
            buckets={"user": rl.BucketConfig(rate=10.0, capacity=10)},
            window_seconds=1.0, cleanup_interval=5.0,
            clock=lambda: clock["t"])
        for i in range(50):
            local.acquire({"user": f"u{i}"})
        self.assertEqual(local.tracked_keys(), 50)
        clock["t"] = 10.0                                  # 窗口+清理间隔均已过
        local.acquire({"user": "fresh"})
        self.assertEqual(local.tracked_keys(), 1)          # 过期 key 已整体清理

    async def test_concurrency_gate_real_semaphore(self):
        limiter = rl.RedisTokenBucketLimiter(redis_client=FakeAsyncRedis(),
                                             max_concurrency=1)
        async with limiter.concurrency():
            self.assertEqual(limiter.get_stats()["active_concurrency"], 1)
            with self.assertRaises(rl.RateLimitExceeded) as ctx:
                async with limiter.concurrency(timeout=0.05):
                    pass
            self.assertEqual(ctx.exception.layer, "concurrency")
        # 释放后可再次获取
        async with limiter.concurrency(timeout=0.05):
            pass
        self.assertEqual(limiter.get_stats()["active_concurrency"], 0)


# ─────────────────────────────────────────────────────
# 2. 网关：幂等命中
# ─────────────────────────────────────────────────────

class TestIdempotency(unittest.IsolatedAsyncioTestCase):

    async def test_idempotency_hit_returns_cached_response(self):
        gateway, client = make_gateway([ok_response("答案A")])
        req1 = gw.GatewayRequest(messages=MSGS, tenant_id="premium",
                                 idempotency_key="idem-1")
        r1 = await gateway.chat(req1)
        self.assertEqual(r1.content, "答案A")
        self.assertFalse(r1.idempotent_replay)
        # 第二次同 key：不再打模型（script 已空，若发请求会 AssertionError）
        req2 = gw.GatewayRequest(messages=MSGS, tenant_id="premium",
                                 idempotency_key="idem-1")
        r2 = await gateway.chat(req2)
        self.assertTrue(r2.idempotent_replay)
        self.assertEqual(r2.content, "答案A")
        self.assertEqual(len(client.calls), 1)


# ─────────────────────────────────────────────────────
# 3. 预算：日期重置 + reconcile
# ─────────────────────────────────────────────────────

class TestTokenBudget(unittest.IsolatedAsyncioTestCase):

    async def test_reserve_reconcile_and_daily_reset(self):
        clock = {"t": 1_700_000_000.0}
        budget = gw.TokenBudgetManager(limits={"free": 150},
                                       clock=lambda: clock["t"])
        # estimate → reserve
        rid = await budget.reserve("free", 100)
        self.assertEqual(await budget.get_usage("free"), 100)
        # 真实 usage → reconcile（多退少补：实际 120）
        await budget.reconcile(rid, 120)
        self.assertEqual(await budget.get_usage("free"), 120)
        # 再要 100 超限（120+100>150），且回滚不留脏数据
        with self.assertRaises(gw.BudgetExceededError) as ctx:
            await budget.reserve("free", 100)
        self.assertEqual(ctx.exception.tenant_id, "free")
        self.assertEqual(await budget.get_usage("free"), 120)
        # 跨自然日：key 含 YYYY-MM-DD，自动重置
        clock["t"] += 86400
        rid2 = await budget.reserve("free", 100)
        self.assertEqual(await budget.get_usage("free"), 100)
        # 失败路径 release = reconcile(rid, 0)
        await budget.release(rid2)
        self.assertEqual(await budget.get_usage("free"), 0)

    async def test_gateway_reconciles_with_real_usage(self):
        budget = gw.TokenBudgetManager(limits={"premium": 100_000})
        gateway, _ = make_gateway(
            [ok_response(prompt_tokens=77, completion_tokens=33)], budget=budget)
        await gateway.chat(gw.GatewayRequest(messages=MSGS, tenant_id="premium"))
        self.assertEqual(await budget.get_usage("premium"), 110)  # 77+33，非估算值

    async def test_budget_released_when_all_models_fail(self):
        budget = gw.TokenBudgetManager(limits={"premium": 100_000})
        gateway, _ = make_gateway([FakeResponse(500)] * 6, budget=budget)
        with self.assertRaises(gw.AllModelsFailedError):
            await gateway.chat(gw.GatewayRequest(messages=MSGS, tenant_id="premium"))
        self.assertEqual(await budget.get_usage("premium"), 0)   # 预占已释放


# ─────────────────────────────────────────────────────
# 4. 重试分流（mock httpx 响应对象）
# ─────────────────────────────────────────────────────

class TestRetryDispatch(unittest.IsolatedAsyncioTestCase):

    async def test_429_reads_retry_after_then_retries(self):
        gateway, client = make_gateway([
            FakeResponse(429, headers={"Retry-After": "0"}),
            ok_response("成功"),
        ])
        resp = await gateway.chat(gw.GatewayRequest(messages=MSGS, tenant_id="premium"))
        self.assertEqual(resp.content, "成功")
        self.assertFalse(resp.fallback_used)
        self.assertEqual(len(client.calls), 2)              # 同一模型重试
        statuses = [c.get("status") for c in gateway.get_recent_calls()]
        self.assertIn("rate_limited", statuses)
        self.assertIn("ok", statuses)

    async def test_5xx_retries_then_falls_back(self):
        gateway, client = make_gateway(
            [FakeResponse(500)] * 3 + [ok_response("备用模型")])
        resp = await gateway.chat(gw.GatewayRequest(messages=MSGS, tenant_id="premium"))
        self.assertEqual(resp.content, "备用模型")
        self.assertTrue(resp.fallback_used)
        self.assertEqual(resp.model_used, "m-bal")
        self.assertEqual(len(client.calls), 4)              # 3 次主模型 + 1 次备用

    async def test_network_error_retries_and_records_breaker(self):
        breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        gateway, client = make_gateway(
            [ConnectionError("boom"), ok_response("恢复")],
            circuit_breaker=breaker)
        resp = await gateway.chat(gw.GatewayRequest(messages=MSGS, tenant_id="premium"))
        self.assertEqual(resp.content, "恢复")
        self.assertEqual(len(client.calls), 2)

    async def test_400_context_overflow_raises_without_retry(self):
        gateway, client = make_gateway([
            FakeResponse(400, text='{"error": {"code": "context_length_exceeded", '
                                   '"message": "maximum context length exceeded"}}'),
            ok_response("不应到达"),
        ])
        with self.assertRaises(gw.ContextOverflowError):
            await gateway.chat(gw.GatewayRequest(messages=MSGS, tenant_id="premium"))
        self.assertEqual(len(client.calls), 1)              # 不重试、不 fallback

    async def test_safety_refusal_no_fallback_no_cache(self):
        gateway, client = make_gateway([
            ok_response("无法回答该问题", finish_reason="content_filter"),
            ok_response("第二次仍打模型"),
        ])
        req = gw.GatewayRequest(messages=MSGS, tenant_id="premium",
                                scene="intent_classification")
        r1 = await gateway.chat(req)
        self.assertTrue(r1.safety_refusal)
        self.assertFalse(r1.fallback_used)                  # 不切备用模型
        # 拒答不写缓存：同请求再来一次仍会打模型
        r2 = await gateway.chat(gw.GatewayRequest(
            messages=MSGS, tenant_id="premium", scene="intent_classification"))
        self.assertFalse(r2.cache_hit)
        self.assertEqual(len(client.calls), 2)

    async def test_cost_not_zero_with_versioned_prices(self):
        gateway, _ = make_gateway([ok_response(prompt_tokens=1_000_000,
                                               completion_tokens=1_000_000)])
        resp = await gateway.chat(gw.GatewayRequest(messages=MSGS, tenant_id="premium"))
        # m-flag → deepseek-chat：2 元/8 元每百万
        self.assertAlmostEqual(resp.cost, 10.0, places=6)

    async def test_attempt_log_has_attempt_id_and_route_reason(self):
        gateway, _ = make_gateway([FakeResponse(500), ok_response("好")])
        await gateway.chat(gw.GatewayRequest(messages=MSGS, tenant_id="premium"))
        entries = gateway.get_recent_calls()
        self.assertEqual(len(entries), 2)                   # 每次尝试独立记录
        ids = {e["attempt_id"] for e in entries}
        self.assertEqual(len(ids), 2)
        for e in entries:
            self.assertIn("scene=", e["route_reason"])
        stats = gateway.get_stats()                         # get_stats 加锁快照
        self.assertEqual(stats["total_attempts"], 2)


# ─────────────────────────────────────────────────────
# 5. 缓存：改名 + key 隔离 + 真 LRU
# ─────────────────────────────────────────────────────

class TestExactResponseCache(unittest.IsolatedAsyncioTestCase):

    async def test_key_includes_tenant_prompt_version_model(self):
        cache = gw.ExactResponseCache()
        msgs = [{"role": "user", "content": "hi"}]
        await cache.put("t1", "v1", "m1", "intent_classification", msgs, "answer")
        self.assertEqual(
            await cache.get("t1", "v1", "m1", "intent_classification", msgs), "answer")
        # 租户 / prompt 版本 / 模型 任一不同均不得命中
        self.assertIsNone(await cache.get("t2", "v1", "m1", "intent_classification", msgs))
        self.assertIsNone(await cache.get("t1", "v2", "m1", "intent_classification", msgs))
        self.assertIsNone(await cache.get("t1", "v1", "m2", "intent_classification", msgs))
        # 非低风险场景不缓存
        await cache.put("t1", "v1", "m1", "customer_reply", msgs, "x")
        self.assertIsNone(await cache.get("t1", "v1", "m1", "customer_reply", msgs))

    async def test_true_lru_eviction(self):
        backend = gw.InMemoryCacheBackend(max_size=2)
        await backend.set("a", "1", 60)
        await backend.set("b", "2", 60)
        await backend.get("a")                              # 访问 a → a 变新
        await backend.set("c", "3", 60)                     # 淘汰最久未用的 b
        self.assertEqual(await backend.get("a"), "1")
        self.assertIsNone(await backend.get("b"))
        self.assertEqual(await backend.get("c"), "3")

    def test_semantic_cache_removed(self):
        self.assertFalse(hasattr(gw, "SemanticCache"))


# ─────────────────────────────────────────────────────
# 6. _extract_json 新行为
# ─────────────────────────────────────────────────────

class TestExtractJson(unittest.TestCase):

    def test_whitelist_expanded_keys(self):
        # 旧白名单只认 intent/ending/satisfaction，情感/评估/工单 JSON 全被丢弃
        self.assertEqual(
            LLMClient._extract_json('结果：{"emotion": "angry", "intensity": 4}'),
            {"emotion": "angry", "intensity": 4})
        self.assertEqual(
            LLMClient._extract_json('{"sufficient": true, "reason": "ok"}'),
            {"sufficient": True, "reason": "ok"})
        self.assertEqual(
            LLMClient._extract_json('{"issue_category": "技术支持", "priority": "high"}'),
            {"issue_category": "技术支持", "priority": "high"})
        self.assertEqual(
            LLMClient._extract_json('{"satisfied": false}'), {"satisfied": False})

    def test_generic_fallback_when_no_whitelist_key(self):
        self.assertEqual(LLMClient._extract_json('前缀 {"foo": 1} 后缀'), {"foo": 1})

    def test_last_whitelisted_object_wins(self):
        text = '示例：{"foo": 0}，最终答案：{"intent": "refund", "ending": true}'
        self.assertEqual(LLMClient._extract_json(text),
                         {"intent": "refund", "ending": True})

    def test_no_json_returns_empty(self):
        self.assertEqual(LLMClient._extract_json("纯文本，没有大括号"), {})
        self.assertEqual(LLMClient._extract_json(""), {})


# ─────────────────────────────────────────────────────
# 7. 熔断：半开态单探针
# ─────────────────────────────────────────────────────

class TestCircuitBreakerHalfOpen(unittest.TestCase):

    def test_half_open_single_probe(self):
        clock = {"t": 0.0}
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0,
                            clock=lambda: clock["t"])
        cb.record_failure("p", "m")
        cb.record_failure("p", "m")
        with self.assertRaises(CircuitOpenError):           # open：快速失败
            cb.allow_request("p", "m")
        clock["t"] = 11.0                                    # 过恢复期 → half-open
        cb.allow_request("p", "m")                           # 单探针放行
        with self.assertRaises(CircuitOpenError):            # 其余请求快速失败
            cb.allow_request("p", "m")
        cb.record_success("p", "m")                          # 探针成功 → closed
        cb.allow_request("p", "m")
        cb.allow_request("p", "m")

    def test_failed_probe_reopens(self):
        clock = {"t": 0.0}
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0,
                            clock=lambda: clock["t"])
        cb.record_failure("p", "m")
        cb.record_failure("p", "m")
        clock["t"] = 11.0
        cb.allow_request("p", "m")                           # 探针
        cb.record_failure("p", "m")                          # 探针失败 → 立刻重新 open
        with self.assertRaises(CircuitOpenError):
            cb.allow_request("p", "m")


# ─────────────────────────────────────────────────────
# 8. chat_sync 同步包装
# ─────────────────────────────────────────────────────

class TestChatSync(unittest.TestCase):

    def test_chat_sync_outside_event_loop(self):
        gateway, _ = make_gateway([ok_response("同步OK")])
        resp = gateway.chat_sync(gw.GatewayRequest(messages=MSGS, tenant_id="premium"))
        self.assertEqual(resp.content, "同步OK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
