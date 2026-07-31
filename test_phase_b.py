# -*- coding: utf-8 -*-
import time


def test_circuit_opens_per_model_and_recovers():
    from agent.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
    now = [0.0]
    breaker = CircuitBreaker(2, 10, clock=lambda: now[0])
    breaker.record_failure("provider", "model-a")
    breaker.record_failure("provider", "model-a")
    assert breaker.state("provider", "model-a") == CircuitState.OPEN
    try:
        breaker.allow_request("provider", "model-a")
        assert False, "open circuit should reject"
    except CircuitOpenError:
        pass
    breaker.allow_request("provider", "model-b")
    now[0] = 11
    breaker.allow_request("provider", "model-a")
    breaker.record_success("provider", "model-a")
    assert breaker.state("provider", "model-a") == CircuitState.CLOSED


async def _async_fail(*args, **kwargs):
    raise ConnectionError("down")


def test_gateway_returns_degraded_result_when_candidates_fail(monkeypatch):
    """When all models fail, gateway raises AllModelsFailedError (not a degraded response)."""
    from agent.llm_gateway import LLMGateway, GatewayRequest, ModelProfile, AllModelsFailedError
    model = ModelProfile("test", "test", "http://localhost", "test-key", "x", 4096, 100,
                         "balanced", 0, 0)
    gateway = LLMGateway([model])
    monkeypatch.setattr(gateway, "_call_model", _async_fail)
    import pytest
    with pytest.raises(AllModelsFailedError, match="All models in fallback chain failed"):
        gateway.chat_sync(GatewayRequest(messages=[{"role": "user", "content": "hi"}], tenant_id="free"))


def test_context_monitor_thresholds(caplog):
    from agent.context_monitor import TokenEstimator
    estimator = TokenEstimator()
    warning = estimator.monitor(system_prompt="x" * 140, context_window=100)
    assert warning.level == "warning"
    dumb_zone = estimator.monitor(system_prompt="x" * 180, context_window=100)
    assert dumb_zone.level == "dumb_zone"


def test_prompt_registry_versions_and_required_variables():
    from agent.prompt_registry import PromptRegistry
    registry = PromptRegistry()
    v1 = registry.register("reply", "Hello {customer}", change_reason="initial")
    text, got = registry.render("reply", {"customer": "Ada"})
    assert text == "Hello Ada"
    assert got.version_no == v1.version_no == 1
    try:
        registry.render("reply", {})
        assert False, "required prompt variable must be validated"
    except ValueError:
        pass


def test_high_risk_tool_requires_confirmation(caplog):
    from agent.tool_registry import ToolCallContext, ToolRegistry, ToolRiskLevel
    registry = ToolRegistry()

    @registry.register("delete_customer", risk_level=ToolRiskLevel.TOOL_WRITE_HIGH)
    def delete_customer():
        return "deleted"

    try:
        registry.execute("delete_customer")
        assert False, "confirmation is required"
    except PermissionError:
        pass
    assert registry.execute("delete_customer", context=ToolCallContext(confirmed=True, request_id="r1")) == "deleted"
    assert "High-risk tool audit" in caplog.text


def test_app_degraded_response_contract():
    from app import ChatHandler
    handler = object.__new__(ChatHandler)
    result = handler._degraded_response("sensitive user input", "session-1", "request-1")
    assert result["fallback"] is True
    assert result["request_id"] == "request-1"
    assert "暂时不可用" in result["error"]
