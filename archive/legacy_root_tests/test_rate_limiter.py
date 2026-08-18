# -*- coding: utf-8 -*-
"""Unit tests for admission control in the multi-level rate limiter."""

import threading

import pytest

from agent.rate_limiter import MultiLevelRateLimiter, RateLimitError


def make_limiter(max_concurrent_requests=2):
    """Use generous token budgets so these tests isolate concurrency behavior."""
    return MultiLevelRateLimiter(
        user_max_requests=100,
        vendor_rate=10_000,
        vendor_capacity=10_000,
        max_concurrent_requests=max_concurrent_requests,
    )


def test_concurrency_slots_are_held_until_release():
    limiter = make_limiter(max_concurrent_requests=2)

    limiter.acquire("user-1")
    limiter.acquire("user-2")
    assert limiter.get_stats()["active_requests"] == 2
    assert limiter.get_stats()["available_concurrency"] == 0

    with pytest.raises(RateLimitError, match="正在处理的请求过多"):
        limiter.acquire("user-3")

    limiter.release()
    assert limiter.get_stats()["active_requests"] == 1
    limiter.acquire("user-3")
    assert limiter.get_stats()["active_requests"] == 2


def test_release_is_idempotent_for_unmatched_calls():
    limiter = make_limiter(max_concurrent_requests=1)

    # A handler that was rejected must not expand capacity during cleanup.
    limiter.release()
    limiter.acquire("user-1")
    with pytest.raises(RateLimitError):
        limiter.acquire("user-2")

    limiter.release()
    limiter.release()
    limiter.acquire("user-2")
    assert limiter.get_stats()["active_requests"] == 1


def test_stats_expose_concurrency_capacity_and_utilization_inputs():
    limiter = make_limiter(max_concurrent_requests=4)
    limiter.acquire("user-1")

    stats = limiter.get_stats()
    assert stats["active_requests"] == 1
    assert stats["max_concurrent_requests"] == 4
    assert stats["available_concurrency"] == 3

    limiter.release()


def test_concurrency_cap_is_thread_safe():
    limiter = make_limiter(max_concurrent_requests=3)
    barrier = threading.Barrier(8)
    admitted = []
    admitted_lock = threading.Lock()

    def try_acquire(index):
        barrier.wait()
        try:
            limiter.acquire(f"user-{index}")
        except RateLimitError:
            return
        with admitted_lock:
            admitted.append(index)

    threads = [threading.Thread(target=try_acquire, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 3
    assert limiter.get_stats()["active_requests"] == 3

    for _ in admitted:
        limiter.release()
    assert limiter.get_stats()["active_requests"] == 0


def test_concurrency_rejection_does_not_consume_user_quota():
    limiter = MultiLevelRateLimiter(
        user_max_requests=1,
        user_window_seconds=60,
        vendor_rate=10_000,
        vendor_capacity=10_000,
        max_concurrent_requests=1,
    )

    limiter.acquire("active-user")
    with pytest.raises(RateLimitError, match="系统繁忙"):
        limiter.acquire("waiting-user")

    limiter.release()
    # The earlier overload rejection did not spend waiting-user's only request.
    limiter.acquire("waiting-user")
    limiter.release()
