# -*- coding: utf-8 -*-
"""
100 Worker 压测（threading + 信号量，不过 LLM）

因为 Runner 的 PostgresSaver 同步连接池不支持高并发，
这里用 threading.Semaphore(10) 限制并发连接数，
真实模拟 100 Worker 排队竞争的场景。
"""
import sys, os, time, json, threading, statistics
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

os.environ.setdefault("DATABASE_URL",
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph")
os.environ.setdefault("API_KEYS", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:8080/v1")
os.environ.setdefault("OPENAI_API_KEY", "sk-local")
os.environ["REDIS_URL"] = ""

# ── Patch BEFORE imports ────────────────────────────
import agent.nodes as _nodes
def _mock_call_llm(messages, system=None, max_tokens=512, stream=False):
    return "您好，我是智能客服助手，请问有什么可以帮您的？"
_nodes._call_llm = _mock_call_llm
_nodes._call_llm_json = lambda messages, system, max_tokens=256: {
    "intent": "consult", "confidence": 0.95, "reasoning": "general"
}
import agent.user_memory as _um
_um.extract_from_message = lambda user_id, user_message, **kw: {}

# ── Import ──────────────────────────────────────────
print("[%s] Loading modules..." % datetime.now().strftime('%H:%M:%S'), flush=True)
import asyncio
from agent import runner
print("[%s] Modules loaded." % datetime.now().strftime('%H:%M:%S'), flush=True)

TEST_MESSAGES = [
    "你们有智能音箱卖吗", "我的X-100连不上WiFi了",
    "想了解售后政策", "X-200和X-300有什么区别",
    "昨天下的单什么时候发货", "可以开发票吗",
    "产品保修期多久", "我想退货怎么操作",
    "你好帮我查订单", "你们客服电话多少",
]

# ── 全局信号量控制 DB 并发 ────────────────────────
DB_SEM = threading.Semaphore(10)
results = []
results_lock = threading.Lock()

def run_one(wid, mid):
    with DB_SEM:
        session_id = "load_sess_%d" % wid
        msg = TEST_MESSAGES[(wid + mid) % len(TEST_MESSAGES)]
        start = time.perf_counter()
        try:
            result = asyncio.run(runner.run(
                session_id=session_id, user_message=msg, user_id="loadtest_zwy"
            ))
            elapsed = time.perf_counter() - start
            ok = result is not None and (result.get("reply") or result.get("bot_reply"))
            with results_lock:
                results.append({"ok": ok, "elapsed": elapsed})
        except Exception as e:
            elapsed = time.perf_counter() - start
            with results_lock:
                results.append({"ok": False, "elapsed": elapsed, "err": str(e)[:60]})

def main():
    WORKERS = 100
    TOTAL = 500
    PER = TOTAL // WORKERS

    print("[%s] Starting %d workers, %d requests..." % (
        datetime.now().strftime('%H:%M:%S'), WORKERS, TOTAL), flush=True)
    print("  Concurrency: max 10 DB connections (ThreadPool: %d)" % WORKERS, flush=True)
    print("  Mock LLM: instant", flush=True)
    print()

    start_ts = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = []
        for w in range(WORKERS):
            for m in range(PER):
                futures.append(pool.submit(run_one, w, m))

        done = 0
        for f in as_completed(futures):
            done += 1
            if done % 50 == 0:
                with results_lock:
                    ok_now = sum(1 for r in results if r.get("ok"))
                print("  %d/%d (%d ok)" % (done, TOTAL, ok_now), flush=True)

    total_elapsed = time.perf_counter() - start_ts

    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    latencies = sorted([r["elapsed"] * 1000 for r in ok])

    print("\n[%s] === Results ===" % datetime.now().strftime('%H:%M:%S'))
    print("  Total:          %d" % len(results))
    print("  Success:        %d (%.1f%%)" % (len(ok), len(ok)/len(results)*100))
    print("  Failures:       %d" % len(fail))
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

    # 错误统计
    err_by_type = {}
    for r in fail:
        err = r.get("err", "unknown")[:40]
        err_by_type[err] = err_by_type.get(err, 0) + 1
    if err_by_type:
        print("\n  Failures by type:")
        for e, c in sorted(err_by_type.items(), key=lambda x: -x[1])[:5]:
            print("    %s: %d" % (e, c))

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

    print()

if __name__ == "__main__":
    main()
