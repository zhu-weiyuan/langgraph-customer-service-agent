#!/usr/bin/env python3
"""
Test suite for LangGraph Customer Service Agent health check endpoints.

Tests:
- /api/health endpoint response structure
- /api/ready endpoint readiness checks  
- /api/metrics endpoint Prometheus format
- Health check response times
- LangGraph-specific metrics
"""

import pytest
import time
import sys
from pathlib import Path


@pytest.fixture
def client():
    """Create test client."""
    # Mock the app import for testing
    from unittest.mock import Mock, patch
    
    # Create a minimal mock for testing
    with patch('app._graph') as mock_graph, \
         patch('app._redis') as mock_redis, \
         patch('app._trace_service') as mock_trace:
        
        # Configure mocks
        mock_graph.get_state.return_value = None
        mock_redis.available = True
        mock_redis.health_check.return_value = {"status": "ok"}
        
        import app
        from app import ChatHandler
        import io
        
        # Create test client using httpx or requests
        try:
            import httpx
            with httpx.Client(app=app.app, base_url="http://test") as client:
                yield client
        except ImportError:
            # Fallback: skip if httpx not available
            pytest.skip("httpx required for testing")


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
        assert "database" in data or "requests" in data
    
    def test_health_llm_status(self, client):
        """Health response should include LLM connectivity status."""
        response = client.get("/api/health")
        data = response.json()
        llm = data["llm"]
        
        assert "reachable" in llm
        assert "url" in llm
        assert isinstance(llm["reachable"], bool)
    
    def test_health_response_time(self, client):
        """Health endpoint should respond within 100ms."""
        start = time.time()
        response = client.get("/api/health")
        elapsed = (time.time() - start) * 1000
        
        assert response.status_code == 200
        assert elapsed < 100, f"Health check took {elapsed}ms (should be < 100ms)"


class TestReadyEndpoint:
    """Test /api/ready endpoint."""
    
    def test_ready_returns_200(self, client):
        """Ready endpoint should return 200 OK."""
        response = client.get("/api/ready")
        assert response.status_code == 200
    
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
        
        # Should check critical dependencies
        assert "llm" in checks or len(checks) > 0
        
        for check_name, check_data in checks.items():
            assert "status" in check_data
            assert check_data["status"] in ["ok", "error"]


class TestMetricsEndpoint:
    """Test /api/metrics endpoint."""
    
    def test_metrics_returns_200(self, client):
        """Metrics endpoint should return 200 OK."""
        response = client.get("/api/metrics")
        assert response.status_code == 200
    
    def test_metrics_content_type(self, client):
        """Metrics should have Prometheus content type."""
        response = client.get("/api/metrics")
        assert "text/plain" in response.headers["content-type"]
    
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
            "langgraph_graph_executions_total",
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
