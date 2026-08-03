# -*- coding: utf-8 -*-
"""Pure tests for readiness/health/metrics cache behavior.

These tests never call the LLM, Redis, or PostgreSQL.  They exercise the
single-flight guarantees that protect those dependencies during probe bursts.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def app_module(monkeypatch):
    import app_fastapi

    monkeypatch.setattr(app_fastapi, "_readiness_cache", None)
    monkeypatch.setattr(app_fastapi, "_readiness_lock", None)
    monkeypatch.setattr(app_fastapi, "_readiness_lock_loop", None)
    monkeypatch.setattr(app_fastapi, "_health_cache", None)
    monkeypatch.setattr(app_fastapi, "_health_lock", None)
    monkeypatch.setattr(app_fastapi, "_health_lock_loop", None)
    monkeypatch.setattr(app_fastapi, "_metrics_cache", None)
    monkeypatch.setattr(app_fastapi, "_metrics_lock", None)
    monkeypatch.setattr(app_fastapi, "_metrics_lock_loop", None)
    monkeypatch.setattr(app_fastapi, "READINESS_CACHE_SECONDS", 30.0)
    monkeypatch.setattr(app_fastapi, "READINESS_PROBE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(app_fastapi, "HEALTH_CACHE_SECONDS", 30.0)
    monkeypatch.setattr(app_fastapi, "METRICS_CACHE_SECONDS", 30.0)
    monkeypatch.setattr(app_fastapi.runner, "_graph", object())
    monkeypatch.setattr(app_fastapi, "_redis_available", True)
    return app_fastapi


@pytest.mark.asyncio
async def test_readiness_concurrent_requests_probe_database_once(app_module, monkeypatch):
    calls = 0

    async def fake_db_read(_fn):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"db": "langgraph", "vector": True}

    monkeypatch.setattr(app_module, "db_read", fake_db_read)
    responses = await asyncio.gather(*(app_module.ready() for _ in range(50)))

    assert calls == 1
    assert {response.status_code for response in responses} == {200}
    assert sum(response.headers["X-Readiness-Cache"] == "refresh"
               for response in responses) == 1
    assert sum(response.headers["X-Readiness-Cache"] == "hit"
               for response in responses) == 49


@pytest.mark.asyncio
async def test_readiness_failure_is_cached(app_module, monkeypatch):
    calls = 0

    async def failing_db_read(_fn):
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(app_module, "db_read", failing_db_read)
    first, second = await asyncio.gather(app_module.ready(), app_module.ready())

    assert calls == 1
    assert first.status_code == second.status_code == 503
    assert json.loads(first.body)["ready"] is False
    assert second.headers["X-Readiness-Cache"] == "hit"


@pytest.mark.asyncio
async def test_readiness_probe_timeout_returns_503(app_module, monkeypatch):
    async def slow_db_read(_fn):
        await asyncio.sleep(0.05)
        return {"db": "langgraph", "vector": True}

    monkeypatch.setattr(app_module, "db_read", slow_db_read)
    monkeypatch.setattr(app_module, "READINESS_PROBE_TIMEOUT_SECONDS", 0.001)

    response = await app_module.ready()

    assert response.status_code == 503
    assert json.loads(response.body)["checks"]["postgresql"]["ok"] is False


@pytest.mark.asyncio
async def test_health_concurrent_requests_are_single_flight(app_module, monkeypatch):
    llm_calls = 0
    db_calls = 0

    async def fake_llm_reachable():
        nonlocal llm_calls
        llm_calls += 1
        await asyncio.sleep(0.01)
        return True

    async def fake_db_read(_fn):
        nonlocal db_calls
        db_calls += 1
        await asyncio.sleep(0.01)
        return {"total_conversations": 2,
                "ratings": {"total": 1, "average": 5.0}}

    monkeypatch.setattr(app_module, "_llm_reachable", fake_llm_reachable)
    monkeypatch.setattr(app_module, "db_read", fake_db_read)
    monkeypatch.setattr(app_module, "get_rate_limiter",
                        lambda: SimpleNamespace(get_stats=lambda: {"ok": True}))
    responses = await asyncio.gather(*(app_module.health() for _ in range(20)))

    assert llm_calls == 1
    assert db_calls == 1
    assert {response.status_code for response in responses} == {200}
    assert sum(response.headers["X-Health-Cache"] == "refresh"
               for response in responses) == 1


@pytest.mark.asyncio
async def test_metrics_cache_reuses_rendered_snapshot(app_module, monkeypatch):
    calls = 0

    def fake_render():
        nonlocal calls
        calls += 1
        return "# TYPE test counter\ntest 1\n", "text/plain"

    monkeypatch.setattr(app_module.metrics, "render", fake_render)
    request = app_module.Request({
        "type": "http",
        "method": "GET",
        "path": "/api/metrics",
        "query_string": b"format=prometheus",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
    })

    first = await app_module.metrics_endpoint(request)
    second = await app_module.metrics_endpoint(request)

    assert calls == 1
    assert first.headers["X-Metrics-Cache"] == "refresh"
    assert second.headers["X-Metrics-Cache"] == "hit"
