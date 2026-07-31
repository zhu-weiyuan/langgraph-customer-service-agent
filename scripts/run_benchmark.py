# -*- coding: utf-8 -*-
"""
100 Worker 压测入口
启动 mock LLM → 启动 cs-agent → 发送 500 请求 → 收数据 → 清理
"""
import sys, os, time, json, subprocess, threading, statistics
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT = r'C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent'
os.chdir(PROJECT)
os.environ.setdefault("DATABASE_URL",
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph")
os.environ.setdefault("API_KEYS", "test-key")

MOCK_PORT = 18081
SRV_PORT = 17860
WORKERS = 100
TOTAL = 500

# ── 启动 mock LLM ─────────────────────────────────
mock_proc = subprocess.Popen(
    [sys.executable, '_mock_llm.py', str(MOCK_PORT)],
    cwd=PROJECT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("[%s] Mock LLM starting on :%d..." % (datetime.now().strftime('%H:%M:%S'), MOCK_PORT))
time.sleep(3)

# Verify mock LLM
import urllib.request
try:
    r = urllib.request.urlopen('http://localhost:%d/v1/models' % MOCK_PORT, timeout=5)
    assert r.status == 200
    print("  Mock LLM OK")
except Exception as e:
    print("  Mock LLM failed:", str(e)[:60])
    mock_proc.kill()
    sys.exit(1)

# ── 启动 cs-agent 服务器 ──────────────────────────
srv_env = os.environ.copy()
srv_env['OPENAI_BASE_URL'] = 'http://localhost:%d/v1' % MOCK_PORT
srv_env['OPENAI_API_KEY'] = 'sk-mock'
srv_env['REDIS_URL'] = ''

with open('server_load.log', 'w') as log:
    srv_proc = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'app_fastapi:app', '--host', '0.0.0.0',
         '--port', str(SRV_PORT), '--workers', '1', '--timeout-keep-alive', '120'],
        cwd=PROJECT, env=srv_env, stdout=log, stderr=log)

print("  Server starting on :%d..." % SRV_PORT)
time.sleep(12)

# Verify
try:
    r = urllib.request.urlopen(urllib.request.Request(
        'http://localhost:%d/api/health' % SRV_PORT,
        headers={'X-API-Key': 'test-key'}), timeout=10)
    h = json.loads(r.read())
    print("  Server OK: llm=%s" % h.get('llm', {}).get('reachable'))
except Exception as e:
    print("  Server failed:", str(e)[:60])
    mock_proc.kill()
    srv_proc.kill()
    sys.exit(1)

# ── 登录 ──────────────────────────────────────────
import http.client
def api(method, path, body=None, hdrs=None):
    conn = http.client.HTTPConnection('localhost', SRV_PORT, timeout=30)
    h = {'Content-Type': 'application/json', 'X-API-Key': 'test-key'}
    if hdrs: h.update(hdrs)
    b = json.dumps(body).encode() if body else None
    conn.request(method, path, body=b, headers=h)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    return data

login = api('POST', '/api/auth/login', {'username': 'loadtest_zwy', 'password': 'test123'})
TOKEN = login.get('access_token')
USER_ID = login.get('user_id')
print("  User=%s Token OK" % USER_ID)

# ── 压测 ──────────────────────────────────────────
TEST_MSGS = [
    "你们有智能音箱卖吗", "我的X-100连不上WiFi了",
    "想了解售后政策", "X-200和X-300有什么区别",
    "昨天下的单什么时候发货", "可以开发票吗",
    "产品保修期多久", "我想退货怎么操作",
    "你好帮我查订单", "你们客服电话多少",
]

results = []
lock = threading.Lock()

def send_one(wid, mid):
    session_id = "load_sess_%d" % wid
    msg = TEST_MSGS[(wid + mid) % len(TEST_MSGS)]
    start = time.perf_counter()
    try:
        resp = api('POST', '/api/chat', {
            'session_id': session_id, 'message': msg, 'user_id': USER_ID,
        }, {'Authorization': 'Bearer ' + TOKEN})
        elapsed = time.perf_counter() - start
        ok = resp.get('reply') is not None or resp.get('reply') != ''
        with lock:
            results.append({'ok': ok, 'elapsed': elapsed, 'code': 200})
    except Exception as e:
        elapsed = time.perf_counter() - start
        with lock:
            results.append({'ok': False, 'elapsed': elapsed, 'err': str(e)[:60]})

print("[%s] Sending %d requests (%d workers)..." % (
    datetime.now().strftime('%H:%M:%S'), TOTAL, WORKERS))

start_ts = time.perf_counter()
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    futures = [pool.submit(send_one, w, m) for w in range(WORKERS) for m in range(TOTAL // WORKERS)]
    done = 0
    for f in as_completed(futures):
        done += 1
        if done % 50 == 0:
            with lock:
                ok_now = sum(1 for r in results if r.get('ok'))
            print("  %d/%d (%d ok)" % (done, TOTAL, ok_now), flush=True)

total_elapsed = time.perf_counter() - start_ts

ok = [r for r in results if r.get('ok')]
fail = [r for r in results if not r.get('ok')]
lats = sorted([r['elapsed'] * 1000 for r in ok])

print("\n[%s] === Results ===" % datetime.now().strftime('%H:%M:%S'))
print("  Total:      %d" % len(results))
print("  Success:    %d (%.1f%%)" % (len(ok), len(ok)/len(results)*100))
print("  Failures:   %d" % len(fail))
if lats:
    print()
    print("  Wall clock: %.2fs" % total_elapsed)
    print("  Throughput: %.1f req/s" % (len(results)/total_elapsed))
    print("  Avg:        %.1fms" % statistics.mean(lats))
    print("  P50:        %.1fms" % lats[len(lats)//2])
    print("  P90:        %.1fms" % lats[int(len(lats)*0.9)])
    print("  P95:        %.1fms" % lats[int(len(lats)*0.95)])
    print("  Min:        %.1fms" % lats[0])
    print("  Max:        %.1fms" % lats[-1])

err_by = {}
for r in fail:
    e = r.get('err', '?')[:40]
    err_by[e] = err_by.get(e, 0) + 1
if err_by:
    print("\n  Error types:")
    for e, c in sorted(err_by.items(), key=lambda x: -x[1])[:5]:
        print("    %s: %d" % (e, c))

# DB stats
try:
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM conversation_history WHERE user_id = %s", (USER_ID,))
    print("\n  DB conversations: %d" % cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM sessions WHERE user_id = %s", (USER_ID,))
    print("  DB sessions: %d" % cur.fetchone()[0])
    conn.close()
except Exception as e:
    print("\n  DB error:", str(e)[:60])

# ── 清理 ──────────────────────────────────────────
srv_proc.kill()
srv_proc.wait(timeout=5)
mock_proc.kill()
mock_proc.wait(timeout=3)
print("\n[%s] Done." % datetime.now().strftime('%H:%M:%S'))
