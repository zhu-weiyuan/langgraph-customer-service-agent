# -*- coding: utf-8 -*-
"""Pure tests for the PostgreSQL runtime connection-pool adapter."""

from __future__ import annotations

from contextlib import contextmanager

import pytest


class FakeConnection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


class FakePool:
    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = True
        self._nconns = 0
        self._max_size = kwargs["max_size"]
        self._waiting = []
        self.connection = FakeConnection()
        self.open_calls = []
        self.close_calls = 0
        self.__class__.created.append(self)

    def open(self, *, wait, timeout):
        self.open_calls.append((wait, timeout))
        self.closed = False
        self._nconns = self.kwargs["min_size"]

    def getconn(self, *, timeout):
        assert not self.closed
        self._nconns += 1
        return self.connection

    def close(self):
        self.close_calls += 1
        self.closed = True


@pytest.fixture
def runtime_db(monkeypatch):
    import agent.runtime_db as module

    module.close_pools()
    FakePool.created.clear()
    monkeypatch.setattr(module, "ConnectionPool", FakePool)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("PG_POOL_MIN_SIZE", "2")
    monkeypatch.setenv("PG_POOL_MAX_SIZE", "7")
    monkeypatch.setenv("PG_POOL_TIMEOUT_SECONDS", "1.5")
    yield module
    module.close_pools()


def test_connect_reuses_pool_and_separates_autocommit(runtime_db):
    first = runtime_db.connect()
    second = runtime_db.connect()
    auto = runtime_db.connect(autocommit=True)

    assert first is second is FakePool.created[0].connection
    assert len(FakePool.created) == 2
    assert FakePool.created[0].kwargs["min_size"] == 2
    assert FakePool.created[0].kwargs["max_size"] == 7
    assert FakePool.created[0].kwargs["kwargs"]["autocommit"] is False
    assert FakePool.created[1].kwargs["kwargs"]["autocommit"] is True
    assert runtime_db.pool_stats()["transactional"]["max_size"] == 7
    assert runtime_db.pool_stats()["autocommit"]["open"] is True


def test_connection_context_commits_and_rolls_back(runtime_db):
    conn = runtime_db.connect()
    with runtime_db.connection() as checked_out:
        assert checked_out is conn
    assert conn.commits == 1
    assert conn.closes == 1

    with pytest.raises(ValueError):
        with runtime_db.connection() as checked_out:
            assert checked_out is conn
            raise ValueError("rollback")
    assert conn.rollbacks == 1
    assert conn.closes == 2


def test_close_pools_allows_clean_recreation(runtime_db):
    runtime_db.connect()
    old = FakePool.created[0]
    runtime_db.close_pools()
    assert old.close_calls == 1
    assert runtime_db.pool_stats() == {}

    runtime_db.connect()
    assert len(FakePool.created) == 2
    assert FakePool.created[1].closed is False

