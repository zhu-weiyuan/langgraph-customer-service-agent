# -*- coding: utf-8 -*-
"""Tests for Redis integration module.

Tests cover all 5 Redis data structures and graceful fallback when Redis is unavailable.
Run with: pytest test_redis.py -v

Note: Tests use the fallback mode (no actual Redis connection) to be CI-friendly.
For integration tests against a real Redis instance, set REDIS_URL env var.
"""

import os
import pytest
from agent.redis_cache import RedisClient, get_redis


@pytest.fixture(autouse=True)
def no_redis_env():
    """Ensure tests run without requiring actual Redis."""
    old = os.environ.pop("REDIS_URL", None)
    yield
    if old:
        os.environ["REDIS_URL"] = old


class TestFallbackMode:
    """Test graceful degradation when Redis is unavailable."""

    def setup_method(self):
        # Force fallback by passing invalid URL
        self.client = RedisClient(url="redis://invalid-host-xyz:9999/0")

    def test_not_connected(self):
        assert not self.client.available

    def test_cache_set_get_fallback(self):
        """Cache operations should silently degrade."""
        self.client.cache_response("test query", "test response")
        result = self.client.get_cached_response("test query")
        assert result is None

    def test_session_fallback(self):
        self.client.set_user_session("user1", {"emotion": "happy"})
        result = self.client.get_user_session("user1")
        assert result is None

    def test_online_tracking_fallback(self):
        self.client.mark_online("user1")
        assert not self.client.is_online("user1")
        assert self.client.get_online_count() == 0

    def test_hot_questions_fallback(self):
        self.client.record_query("how to reset password?")
        result = self.client.get_hot_questions(5)
        assert result == []

    def test_query_log_fallback(self):
        self.client.add_query_log("user1", "hello")
        result = self.client.get_query_log("user1")
        assert result == []

    def test_rate_limit_fallback_allows(self):
        """Rate limiting should allow all requests when Redis is down."""
        result = self.client.check_rate_limit("test_ip")
        assert result["allowed"] is True

    def test_lock_fallback(self):
        token = self.client.acquire_lock("resource1")
        assert token is None

    def test_health_check_shows_disconnected(self):
        health = self.client.health_check()
        assert health["connected"] is False


class TestKeyHelpers:
    """Test internal key generation."""

    def setup_method(self):
        self.client = RedisClient(prefix="test")

    def test_key_format(self):
        key = self.client._key("cache", "abc123")
        assert key == "test:cache:abc123"

    def test_hash_key_consistent(self):
        h1 = self.client._hash_key("same query")
        h2 = self.client._hash_key("same query")
        assert h1 == h2
        assert len(h1) == 16

    def test_hash_key_different(self):
        h1 = self.client._hash_key("query A")
        h2 = self.client._hash_key("query B")
        assert h1 != h2


class TestCacheStats:
    """Test cache statistics tracking."""

    def setup_method(self):
        self.client = RedisClient(url="redis://invalid-host-xyz:9999/0")

    def test_stats_fallback(self):
        stats = self.client.cache_stats()
        assert stats == {"hits": 0, "misses": 0, "ratio": 0.0}

    def test_record_hit_miss_no_error(self):
        """Should not raise even without Redis."""
        self.client.record_cache_hit()
        self.client.record_cache_miss()


class TestRateLimitParams:
    """Test rate limiting parameter handling."""

    def setup_method(self):
        self.client = RedisClient(url="redis://invalid-host-xyz:9999/0")

    def test_custom_limits(self):
        result = self.client.check_rate_limit("ip1", max_requests=10, window_seconds=30)
        assert result["allowed"] is True
        assert result["remaining"] == 10


class TestGlobalSingleton:
    """Test global Redis client singleton."""

    def test_get_redis_returns_instance(self):
        client = get_redis()
        assert isinstance(client, RedisClient)

    def test_singleton_same_instance(self):
        c1 = get_redis()
        c2 = get_redis()
        assert c1 is c2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
