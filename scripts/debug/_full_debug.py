"""Comprehensive startup diagnostic - print every failure point."""
import sys, os, traceback

os.chdir("C:\\Users\\Administrator\\.openclaw\\workspace\\langgraph-customer-service-agent")
sys.path.insert(0, ".")

def section(name):
    print(f"\n{'='*60}")
    print(f">>> {name}")
    print(f"{'='*60}")

try:
    section("1. Import app module (full module-level side effects)")
    from app import main as app_main, _install_shutdown_signals, PORT
    print(f"  OK: loaded app.main, PORT={PORT}")
except Exception as e:
    traceback.print_exc()
    sys.exit(1)

try:
    section("2. Call _install_shutdown_signals")
    _install_shutdown_signals()
    print("  OK")
except Exception as e:
    traceback.print_exc()
    sys.exit(1)

try:
    section("3. Check environment variables")
    db_url = os.environ.get('DATABASE_URL', '(not set)')
    use_sqlite = os.environ.get('USE_SQLITE', '0')
    print(f"  DATABASE_URL={db_url}")
    print(f"  USE_SQLITE={use_sqlite}")
except Exception as e:
    traceback.print_exc()

try:
    section("4. Build graph")
    from agent.graph import build_graph
    g = build_graph(use_sqlite=False, db_path="checkpoints.db")
    print(f"  OK: {type(g).__name__}")
except Exception as e:
    traceback.print_exc()
    sys.exit(1)

try:
    section("5. Init _trace_service")
    from agent.observability import TraceService
    ts = TraceService()
    print(f"  OK: {ts}")
except Exception as e:
    traceback.print_exc()
    sys.exit(1)

try:
    section("6. Init memory DB")
    from agent.memory import _init_db
    _init_db()
    print("  OK")
except Exception as e:
    traceback.print_exc()
    sys.exit(1)

try:
    section("7. LLM connectivity check")
    from app import _check_llm_connectivity
    ok = _check_llm_connectivity()
    print(f"  LLM reachable: {ok}")
except Exception as e:
    traceback.print_exc()

try:
    section("8. Start server (should bind to port)")
    # Don't actually start serve_forever(), just verify the server can be created
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    from app import ChatHandler
    server = ThreadingHTTPServer(('127.0.0.1', PORT), ChatHandler)
    print(f"  OK: Server object created on port {PORT}")
    server.server_close()
except Exception as e:
    traceback.print_exc()
    sys.exit(1)

print(f"\n{'='*60}")
print("ALL DIAGNOSTIC CHECKS PASSED")
print(f"{'='*60}")
