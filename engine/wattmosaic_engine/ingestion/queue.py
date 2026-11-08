"""Bounded, drop-oldest queues used between ingestion and processing.

The engine must never let a slow consumer grow memory without limit.  These
queues apply backpressure by discarding the **oldest** item and counting the
loss; they never block the producer (which is the HA WebSocket reader) and never
raise.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Generic, TypeVar

T = TypeVar("T")


class BoundedQueue(Generic[T]):
    """A bounded async queue with drop-oldest semantics and counters."""

    def __init__(self, maxsize: int = 5000) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = int(maxsize)
        self._items: deque[T] = deque()
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Event()
        self._closed = False
        self.total_enqueued = 0
        self.total_dequeued = 0
        self.total_dropped = 0

    def put_nowait(self, item: T) -> bool:
        """Enqueue without waiting.  Returns False when an old item was dropped."""
        if self._closed:
            self.total_dropped += 1
            return False
        dropped = False
        if len(self._items) >= self.maxsize:
            self._items.popleft()
            self.total_dropped += 1
            dropped = True
        self._items.append(item)
        self.total_enqueued += 1
        self._not_empty.set()
        return not dropped

    async def put(self, item: T) -> bool:
        return self.put_nowait(item)

    async def get(self) -> T | None:
        """Wait for an item.  Returns ``None`` once the queue is closed/drained."""
        while True:
            if self._items:
                item = self._items.popleft()
                self.total_dequeued += 1
                if not self._items:
                    self._not_empty.clear()
                return item
            if self._closed:
                return None
            self._not_empty.clear()
            await self._not_empty.wait()

    def get_nowait(self) -> T | None:
        if not self._items:
            return None
        item = self._items.popleft()
        self.total_dequeued += 1
        if not self._items:
            self._not_empty.clear()
        return item

    def drain(self, limit: int | None = None) -> list[T]:
        out: list[T] = []
        while self._items and (limit is None or len(out) < limit):
            out.append(self._items.popleft())
            self.total_dequeued += 1
        if not self._items:
            self._not_empty.clear()
        return out

    def close(self) -> None:
        self._closed = True
        self._not_empty.set()

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        return len(self._items)

    @property
    def depth(self) -> int:
        return len(self._items)

    @property
    def full(self) -> bool:
        return len(self._items) >= self.maxsize

    def stats(self) -> dict[str, int]:
        return {
            "depth": len(self._items),
            "maxsize": self.maxsize,
            "enqueued": self.total_enqueued,
            "dequeued": self.total_dequeued,
            "dropped": self.total_dropped,
            "closed": int(self._closed),
        }


__all__ = ["BoundedQueue"]
