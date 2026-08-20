"""Step-by-step diagnostic for customer service agent startup."""
import sys, os, traceback

os.chdir("C:\\Users\\Administrator\\.openclaw\\workspace\\langgraph-customer-service-agent")
sys.path.insert(0, ".")

steps = []
current_step = ""

def step(name):
    global current_step
    current_step = name
    print(f"\n{'='*60}")
    print(f"STEP: {name}")
    print(f"{'='*60}")
    return name

for s in [
    "Step 1: Basic imports",
    "Step 2: graph module load",
    "Step 3: build_graph (no SQL)",
    "Step 4: app module imports",
    "Step 5: TraceService",
    "Step 6: memory._init_db",
    "Step 7: LLM connectivity check",
]:
    step(s)

print("=== Step 1: Basic imports ===")
try:
    import os, sys, time, signal, threading, json
    from contextlib import asynccontextmanager
    print("OK: All basic imports succeed")
except Exception as e:
    traceback.print_exc()

print("\n=== Step 2: graph module load ===")
try:
    from agent.graph import build_graph
    print(f"OK: build_graph imported: {build_graph}")
except Exception as e:
    traceback.print_exc()

print("\n=== Step 3: build_graph (use_sqlite=False) ===")
try:
    g = build_graph(use_sqlite=False, db_path="checkpoints.db")
    print(f"OK: Graph compiled. Type={type(g).__name__}")
except Exception as e:
    traceback.print_exc()

print("\n=== Step 4: app module imports ===")
try:
    from agent.security.pii_redactor import redact, scan_and_log
    from agent.llm_client import get_llm_client
    from agent.rate_limiter import get_rate_limiter
    from agent.metrics import metrics
    print("OK: Core imports succeed")
except Exception as e:
    traceback.print_exc()

print("\n=== Step 5: TraceService ===")
try:
    from agent.observability import TraceService
    ts = TraceService()
    print(f"OK: TraceService created: {ts}")
except Exception as e:
    traceback.print_exc()

print("\n=== Step 6: memory._init_db ===")
try:
    from agent.memory import _init_db
    _init_db()
    print("OK: Memory DB initialized")
except Exception as e:
    traceback.print_exc()

print("\n=== ALL DIAGNOSTIC STEPS COMPLETE ===")
