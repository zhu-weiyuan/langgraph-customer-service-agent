# -*- coding: utf-8 -*-
"""Pure tests for bounded, best-effort background work."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.background_queue import BoundedAsyncJobQueue  # noqa: E402


class TestBoundedAsyncJobQueue(unittest.IsolatedAsyncioTestCase):
    async def test_submit_before_start_is_dropped(self):
        queue = BoundedAsyncJobQueue(lambda _item: asyncio.sleep(0), maxsize=1)

        self.assertFalse(queue.try_submit("not-running"))
        stats = queue.stats()
        self.assertEqual(stats["dropped_total"], 1)
        self.assertFalse(stats["running"])

    async def test_process_and_drain(self):
        seen = []

        async def worker(item):
            seen.append(item)

        queue = BoundedAsyncJobQueue(worker, maxsize=2, name="test")
        await queue.start()
        self.assertTrue(queue.try_submit("a"))
        self.assertTrue(queue.try_submit("b"))
        self.assertTrue(await queue.drain(timeout=1))

        stats = queue.stats()
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(stats["enqueued_total"], 2)
        self.assertEqual(stats["processed_total"], 2)
        self.assertEqual(stats["failed_total"], 0)
        self.assertTrue(await queue.stop(timeout=1))
        self.assertFalse(queue.stats()["running"])

    async def test_full_queue_returns_immediately_and_counts_drop(self):
        worker_started = asyncio.Event()
        release_worker = asyncio.Event()

        async def worker(_item):
            worker_started.set()
            await release_worker.wait()

        queue = BoundedAsyncJobQueue(worker, maxsize=1, name="full")
        await queue.start()
        self.assertTrue(queue.try_submit("running"))
        await asyncio.wait_for(worker_started.wait(), timeout=1)

        # The worker is busy, so one item occupies the only queue slot and the
        # next item must be rejected without waiting.
        self.assertTrue(queue.try_submit("queued"))
        self.assertFalse(queue.try_submit("dropped"))
        self.assertEqual(queue.stats()["dropped_total"], 1)

        release_worker.set()
        self.assertTrue(await queue.stop(timeout=1))
        stats = queue.stats()
        self.assertEqual(stats["processed_total"], 2)
        self.assertFalse(stats["running"])

    async def test_worker_failure_is_accounted_and_worker_survives(self):
        seen = []

        async def worker(item):
            if item == "bad":
                raise RuntimeError("expected test failure")
            seen.append(item)

        queue = BoundedAsyncJobQueue(worker, maxsize=2, name="failure")
        await queue.start()
        self.assertTrue(queue.try_submit("bad"))
        self.assertTrue(queue.try_submit("good"))
        self.assertTrue(await queue.drain(timeout=1))

        stats = queue.stats()
        self.assertEqual(seen, ["good"])
        self.assertEqual(stats["failed_total"], 1)
        self.assertEqual(stats["processed_total"], 1)
        self.assertTrue(stats["running"])
        await queue.stop(timeout=1)

    async def test_multiple_workers_process_jobs(self):
        started = 0
        release = asyncio.Event()

        async def worker(_item):
            nonlocal started
            started += 1
            await release.wait()

        queue = BoundedAsyncJobQueue(worker, maxsize=4, workers=2,
                                     name="parallel")
        await queue.start()
        self.assertTrue(queue.try_submit(1))
        self.assertTrue(queue.try_submit(2))
        for _ in range(20):
            if started == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(started, 2)
        release.set()
        self.assertTrue(await queue.stop(timeout=1))
        self.assertEqual(queue.stats()["worker_count"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
