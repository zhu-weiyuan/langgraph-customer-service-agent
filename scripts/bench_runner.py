# -*- coding: utf-8 -*-
"""
Graph Runner 压测（不过 LLM）。
直接调用 runner.run()，跳过 HTTP 层，专注测试 Graph 编排 + DB 写入。
"""
import sys, os, time, json, asyncio, statistics
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

os.environ.setdefault("DATABASE_URL",
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph")
os.environ.setdefault("API_KEYS", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:8080/v1")
os.environ.setdefault("OPENAI_API_KEY", "sk-local")
os.environ["REDIS_URL"] = ""

# ── Patch BEFORE any project imports ────────────────
import agent.nodes as _nodes
def _mock_call_llm(messages, system=None, max_tokens=512, stream=False):
    return "您好，我是智能客服助手，请问有什么可以帮您的？"
_nodes._call_llm = _mock_call_llm
_nodes._call_llm_json = lambda messages, system, max_tokens=256: {
    "intent": "consult", "confidence": 0.95, "reasoning": "general"
}

import agent.user_memory as _um
_um.extract_from_message = lambda user_id, user_message, **kw: {}

# ── 导入项目模块 ────────────────────────────────────
print("[%s] Loading modules..." % datetime.now().strftime('%H:%M:%S'), flush=True)
from agent import runner
print("[%s] Modules loaded." % datetime.now().strftime('%H:%M:%S'), flush=True)

TEST_MESSAGES = [
    "你们有智能音箱卖吗", "我的X-100连不上WiFi了",
    "想了解售后政策", "X-200和X-300有什么区别",
    "昨天下的单什么时候发货", "可以开发票吗",
    "产品保修期多久", "我想退货怎么操作",
    "你好帮我查订单", "你们客服电话多少",
]

sentinel = object()

async def run_one(wid, mid):
    """单次 runner.run() 调用。"""
    session_id = "load_sess_%d" % wid
    msg = TEST_MESSAGES[(wid + mid) % len(TEST_MESSAGES)]
    start = time.perf_counter()
    try:
        result = await runner.run(session_id=session_id, user_message=msg, user_id="loadtest_zwy")
        elapsed = time.perf_counter() - start
        ok = result is not None and (result.get("reply") or result.get("bot_reply"))
        return {"ok": ok, "elapsed": elapsed, "session": session_id}
    except Exception as e:
        elapsed = time.perf_counter() - start
        return {"ok": False, "elapsed": elapsed, "err": str(e)[:60]}

async def main():
    WORKERS = 100
    TOTAL = 500
    PER = TOTAL // WORKERS

    print("[%s] Starting %d workers, %d requests..." % (
        datetime.now().strftime('%H:%M:%S'), WORKERS, TOTAL), flush=True)
    print("  Mock LLM: instant (no llama.cpp)", flush=True)
    print("  Target: Graph runner.run() -> DB write", flush=True)
    print()

    tasks = []
    for w in range(WORKERS):
        for m in range(PER):
            tasks.append(run_one(w, m))

    # 限流: 最多 5 并发 (DB 连接池有限)
    sem = asyncio.Semaphore(5)
    
    async def throttled_run(w, m):
        async with sem:
            return await run_one(w, m)
    
    tasks = [throttled_run(w, m) for w in range(WORKERS) for m in range(PER)]

    BATCH = 20
    results = []
    start_ts = time.perf_counter()
    for i in range(0, len(tasks), BATCH):
        batch = await asyncio.gather(*tasks[i:i+BATCH], return_exceptions=True)
        results.extend(batch)
        done = i + len(batch)
        ok_in_batch = sum(1 for r in batch if isinstance(r, dict) and r.get("ok"))
        print("  %d/%d (%d ok batch)" % (min(done, TOTAL), TOTAL, ok_in_batch), flush=True)

    total_elapsed = time.perf_counter() - start_ts

    ok = [r for r in results if isinstance(r, dict) and r.get("ok")]
    fail = [r for r in results if isinstance(r, dict) and not r.get("ok")]
    exc = [r for r in results if isinstance(r, Exception)]
    latencies = sorted([r["elapsed"] * 1000 for r in ok])

    print("\n[%s] === Results ===" % datetime.now().strftime('%H:%M:%S'))
    print("  Total:          %d" % len(results))
    print("  Success:        %d (%.1f%%)" % (len(ok), len(ok)/len(results)*100))
    print("  Failures:       %d" % len(fail))
    print("  Exceptions:     %d" % len(exc))
    if latencies:
        print()
        print("  Wall clock:     %.2fs" % total_elapsed)
        print("  Throughput:     %.1f req/s" % (len(results)/total_elapsed))
        print("  Avg latency:    %.1fms" % statistics.mean(latencies))
        print("  P50:            %.1fms" % latencies[len(latencies)//2])
        print("  P90:            %.1fms" % latencies[int(len(latencies)*0.9)])
        print("  P95:            %.1fms" % latencies[int(len(latencies)*0.95)])
        print("  P99:            %.1fms" % latencies[int(len(latencies)*0.99)])
        print("  Min:            %.1fms" % latencies[0])
        print("  Max:            %.1fms" % latencies[-1])

    # DB 校验
    import psycopg
    try:
        conn = psycopg.connect(os.environ["DATABASE_URL"])
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM conversation_history WHERE user_id = 'loadtest_zwy'")
        dbc = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM sessions WHERE user_id = 'loadtest_zwy'")
        dbs = cur.fetchone()[0]
        print("\n  DB writes:")
        print("    conversations: %d" % dbc)
        print("    sessions:      %d" % dbs)
        conn.close()
    except Exception as e:
        print("\n  DB check: %s" % str(e)[:60])

    return {"total": len(results), "ok": len(ok), "fail": len(fail),
            "elapsed": total_elapsed, "latencies": latencies}

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    result = asyncio.run(main())
