# -*- coding: utf-8 -*-
"""
Redis integration for customer service agent.

Features (covers all 5 Redis data structures):
1. String: LLM response cache with TTL
2. Hash: User session state
3. Set: Online users tracking
4. Sorted Set: Hot questions leaderboard
5. List: Recent query log per user

Also includes:
- Rate limiting (sliding window algorithm)
- Distributed lock (SET NX EX)
"""

import json
import time
import hashlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    redis = None  # type: ignore
    REDIS_AVAILABLE = False


class RedisClient:
    """Redis client with graceful fallback when Redis is unavailable.

    All methods silently degrade to no-op or in-memory behavior,
    so the agent works without Redis installed/running.
    """

    def __init__(self, url: str = "redis://localhost:6379/0", prefix: str = "cs"):
        self.prefix = prefix
        self._conn: Optional[redis.Redis] = None
        if REDIS_AVAILABLE:
            try:
                self._conn = redis.from_url(url, decode_responses=True)
                self._conn.ping()
                logger.info(f"Connected to Redis at {url}")
            except Exception as e:
                logger.warning(f"Redis unavailable ({e}), using fallback mode")
                self._conn = None

    @property
    def available(self) -> bool:
        return self._conn is not None

    # ── Key helpers ────────────────────────────────────────

    def _key(self, *parts: str) -> str:
        return f"{self.prefix}:{':'.join(parts)}"

    def _hash_key(self, text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()[:16]

    # ── 1. String: LLM Response Cache ──────────────────────

    def cache_response(self, query: str, response: str, ttl: int = 3600) -> None:
        """Cache an LLM response for deduplication.

        Args:
            query: User's original question (normalized)
            response: LLM's response to cache
            ttl: Time-to-live in seconds (default 1 hour)
        """
        if not self._conn:
            return
        key = self._key("cache", self._hash_key(query))
        try:
            self._conn.setex(key, ttl, json.dumps(response, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"Redis setex failed: {e}")

    def get_cached_response(self, query: str) -> Optional[str]:
        """Get cached response if available. Returns None on cache miss."""
        if not self._conn:
            return None
        key = self._key("cache", self._hash_key(query))
        try:
            val = self._conn.get(key)
            return json.loads(val) if val else None
        except Exception as e:
            logger.debug(f"Redis get failed: {e}")
            return None

    def cache_stats(self) -> dict:
        """Get cache hit/miss statistics."""
        if not self._conn:
            return {"hits": 0, "misses": 0, "ratio": 0.0}
        try:
            hits = int(self._conn.get(self._key("stats", "cache_hits")) or 0)
            misses = int(self._conn.get(self._key("stats", "cache_misses")) or 0)
            total = hits + misses
            return {
                "hits": hits,
                "misses": misses,
                "ratio": round(hits / total * 100, 1) if total > 0 else 0.0,
            }
        except Exception:
            return {"hits": 0, "misses": 0, "ratio": 0.0}

    def record_cache_hit(self) -> None:
        if not self._conn:
            return
        try:
            self._conn.incr(self._key("stats", "cache_hits"))
        except Exception:
            pass

    def record_cache_miss(self) -> None:
        if not self._conn:
            return
        try:
            self._conn.incr(self._key("stats", "cache_misses"))
        except Exception:
            pass

    # ── 2. Hash: User Session State ────────────────────────

    def set_user_session(self, user_id: str, session_data: dict, ttl: int = 1800) -> None:
        """Store user session data as Redis Hash.

        Args:
            user_id: Unique user identifier
            session_data: Dict with session fields (emotion, language, etc.)
            ttl: Time-to-live in seconds (default 30 min)
        """
        if not self._conn:
            return
        key = self._key("session", user_id)
        try:
            pipe = self._conn.pipeline()
            for k, v in session_data.items():
                pipe.hset(key, k, json.dumps(v, ensure_ascii=False))
            pipe.expire(key, ttl)
            pipe.execute()
        except Exception as e:
            logger.debug(f"Redis hset failed: {e}")

    def get_user_session(self, user_id: str) -> Optional[dict]:
        """Retrieve user session data."""
        if not self._conn:
            return None
        key = self._key("session", user_id)
        try:
            data = self._conn.hgetall(key)
            return {k: json.loads(v) for k, v in data.items()} if data else None
        except Exception:
            return None

    def update_session_field(self, user_id: str, field: str, value: Any) -> None:
        """Update a single field in user session hash."""
        if not self._conn:
            return
        key = self._key("session", user_id)
        try:
            self._conn.hset(key, field, json.dumps(value, ensure_ascii=False))
        except Exception:
            pass

    # ── 3. Set: Online Users Tracking ──────────────────────

    def mark_online(self, user_id: str, ttl: int = 300) -> None:
        """Mark a user as online (set with TTL for auto-expiry)."""
        if not self._conn:
            return
        try:
            self._conn.sadd(self._key("online"), user_id)
            self._conn.expire(self._key("online"), ttl)
        except Exception:
            pass

    def is_online(self, user_id: str) -> bool:
        """Check if a user is currently online."""
        if not self._conn:
            return False
        try:
            return bool(self._conn.sismember(self._key("online"), user_id))
        except Exception:
            return False

    def get_online_count(self) -> int:
        """Get the number of currently online users."""
        if not self._conn:
            return 0
        try:
            return self._conn.scard(self._key("online"))
        except Exception:
            return 0

    # ── 4. Sorted Set: Hot Questions Leaderboard ───────────

    def record_query(self, question: str) -> None:
        """Increment the count for a question (sorted set score)."""
        if not self._conn:
            return
        key = self._key("hot_questions")
        try:
            normalized = question.strip().lower()[:100]
            self._conn.zincrby(key, 1, normalized)
            # Keep only top 100
            self._conn.zremrangebyrank(key, 0, -101)
        except Exception:
            pass

    def get_hot_questions(self, top_n: int = 10) -> list[tuple[str, int]]:
        """Get the most frequently asked questions.

        Returns:
            List of (question, count) tuples, sorted by frequency descending.
        """
        if not self._conn:
            return []
        try:
            items = self._conn.zrevrange(self._key("hot_questions"), 0, top_n - 1, withscores=True)
            return [(q, int(c)) for q, c in items]
        except Exception:
            return []

    # ── 5. List: Recent Query Log per User ─────────────────

    def add_query_log(self, user_id: str, query: str, max_len: int = 20) -> None:
        """Add a query to user's recent queries list."""
        if not self._conn:
            return
        key = self._key("log", user_id)
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self._conn.lpush(key, f"[{ts}] {query}")
            self._conn.ltrim(key, 0, max_len - 1)
        except Exception:
            pass

    def get_query_log(self, user_id: str) -> list[str]:
        """Get user's recent query history."""
        if not self._conn:
            return []
        try:
            return self._conn.lrange(self._key("log", user_id), 0, -1)
        except Exception:
            return []

    # ── Rate Limiting (Sliding Window Algorithm) ───────────

    def check_rate_limit(self, identifier: str, max_requests: int = 60, window_seconds: int = 60) -> dict:
        """Check and enforce rate limit using sliding window algorithm.

        Uses Redis sorted set with timestamps as scores for precise sliding window.

        Args:
            identifier: User ID or IP address
            max_requests: Maximum requests allowed in the window
            window_seconds: Time window in seconds

        Returns:
            dict with 'allowed' (bool), 'remaining' (int), 'reset_at' (float)
        """
        if not self._conn:
            return {"allowed": True, "remaining": max_requests, "reset_at": 0}

        key = self._key("ratelimit", identifier)
        now = time.time()
        window_start = now - window_seconds

        try:
            pipe = self._conn.pipeline()
            # Remove expired entries
            pipe.zremrangebyscore(key, 0, window_start)
            # Count current requests in window
            pipe.zcard(key)
            # Add new request
            pipe.zadd(key, {f"{now}:{id(self)}": now})
            # Set expiry to auto-cleanup
            pipe.expire(key, window_seconds + 1)
            results = pipe.execute()

            current_count = results[1]
            allowed = current_count < max_requests

            if not allowed:
                # Remove the entry we just added
                self._conn.zrem(key, f"{now}:{id(self)}")

            remaining = max(0, max_requests - current_count)
            reset_at = window_start + window_seconds

            return {
                "allowed": allowed,
                "remaining": remaining,
                "reset_at": reset_at,
                "current": current_count,
            }
        except Exception as e:
            logger.warning(f"Rate limit check failed: {e}")
            return {"allowed": True, "remaining": max_requests, "reset_at": 0}

    # ── Distributed Lock (SET NX EX) ───────────────────────

    def acquire_lock(self, resource: str, ttl: int = 10) -> Optional[str]:
        """Acquire a distributed lock.

        Args:
            resource: Resource name to lock
            ttl: Lock timeout in seconds

        Returns:
            Lock token string if acquired, None if already locked.
        """
        if not self._conn:
            return None
        key = self._key("lock", resource)
        token = f"{time.time()}:{id(self)}"
        try:
            # SET key value NX EX ttl (atomic)
            acquired = self._conn.set(key, token, nx=True, ex=ttl)
            return token if acquired else None
        except Exception:
            return None

    def release_lock(self, resource: str, token: str) -> bool:
        """Release a distributed lock (only if token matches).

        Uses Lua script for atomic check-and-delete.
        """
        if not self._conn:
            return False
        key = self._key("lock", resource)
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            return bool(self._conn.eval(lua_script, 1, key, token))
        except Exception:
            return False

    # ── Health Check & Debug ───────────────────────────────

    def health_check(self) -> dict:
        """Check Redis connection and return server info."""
        if not self._conn:
            return {"connected": False, "error": "Redis client not available"}
        try:
            ping = self._conn.ping()
            info = self._conn.info("server")
            return {
                "connected": True,
                "ping": ping,
                "redis_version": info.get("redis_version", "unknown"),
                "uptime_seconds": info.get("uptime_in_seconds", 0),
                "used_memory_human": self._conn.info("memory").get("used_memory_human", "?"),
            }
        except Exception as e:
            return {"connected": False, "error": str(e)}


# Global singleton (lazy init)
_redis_client: Optional[RedisClient] = None


def get_redis() -> RedisClient:
    """Get or create the global Redis client."""
    global _redis_client
    if _redis_client is None:
        import os
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _redis_client = RedisClient(url=url)
    return _redis_client
