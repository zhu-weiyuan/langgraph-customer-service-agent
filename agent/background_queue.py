"""Small bounded asyncio job queue used for non-critical side effects.

The queue deliberately exposes a non-blocking ``try_submit`` method.  A full
queue must never hold up the request that produced an auxiliary event.  The
caller can inspect the counters through ``stats`` and drain the queue during
graceful shutdown.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable, Generic, Optional, TypeVar

logger = logging.getLogger("agent.background_queue")

T = TypeVar("T")


class BoundedAsyncJobQueue(Generic[T]):
    """Bounded asyncio job queue with loss accounting for best-effort jobs."""

    def __init__(self, worker: Callable[[T], Awaitable[None]], *,
                 maxsize: int = 1024, name: str = "background",
                 workers: int = 1) -> None:
        self._worker = worker
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=max(1, int(maxsize)))
        self._name = name
        self._worker_count = max(1, int(workers))
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._enqueued = 0
        self._dropped = 0
        self._processed = 0
        self._failed = 0

    async def start(self) -> None:
        if any(not task.done() for task in self._tasks):
            return
        self._started = True
        self._tasks = [
            asyncio.create_task(self._run(), name=f"{self._name}-worker-{i + 1}")
            for i in range(self._worker_count)
        ]

    def try_submit(self, item: T) -> bool:
        """Submit without waiting; return False when not started or full."""
        if not self._started or not any(not task.done() for task in self._tasks):
            self._dropped += 1
            return False
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._dropped += 1
            return False
        self._enqueued += 1
        return True

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._worker(item)
                self._processed += 1
            except asyncio.CancelledError:
                # Keep the item accounted for before propagating cancellation.
                raise
            except Exception:
                self._failed += 1
                logger.warning("%s background job failed", self._name,
                               exc_info=True)
            finally:
                self._queue.task_done()

    async def drain(self, timeout: Optional[float] = None) -> bool:
        """Wait for submitted work; return False if the deadline expires."""
        if not self._tasks:
            return True
        try:
            waiter = self._queue.join()
            if timeout is None:
                await waiter
            else:
                await asyncio.wait_for(waiter, timeout=max(0.0, timeout))
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self, timeout: Optional[float] = None) -> bool:
        """Drain best-effort work, then stop the worker task."""
        drained = await self.drain(timeout)
        if not drained:
            # The remaining items are explicitly best-effort.  Account for
            # them before cancelling the worker so shutdown cannot leave a
            # misleading queue size or silently lose the drop metric.
            pending = 0
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    self._queue.task_done()
                    pending += 1
            self._dropped += pending
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._started = False
        self._tasks = []
        return drained

    def stats(self) -> dict:
        return {
            "queue_size": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "worker_count": self._worker_count,
            "enqueued_total": self._enqueued,
            "processed_total": self._processed,
            "dropped_total": self._dropped,
            "failed_total": self._failed,
            "running": bool(self._started and any(
                not task.done() for task in self._tasks)),
        }


__all__ = ["BoundedAsyncJobQueue"]
