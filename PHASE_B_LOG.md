# Phase B — Production Hardening Log

## Findings
- `agent/llm_gateway.py` is the centralized gateway, but the legacy graph currently calls `LLMClient` directly. Circuit protection is implemented in the gateway; graph migration to gateway is a Phase C integration concern.
- The HTTP service is `ThreadingHTTPServer` with `ChatHandler`; standard JSON chat errors formerly returned raw exception text in a HTTP 200 envelope.
- `ContextCompactor.maybe_compact(..., force=True)` already existed and was safe to reuse for the >80% utilization path.
- No existing ToolRegistry was present, so a standalone registry was introduced without changing unregistered tools.

## Changes
- Added `agent/circuit_breaker.py`: thread-safe per-provider/model CLOSED/OPEN/HALF_OPEN circuit breaker, with configurable failure threshold/recovery timeout and `CircuitOpenError`.
- Updated `agent/llm_gateway.py`: circuit preflight/success/failure recording, fallback degraded response when all candidates fail, and prompt-version audit field in responses/traces. Environment: `LLM_CIRCUIT_FAILURE_THRESHOLD` (5), `LLM_CIRCUIT_RECOVERY_SECONDS` (30).
- Added `agent/context_monitor.py`: context estimation, >60% warning and >80% Dumb Zone error.
- Updated `agent/nodes.py`: monitors composed context and forces existing compaction before an LLM call in the Dumb Zone.
- Added `agent/prompt_registry.py`: immutable in-memory prompt versions, startup file/env loader, required-variable validation, rendered version metadata.
- Added `agent/tool_registry.py`: read/low-write/high-write risk labels; high risk needs `ToolCallContext.confirmed` and always emits an audit warning.
- Updated `app.py`: `_degraded_response()` and safe structured response on standard chat execution failure.
- Added `test_phase_b.py`: six targeted tests.

## Commands and outcomes
- `py -W ignore -m pytest test_phase_a.py test_phase_b.py`
  - Initial result: 20 passed, 2 failed (fixed GatewayResponse field and handler test name).
  - Final result: **22 passed** (`test_phase_a.py`: 16; `test_phase_b.py`: 6).

## Phase C gaps / concerns
- Migrate `agent/nodes.py` / `LLMClient` production paths to `LLMGateway` so circuit breaking and prompt audit protect every graph call (currently gateway only protects gateway consumers).
- Wire `PromptRegistry` to the existing system prompt source at startup and pass a real version identifier rather than the current basic `system:1` marker.
- Register real tool integrations with the new `ToolRegistry`; no existing central registry was found.
- Streaming `/api/chat` error branch still sends SSE error events; it should adopt the same degraded payload semantics if frontend contract requires it.
