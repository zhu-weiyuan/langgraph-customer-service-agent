"""Debug the customer service agent initialization."""
import sys, os

# Must be run from the project root
sys.path.insert(0, ".")

print("=== Step 1: imports ===")
try:
    from agent.graph import build_graph
    print("OK: graph module loaded")
except Exception as e:
    import traceback; traceback.print_exc()
    sys.exit(1)

print("\n=== Step 2: build_graph (use_sqlite=False, no DB_URL) ===")
try:
    g = build_graph(use_sqlite=False, db_path="checkpoints.db")
    print("OK: graph compiled successfully")
except Exception as e:
    import traceback; traceback.print_exc()
    sys.exit(1)

print("\n=== Step 3: app imports ===")
try:
    from agent.observability import TraceService
    ts = TraceService()
    print(f"OK: TraceService initialized")
except Exception as e:
    import traceback; traceback.print_exc()
    sys.exit(1)

print("\n=== ALL CHECKS PASSED ===")
