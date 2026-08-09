# -*- coding: utf-8 -*-
"""
压测入口: 先 patching 再启动服务器, 然后运行 100 worker 压测。
用法:
    python scripts/run_load_test.py [workers=100] [requests=500]
"""
import sys, os, time, json, asyncio, statistics, subprocess, urllib.request

# ── 配置 ────────────────────────────────────────────
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 100
TOTAL_REQUESTS = int(sys.argv[2]) if len(sys.argv) > 2 else 500
MESSAGES_PER_WORKER = TOTAL_REQUESTS // WORKERS
TEST_PORT = 17860  # 避免跟已有 :7860 冲突

# ── 写入 patched 版的 server entrypoint ─────────────
PATCHED_SERVER = os.path.join(os.path.dirname(__file__), '_load_test_server.py')
with open(PATCHED_SERVER, 'w', encoding='utf-8') as f:
    f.write(r'''# -*- coding: utf-8 -*-
"""
Patched server for load testing (LLM + rate limiter both mocked).
Auto-starts on port %d.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 1) Mock rate limiter (before import!)
import agent.rate_limiter as _rl
class _MockLimiter:
    async def acquire(self, *, user_id="", ip="", session_id=""):
        pass
    def concurrency(self, timeout=30):
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _noop(): yield
        return _noop()
    def get_stats(self):
        return {"by_layer": {}, "total_requests": 0, "total_blocked": 0}
_rl.get_rate_limiter = lambda: _MockLimiter()

# 2) Mock LLM (before node functions are called)
import agent.nodes as _nodes
def _mock_call_llm(messages, system=None, max_tokens=512, stream=False):
    return "您好，我是智能客服助手，请问有什么可以帮您的？"
_nodes._call_llm = _mock_call_llm
_nodes._call_llm_json = lambda messages, system, max_tokens=256: {
    "intent": "consult", "confidence": 0.95, "reasoning": "general question"
}

# 3) Patch user_memory extraction so it doesn't fail silently
import agent.user_memory as _um
_um.extract_from_message = lambda user_id, user_message, **kw: {}

# 4) Now import the real app
os.environ.setdefault("DATABASE_URL",
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph")
os.environ.setdefault("API_KEYS", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:8080/v1")
os.environ.setdefault("OPENAI_API_KEY", "sk-local")
os.environ["REDIS_URL"] = ""  # 禁用 Redis 限流

from app_fastapi import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=%d, log_level="warning")
''' % (TEST_PORT, TEST_PORT))

# ── 启动 patched 服务器 ────────────────────────────
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
print("[%s] Starting patched server on :%d..." % (time.strftime('%H:%M:%S'), TEST_PORT))

proc = subprocess.Popen(
    [sys.executable, PATCHED_SERVER],
    cwd=BASE,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
time.sleep(12)  # 等待启动

# 验证
KEY = 'test-key'
try:
    r = urllib.request.urlopen(
        urllib.request.Request('http://localhost:%d/api/health' % TEST_PORT,
                               headers={'X-API-Key': KEY}), timeout=10)
    h = json.loads(r.read())
    print("  Server OK: llm_reachable=%s" % h.get('llm', {}).get('reachable'))
except Exception as e:
    print("  Server FAIL: %s" % str(e)[:80])
    proc.kill()
    sys.exit(1)

# ── 登录 ────────────────────────────────────────────
login_body = json.dumps({"username": "loadtest_zwy", "password": "test123"}).encode()
r = urllib.request.urlopen(
    urllib.request.Request('http://localhost:%d/api/auth/login' % TEST_PORT,
                           data=login_body,
                           headers={'Content-Type': 'application/json'},
                           method='POST'), timeout=10)
login = json.loads(r.read())
TOKEN = login.get("access_token")
USER_ID = login.get("user_id")
print("  User=%s Token OK" % USER_ID)

# ── 压测消息 ────────────────────────────────────────
TEST_MESSAGES = [
    "你们有智能音箱卖吗", "我的X-100连不上WiFi了",
    "想了解售后政策", "X-200和X-300有什么区别",
    "昨天下的单什么时候发货", "可以开发票吗",
    "产品保修期多久", "我想退货怎么操作",
    "你好帮我查订单", "你们客服电话多少",
]

# ── 异步 HTTP 客户端 ───────────────────────────────
async def send_one(session_id, message):
    import aiohttp
    start = time.perf_counter()
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                'http://localhost:%d/api/chat' % TEST_PORT,
                json={"session_id": session_id, "message": message, "user_id": USER_ID},
                headers={"Authorization": "Bearer " + TOKEN},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                elapsed = time.perf_counter() - start
                text = await resp.text()
                return {"ok": resp.status == 200, "status": resp.status, "elapsed": elapsed,
                        "body": text[:100] if resp.status != 200 else ""}
    except Exception as e:
        return {"ok": False, "status": 0, "elapsed": time.perf_counter() - start,
                "body": str(e)[:80]}

async def run_load():
    # 分批发送避免 socket 溢出
    BATCH_SIZE = 20
    all_tasks = []
    for w in range(WORKERS):
        sid = "load_sess_%d" % w
        for m in range(MESSAGES_PER_WORKER):
            msg = TEST_MESSAGES[(w + m) % len(TEST_MESSAGES)]
            all_tasks.append(send_one(sid, msg))

    print("\n[%s] Sending %d requests with %d workers..." % (
        time.strftime('%H:%M:%S'), len(all_tasks), WORKERS))
    start_ts = time.perf_counter()

    results = []
    for i in range(0, len(all_tasks), BATCH_SIZE):
        batch = all_tasks[i:i+BATCH_SIZE]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)
        results.extend(batch_results)
        pct = min(100, int((i+BATCH_SIZE)/len(all_tasks)*100))
        done_ok = sum(1 for r in batch_results if isinstance(r, dict) and r.get("ok"))
        print("  %d%% (%d ok this batch)" % (pct, done_ok), flush=True)

    total_elapsed = time.perf_counter() - start_ts

    # ── 统计 ──
    ok = [r for r in results if isinstance(r, dict) and r.get("ok")]
    fail = [r for r in results if isinstance(r, dict) and not r.get("ok")]
    exc = [r for r in results if isinstance(r, Exception)]
    latencies = sorted([r["elapsed"] * 1000 for r in ok])

    print("\n[%s] === Results ===" % time.strftime('%H:%M:%S'))
    print("  Total:       %d" % len(results))
    print("  Success:     %d (%.1f%%)" % (len(ok), len(ok)/len(results)*100))
    print("  HTTP errors: %d" % len(fail))
    print("  Exceptions:  %d" % len(exc))
    print()
    if latencies:
        print("  Wall clock:  %.2fs" % total_elapsed)
        print("  Throughput:  %.1f req/s" % (len(results)/total_elapsed))
        print("  Avg:         %.1fms" % statistics.mean(latencies))
        print("  P50:         %.1fms" % latencies[len(latencies)//2])
        print("  P90:         %.1fms" % latencies[int(len(latencies)*0.9)])
        print("  P95:         %.1fms" % latencies[int(len(latencies)*0.95)])
        print("  P99:         %.1fms" % latencies[int(len(latencies)*0.99)])
        print("  Min:         %.1fms" % latencies[0])
        print("  Max:         %.1fms" % latencies[-1])

    if fail:
        from collections import Counter
        sc = Counter(r.get("status", 0) for r in fail)
        print("\n  Error status: %s" % dict(sc.most_common(5)))

    # DB 写入统计
    try:
        import psycopg
        conn = psycopg.connect(os.environ.get("DATABASE_URL",
            "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph"))
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM conversation_history WHERE user_id = %s", (USER_ID,))
        dbc = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM sessions WHERE user_id = %s", (USER_ID,))
        dbs = cur.fetchone()[0]
        print("\n  DB writes:")
        print("    conversations: %d" % dbc)
        print("    sessions: %d" % dbs)
        conn.close()
    except Exception as e:
        print("\n  DB check: %s" % str(e)[:60])

    return {"total": len(results), "ok": len(ok), "fail": len(fail),
            "elapsed": total_elapsed, "latencies": latencies}

try:
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    result = asyncio.run(run_load())
finally:
    # 清理
    proc.kill()
    proc.wait(timeout=5)
    try:
        os.remove(PATCHED_SERVER)
    except OSError:
        pass
    print("\n[%s] Server stopped, temp files cleaned." % time.strftime('%H:%M:%S'))
