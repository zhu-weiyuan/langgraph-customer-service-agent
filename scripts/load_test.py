# -*- coding: utf-8 -*-
"""
智能客服 100 Worker 压测（不过 LLM）

模拟 100 并发请求直接走 Graph（LLM 响应被 mock 为即时返回），
测试除 LLM 推理外的全链路吞吐：认证 → 限流 → 意图识别 → Graph编排 → 记忆写入。

使用方式:
    cd langgraph-customer-service-agent
    python scripts/load_test.py

输出: 吞吐量、延迟百分位、错误率、下游调用统计。
"""
import sys, os, time, json, asyncio, statistics
from datetime import datetime

# ── 将项目根加入 sys.path ──────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

# ── 在 import app 之前 patching ──────────────────────
import agent.nodes as _nodes
_ORIGINAL_CALL_LLM = _nodes._call_llm

def _mock_call_llm(messages, system=None, max_tokens=512, stream=False):
    """Mock LLM: 即时返回固定回复（不过 LLM）。"""
    return "您好，感谢您的咨询！我是智能客服助手，请问有什么可以帮助您的？"

_nodes._call_llm = _mock_call_llm
_nodes._call_llm_json = lambda messages, system, max_tokens=256: {
    "intent": "consult", "confidence": 0.95,
    "reasoning": "user is asking a general question"
}

# ── Patche rate limiter 允许高并发 ─────────────────
import agent.rate_limiter as _rl
class _MockLimiter:
    """放行一切请求的 mock limiter, 不限速不限并发。"""
    async def acquire(self, *, user_id="", ip="", session_id="") -> None:
        pass
    def concurrency(self, timeout=30):
        """返回一个 no-op 异步上下文管理器。"""
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _noop():
            yield
        return _noop()
    def get_stats(self):
        return {"by_layer": {}, "total_requests": 0, "total_blocked": 0}
_rl.get_rate_limiter = lambda: _MockLimiter()

# ── 启动 patched 服务器 ─────────────────────────────
os.environ.setdefault("DATABASE_URL",
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph")
os.environ.setdefault("API_KEYS", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:8080/v1")
os.environ.setdefault("OPENAI_API_KEY", "sk-local")

from fastapi.testclient import TestClient
from app_fastapi import app
client = TestClient(app)

# ── 登录获取 token ──────────────────────────────────
LOGIN_PAYLOAD = {"username": "loadtest_zwy", "password": "test123"}
login_resp = client.post("/api/auth/login", json=LOGIN_PAYLOAD)
assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
TOKEN = login_resp.json().get("access_token")
USER_ID = login_resp.json().get("user_id")
print(f"[{datetime.now().strftime('%H:%M:%S')}] User={USER_ID} Token OK")

# ── 测试消息 ────────────────────────────────────────
TEST_MESSAGES = [
    "请问你们有智能音箱卖吗",
    "我的X-100连不上WiFi了",
    "想了解一下你们的售后政策",
    "X-200和X-300有什么区别",
    "我昨天下的单什么时候发货",
    "可以开发票吗",
    "你们的客服电话是多少",
    "产品保修期多久",
    "我想退货怎么操作",
    "你好，帮我查一下订单",
]

# ── 任务函数 ────────────────────────────────────────
def send_chat(session_id, message):
    """单次 /api/chat 请求（同步, 因为 TestClient 是 sync）。"""
    start = time.perf_counter()
    resp = client.post(
        "/api/chat",
        json={"session_id": session_id, "message": message, "user_id": USER_ID},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    elapsed = time.perf_counter() - start
    ok = resp.status_code == 200
    return {
        "ok": ok,
        "status": resp.status_code,
        "elapsed": elapsed,
        "session": session_id,
    }

# ── 100 Worker 并发 ─────────────────────────────────
async def run_load_test():
    WORKERS = 100
    TOTAL_REQUESTS = 500  # 每个 worker 发 5 条, 共 500 req
    MESSAGES_PER_WORKER = TOTAL_REQUESTS // WORKERS
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting {WORKERS} workers, {TOTAL_REQUESTS} requests...")
    print(f"    Mock LLM: instant reply (no llama.cpp inference)")
    print()
    
    loop = asyncio.get_event_loop()
    tasks = []
    
    for w in range(WORKERS):
        session_id = f"load_sess_{w}"
        for m in range(MESSAGES_PER_WORKER):
            msg = TEST_MESSAGES[(w + m) % len(TEST_MESSAGES)]
            tasks.append(
                loop.run_in_executor(None, send_chat, session_id, msg)
            )
    
    start_ts = time.perf_counter()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    total_elapsed = time.perf_counter() - start_ts
    
    # ── 统计 ────────────────────────────────────────
    ok_results = [r for r in results if isinstance(r, dict) and r.get("ok")]
    fail_results = [r for r in results if isinstance(r, dict) and not r.get("ok")]
    exc_results = [r for r in results if isinstance(r, Exception)]
    
    latencies = [r["elapsed"] * 1000 for r in ok_results]  # ms
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] === Results ===")
    print(f"  Total requests:     {len(results)}")
    print(f"  Success (200):      {len(ok_results)} ({len(ok_results)/len(results)*100:.1f}%)")
    print(f"  HTTP errors:        {len(fail_results)}")
    print(f"  Exceptions:         {len(exc_results)}")
    print()
    
    if latencies:
        latencies.sort()
        print(f"  Wall clock:         {total_elapsed:.2f}s")
        print(f"  Throughput:         {len(results)/total_elapsed:.1f} req/s")
        print(f"  Avg latency:        {statistics.mean(latencies):.1f}ms")
        print(f"  P50 (median):       {latencies[len(latencies)//2]:.1f}ms")
        print(f"  P90:                {latencies[int(len(latencies)*0.9)]:.1f}ms")
        print(f"  P95:                {latencies[int(len(latencies)*0.95)]:.1f}ms")
        print(f"  P99:                {latencies[int(len(latencies)*0.99)]:.1f}ms")
        print(f"  Min:                {latencies[0]:.1f}ms")
        print(f"  Max:                {latencies[-1]:.1f}ms")
    else:
        print("  No successful requests to report latency")
    
    # 错误详情
    if fail_results:
        print(f"\n  HTTP errors by status:")
        by_status = {}
        for r in fail_results:
            by_status[r.get("status", 0)] = by_status.get(r.get("status", 0), 0) + 1
        for code, cnt in sorted(by_status.items()):
            print(f"    {code}: {cnt}")
    
    if exc_results:
        from collections import Counter
        exc_types = Counter(type(e).__name__ for e in exc_results)
        print(f"\n  Exceptions by type:")
        for t, cnt in exc_types.most_common():
            print(f"    {t}: {cnt}")
    
    # DB 写入统计
    import psycopg
    try:
        conn = psycopg.connect(os.environ["DATABASE_URL"])
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM conversation_history WHERE user_id = %s", (USER_ID,))
        db_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM sessions WHERE user_id = %s", (USER_ID,))
        sess_count = cur.fetchone()[0]
        print(f"\n  DB writes:")
        print(f"    conversations: {db_count}")
        print(f"    sessions: {sess_count}")
        conn.close()
    except Exception as e:
        print(f"\n  DB check error: {e}")
    
    print(f"\n  Done.")

if __name__ == "__main__":
    asyncio.run(run_load_test())
