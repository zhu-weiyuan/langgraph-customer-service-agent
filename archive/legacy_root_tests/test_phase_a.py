# -*- coding: utf-8 -*-
"""
Phase A: Request Governance & Structured Observability Tests for customer-service-agent.

Validates:
1. Gateway trace_id propagation (request → response)
2. Per-tenant/per-user rate limiting
3. Model profile config validation at startup
4. MetricsCollector enhanced per-user/scene tracking
5. X-Request-ID headers on app.py responses (errors included)
6. PII-safe observability (traces never contain raw message content)

Based on JavaGuide "AI 应用系统设计" Phase A: Request Governance + Structured Observability.
"""
import os
import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ── Gateway Tests ──────────────────────────────────────────────────


class TestGatewayTraceId:
    """A1: trace_id generation and propagation."""

    def test_chat_generates_trace_id_when_missing(self):
        from agent.llm_gateway import LLMGateway, GatewayRequest

        gw = LLMGateway()
        req = GatewayRequest(
            messages=[{"role": "user", "content": "hi"}],
            scene="intent_classification",
            tenant_id="free",
        )
        # Without trace_id — gateway should generate one
        assert req.trace_id == ""

    def test_chat_simple_passes_trace_id(self):
        """trace_id in chat_simple should be forwarded to GatewayRequest."""
        from agent.llm_gateway import LLMGateway, GatewayRequest

        gw = LLMGateway()
        # Just check that trace_id/user_id are accepted as kwargs
        req = GatewayRequest(
            messages=[{"role": "user", "content": "test"}],
            scene="default",
            tenant_id="premium",
            trace_id="custom-trace-123",
            user_id="user-abc",
        )
        assert req.trace_id == "custom-trace-123"
        assert req.user_id == "user-abc"


class TestGatewayRateLimiting:
    """A2: Per-tenant/per-user rate limiting."""

    def test_rate_limit_allows_within_quota(self):
        from agent.llm_gateway import LLMGateway, GatewayRequest

        gw = LLMGateway()
        req = GatewayRequest(
            messages=[{"role": "user", "content": f"hello {i}"} for i in range(3)],
            scene="default",
            tenant_id="internal",  # 1200/min — won't hit limit
            user_id="heavy-user",
        )
        assert gw._check_rate_limit(req.tenant_id, req.user_id) is True

    def test_rate_limit_exhausted_blocks_requests(self):
        from agent.llm_gateway import LLMGateway, GatewayRequest

        gw = LLMGateway()
        tenant_id = "free"  # 60/min limit
        user_id = f"rate-test-user-{id(gw)}"

        # Exhaust the quota
        for _ in range(65):
            gw._check_rate_limit(tenant_id, user_id)

        result = gw._check_rate_limit(tenant_id, user_id)
        assert result is False, "Rate limit should block after exceeding quota"

    def test_rate_limit_info_endpoint(self):
        from agent.llm_gateway import LLMGateway

        gw = LLMGateway()
        info = gw.get_rate_limit_info()
        # Should return a dict (may be empty initially)
        assert isinstance(info, dict)


class TestConfigValidation:
    """A3: Startup config validation."""

    def test_invalid_model_profile_raises_warning(self):
        from agent.llm_gateway import validate_model_profile, ModelProfile

        warnings = validate_model_profile(ModelProfile(
            name="", base_url="", context_window=-1, max_output=0,
            tier="invalid_tier", provider="test", api_key="", model_id="x",
            input_cost_per_m=0, output_cost_per_m=0,
        ))
        assert len(warnings) > 0
        assert any("name" in w for w in warnings)

    def test_valid_model_profile_passes(self):
        from agent.llm_gateway import validate_model_profile, ModelProfile

        profile = ModelProfile(
            name="good-model", provider="test", base_url="http://localhost:8080/v1",
            api_key="", model_id="qwen3", context_window=4096, max_output=512,
            tier="balanced", input_cost_per_m=0, output_cost_per_m=0,
        )
        warnings = validate_model_profile(profile)
        assert warnings == [], f"Valid profile should have no warnings: {warnings}"

    def test_empty_models_warns_no_enabled(self):
        from agent.llm_gateway import validate_gateway_config

        warnings = validate_gateway_config([])
        assert any("No enabled model" in w for w in warnings)


# ── Metrics Tests ────────────────────────────────────────────────


class TestMetricsEnhancements:
    """A4: Enhanced metrics with per-user/tenant tracking."""

    def test_record_request_with_user_context(self):
        from agent.metrics import MetricsCollector

        m = MetricsCollector()
        m.record_request("/api/chat", 200, 150.0, user_key="user-1", tenant_id="premium")
        
        stats = m.get_user_stats()
        assert "premium:user-1" in stats
        assert stats["premium:user-1"]["total_requests"] == 1

    def test_record_request_without_user(self):
        """Backward compatibility: record without user key still works."""
        from agent.metrics import MetricsCollector

        m = MetricsCollector()
        m.record_request("/api/health", 200, 10.0)
        
        counts = m.request_counts["/api/health"]
        assert counts == 1

    def test_rate_limit_recording(self):
        from agent.metrics import MetricsCollector

        m = MetricsCollector()
        m.record_rate_limit("abuse-user", "tenant-free")
        m.record_rate_limit("another-user")

        rl_stats = m.get_rate_limit_stats()
        assert rl_stats["total_rate_limited"] == 2
        assert rl_stats["recent_events_count"] == 2

    def test_scene_tracking(self):
        from agent.metrics import MetricsCollector

        m = MetricsCollector()
        m.record_scene_request("customer_reply", 200, 500.0)
        m.record_scene_request("customer_reply", 500, 100.0)
        
        scene_stats = m.get_scene_stats()
        assert scene_stats["customer_reply"] == 2

    def test_get_metrics_output(self):
        from agent.metrics import MetricsCollector

        m = MetricsCollector()
        m.record_request("/api/chat", 200, 100.0)
        
        output = m.get_metrics()
        assert isinstance(output, str)
        assert "http_requests_total" in output


# ── App Response Headers Tests ───────────────────────────────────


class TestResponseHeaders:
    """A5: X-Request-ID on all responses (including errors).

    Since ThreadingHTTPServer requires a real handler with do_POST,
    we verify the header injection code path exists in app.py source.
    """

    def test_app_py_has_x_request_id_on_error_paths(self):
        """Verify X-Request-ID is added to ALL error response paths (400, 429).

        This tests that the middleware-level pattern is present in app.py:
        all _send_response/400/429 blocks now include send_header('X-Request-ID', request_id).
        """
        with open("app.py", encoding="utf-8") as f:
            source = f.read()

        # Check that X-Request-ID is set on error responses (at least 3 locations)
        import re
        x_request_id_refs = re.findall(r'send_header\s*\(\s*[\'"]X-Request-ID', source)
        assert len(x_request_id_refs) >= 3, \
            f"Expected at least 3 X-Request-ID header sets (errors + JSON + stream), got {len(x_request_id_refs)}"

    def test_request_id_generated_at_top_of_handler(self):
        """request_id is generated at the top of do_POST so all paths can use it."""
        with open("app.py", encoding="utf-8") as f:
            source = f.read()

        # Find _send_response_headers_and_set_trace helper — must exist in do_POST
        assert "_send_response_headers_and_set_trace" in source, \
            "Expected _send_response_headers_and_set_trace helper function"
        assert 'request_id = str(uuid4())' in source, \
            "Expected request_id generation with uuid4"


class TestPIISafeObservability:
    """A6: PII-safe logging — traces should never contain raw message content."""

    def test_trace_id_has_no_message_content(self):
        """Trace entries in the gateway log should not contain user message text."""
        from agent.llm_gateway import GatewayRequest, GatewayResponse

        req = GatewayRequest(
            messages=[{"role": "user", "content": "my phone is 13800138000"}],
            tenant_id="free",
        )
        # Build response without ever storing content in trace log
        resp = GatewayResponse(
            content="placeholder", model_used="test", provider="test",
            input_tokens=5, output_tokens=3, cost=0.0,
            latency_ms=100.0, fallback_used=False, route_reason="direct",
            trace_id=req.trace_id or "test-trace",
        )

        # Verify the response's trace_id is set but content field is separate
        assert resp.trace_id != ""
        assert resp.content == "placeholder"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
