#!/usr/bin/env bash
# ===================================================================
#  一键应用层压测（Linux/macOS 版；Windows 用 run_loadtest.bat）
#
#  流程：设 MOCK_LLM=1 → 起多 worker uvicorn → 等就绪 → 压测 → 关服务
#
#  用法：
#    scripts/loadtest/run_loadtest.sh                 # 默认 100 用户 / 60s / 4 worker
#    USERS=200 DURATION=120 WORKERS=8 scripts/loadtest/run_loadtest.sh
#    scripts/loadtest/run_loadtest.sh 50 30 2         # users duration workers
# ===================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT"

USERS="${1:-${USERS:-100}}"
DURATION="${2:-${DURATION:-60}}"
WORKERS="${3:-${WORKERS:-4}}"
RAMP="${RAMP:-15}"
PORT="${PORT:-7860}"
PYTHON="${PYTHON:-python3}"
MOCK_LLM_DELAY_MS="${MOCK_LLM_DELAY_MS:-200}"

# ── mock 开关：LLM/embedding 不出进程 ──────────────────────
export MOCK_LLM=1
export MOCK_EMBEDDING=1
export MOCK_LLM_DELAY_MS
export MOCK_LLM_JSON_DELAY_MS="$MOCK_LLM_DELAY_MS"
export PYTHONUNBUFFERED=1

STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT_DIR="$ROOT/reports"
mkdir -p "$REPORT_DIR"
JSON_OUT="$REPORT_DIR/loadtest_${USERS}u_${WORKERS}w_${STAMP}.json"
CSV_OUT="$REPORT_DIR/loadtest_${USERS}u_${WORKERS}w_${STAMP}.csv"
SERVER_LOG="$REPORT_DIR/uvicorn_${STAMP}.log"

echo "==================================================================="
echo " Application-layer load test (LLM mocked at ${MOCK_LLM_DELAY_MS}ms)"
echo "==================================================================="
echo "  workers=$WORKERS users=$USERS duration=${DURATION}s ramp=${RAMP}s port=$PORT"
echo "  report: $JSON_OUT"
echo

SERVER_PID=""
cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "[4/4] stopping server (pid $SERVER_PID) ..."
        kill -TERM "-$SERVER_PID" 2>/dev/null || kill -TERM "$SERVER_PID" 2>/dev/null
        sleep 2
        kill -KILL "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "[1/4] starting uvicorn with $WORKERS workers ..."
setsid "$PYTHON" -m uvicorn app_fastapi:app --host 127.0.0.1 --port "$PORT" \
    --workers "$WORKERS" --log-level warning > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!

echo "[2/4] waiting for /healthz ..."
READY=0
for i in $(seq 1 60); do
    if "$PYTHON" -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${PORT}/healthz',timeout=2).status==200 else 1)" 2>/dev/null; then
        READY=1; echo "      ready after ${i}s"; break
    fi
    sleep 1
done
if [ "$READY" -ne 1 ]; then
    echo "[ERROR] server not ready in 60s; see $SERVER_LOG"
    tail -20 "$SERVER_LOG" || true
    exit 1
fi

echo "[3/4] running load test ..."
echo
"$PYTHON" "$ROOT/scripts/loadtest/run_loadtest.py" \
    --host "http://127.0.0.1:${PORT}" \
    --users "$USERS" --duration "$DURATION" --ramp "$RAMP" \
    --profile --proc-filter uvicorn \
    --json "$JSON_OUT" --csv "$CSV_OUT" \
    --label "workers=${WORKERS},mock_delay=${MOCK_LLM_DELAY_MS}ms"
RC=$?

echo
echo "Reports: $JSON_OUT / $CSV_OUT / $SERVER_LOG"
echo "NOTE: application-layer numbers (LLM mocked). See LOADTEST_README.md."
exit $RC
