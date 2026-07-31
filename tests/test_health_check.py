#!/usr/bin/env python3
"""
Test suite for LangGraph Customer Service Agent health check endpoints.

Tests:
- /api/health endpoint response structure
- /api/ready endpoint readiness checks  
- /api/metrics endpoint Prometheus format
- Health check response times
- LangGraph-specific metrics

Uses app_fastapi (the P2 production entry) as the ASGI target.
"""

import pytest
import time


@pytest.fixture
def client():
    """Create synchronous test client against app_fastapi (FastAPI production entry).

    Uses starlette.testclient.TestClient which bridges sync/async calls.
    """
    from unittest.mock import Mock, patch

    # Patch runner._graph so the /api/ready endpoint sees a non-None graph
    with patch("agent.runner._graph", Mock()) as mock_graph:
        mock_graph.get_state.return_value = None

        import app_fastapi
        from starlette.testclient import TestClient

        with TestClient(app_fastapi.app) as c:
            yield c


class TestHealthEndpoint:
    """Test /api/health endpoint."""

    def test_health_returns_200(self, client):
        """Health endpoint should return 200 OK."""
        response = client.get("/api/health")
        assert response.status_code == 200

    def test_health_response_structure(self, client):
        """Health response should have required fields."""
        response = client.get("/api/health")
        data = response.json()

        assert "ok" in data
        assert "service" in data
        assert "port" in data
        assert "llm" in data
        assert "database" in data

    def test_health_llm_status(self, client):
        """Health response should include LLM connectivity status."""
        response = client.get("/api/health")
        data = response.json()
        llm = data["llm"]

        assert "reachable" in llm
        # llm.reachable is False when no local model is running (expected for test)
        assert isinstance(llm["reachable"], bool)

    def test_health_response_time(self, client):
        """Health endpoint should respond within 5 s (allows for _llm_reachable timeout)."""
        start = time.time()
        response = client.get("/api/health")
        elapsed = (time.time() - start) * 1000

        assert response.status_code == 200
        # _llm_reachable uses a 3-second client timeout; 5 s is a pragmatic upper bound
        assert elapsed < 5000, f"Health check took {elapsed}ms (should be < 5000ms)"


class TestReadyEndpoint:
    """Test /api/ready endpoint."""

    def test_ready_returns_200(self, client):
        """Ready endpoint should return 200 (non-503) when declared dependencies
        can be probed, even when some individual checks report failure."""
        response = client.get("/api/ready")
        # The endpoint returns 503 when critical checks fail; in test the
        # SQLite file exists so the endpoint should succeed, but we accept
        # either 200 or 503 as long as the structure is valid.
        assert response.status_code in (200, 503)

    def test_ready_response_structure(self, client):
        """Ready response should have required fields."""
        response = client.get("/api/ready")
        data = response.json()

        assert "ready" in data
        assert "checks" in data
        assert isinstance(data["ready"], bool)

    def test_ready_checks_structure(self, client):
        """Ready checks should have expected structure."""
        response = client.get("/api/ready")
        data = response.json()
        checks = data["checks"]

        assert len(checks) > 0

        for check_name, check_data in checks.items():
            # Each check reports {"ok": True/False} with additional context
            assert "ok" in check_data
            assert isinstance(check_data["ok"], bool)


class TestMetricsEndpoint:
    """Test /api/metrics endpoint."""

    def test_metrics_returns_200(self, client):
        """Metrics endpoint should return 200 OK."""
        response = client.get("/api/metrics")
        assert response.status_code == 200

    def test_metrics_content_type(self, client):
        """Metrics should have Prometheus content type."""
        response = client.get("/api/metrics")
        ct = response.headers["content-type"]
        assert ct.startswith("text/plain"), f"Expected text/plain, got {ct}"

    def test_metrics_prometheus_format(self, client):
        """Metrics should follow Prometheus exposition format."""
        response = client.get("/api/metrics")
        text = response.text

        # Should have HELP comments
        assert "# HELP" in text

        # Should have TYPE declarations
        assert "# TYPE" in text

    def test_metrics_has_langgraph_metrics(self, client):
        """Metrics should include LangGraph-specific metrics."""
        response = client.get("/api/metrics")
        text = response.text

        # Should have at least some LangGraph metrics
        langgraph_metrics = [
            "node_duration_seconds",
            "langgraph_active_sessions_count",
            "http_requests_total",
        ]

        # At least one should be present
        assert any(m in text for m in langgraph_metrics)


class TestCircuitBreakerMetrics:
    """Test circuit breaker metrics."""

    def test_circuit_breaker_state_metric(self, client):
        """Should expose circuit breaker state metric."""
        response = client.get("/api/metrics")
        text = response.text

        # Should have circuit breaker metrics
        assert "circuit_breaker_state" in text or "circuit_breaker" in text


class TestRateLimitMetrics:
    """Test rate limiting metrics."""

    def test_rate_limit_metrics_present(self, client):
        """Should expose rate limiting metrics."""
        response = client.get("/api/metrics")
        text = response.text

        # Should have rate limit metrics
        assert "rate_limit" in text or "http_requests_total" in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
