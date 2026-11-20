"""Background compute executor for the three-loop runtime (P6, DEC-37).

One worker thread runs all off-loop jobs (learning, residual/prior refresh,
ledger flush, unknown profiles, configuration detectors, matrix reconcile and
match expiry).  A job is handed a :class:`JobBudget` so a cooperative job can
split its work into chunks no longer than ``max_chunk_s`` (2 s by default) and
yield the thread between them: DEC-37 caps *per-job CPU*, not wall-clock, and a
single greedy job must never starve ``/healthz`` (the ADR-0033 outage class).

The executor is deliberately tiny and dependency-free:

* :class:`JobBudget` is a pure time-source-injected budget.  Tests inject a
  fake clock so chunking is deterministic without sleeping.
* :class:`BackgroundExecutor` satisfies the ``Executor.submit`` seam the L6
  :class:`~wattmosaic_engine.learning.worker.LearningWorker` expects, so P6 can
  inject it directly.
* Production code never imports ``runtime.fakes``; the module reads no wall
  clock except the injected default ``time.monotonic`` used to measure chunks.

No DB is touched here; jobs own their transactions and write through the
persist queue or their own short transactions (single-writer rule ADR-0004).
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_MAX_CHUNK_S",
    "ChunkResult",
    "BackgroundExecutor",
    "JobBudget",
]

log = logging.getLogger("wattmosaic.runtime")

#: DEC-37: a single job may consume at most this much CPU before yielding.
DEFAULT_MAX_CHUNK_S = 2.0


@dataclass(frozen=True)
class ChunkResult:
    """The outcome of one cooperative chunk, returned by a job step."""

    done: bool
    detail: str = ""
    value: Any = None


class JobBudget:
    """A cooperative CPU budget handed to background jobs.

    ``now`` is injected (default ``time.monotonic``) and ``max_chunk_s`` bounds
    one chunk.  A pure job checks :meth:`exceeded` between units of work and
    returns ``ChunkResult(done=False)`` so the executor can reschedule it, which
    keeps every individual chunk at or below the budget.  ``remaining`` stays
    ``None``-free: it is a duration, always a finite non-negative float.
    """

    __slots__ = ("_now", "_start", "_max_chunk_s", "_chunk_starts", "_chunks")

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        max_chunk_s: float = DEFAULT_MAX_CHUNK_S,
    ) -> None:
        self._now = now or time.monotonic
        self._max_chunk_s = max(0.0, float(max_chunk_s))
        self._start = self._now()
        self._chunk_starts = self._now()
        self._chunks = 0

    @property
    def max_chunk_s(self) -> float:
        return self._max_chunk_s

    @property
    def chunks(self) -> int:
        """How many chunks this job has run (observable, never guessed)."""
        return self._chunks

    @property
    def elapsed_s(self) -> float:
        return max(0.0, self._now() - self._start)

    def start_chunk(self) -> None:
        """Mark the start of a fresh chunk (called by the executor)."""
        self._chunk_starts = self._now()
        self._chunks += 1

    def exceeded(self) -> bool:
        """True when the current chunk has used its CPU budget."""
        return (self._now() - self._chunk_starts) >= self._max_chunk_s

    def remaining_s(self) -> float:
        """Seconds left in the current chunk (never negative)."""
        return max(0.0, self._max_chunk_s - (self._now() - self._chunk_starts))


class BackgroundExecutor:
    """A single-thread executor with a per-job CPU chunk budget.

    Parameters
    ----------
    max_chunk_s:
        DEC-37 CPU cap handed to cooperative jobs via :class:`JobBudget`.
    max_workers:
        Always 1: the whole point of DEC-37 is a single background worker.
        Kept as a parameter so a test can assert the configured value, not to
        allow more threads in production.
    """

    def __init__(
        self,
        *,
        max_chunk_s: float = DEFAULT_MAX_CHUNK_S,
        now: Callable[[], float] | None = None,
        max_workers: int = 1,
    ) -> None:
        self.max_chunk_s = float(max_chunk_s)
        self._now = now or time.monotonic
        self.max_workers = max(1, int(max_workers))
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="wm-background"
        )
        self.submitted = 0
        self.completed = 0
        self.failed = 0
        self.last_error: str | None = None
        self._jobs: list[Future[Any]] = []
        self._lock = threading.Lock()
        self._closed = False

    def _wrap(self, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Run one job with a fresh budget, handed as ``budget=`` when accepted."""
        budget = JobBudget(now=self._now, max_chunk_s=self.max_chunk_s)
        budget.start_chunk()
        try:
            if _accepts_budget(fn):
                return fn(*args, budget=budget, **kwargs)
            return fn(*args, **kwargs)
        finally:
            with self._lock:
                self.completed += 1

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        """Submit one job to the single background thread."""
        if self._closed:
            raise RuntimeError("background executor is shut down")
        with self._lock:
            self.submitted += 1
        future = self._pool.submit(self._wrap, fn, args, kwargs)
        # Track before the callback is attached: a job that already finished
        # runs _on_done immediately, and must find (and drop) its entry.
        with self._lock:
            self._jobs.append(future)
        future.add_done_callback(self._on_done)
        return future

    def _on_done(self, future: Future[Any]) -> None:
        # A finished job is no longer pending: keeping every Future (and its
        # result) forever leaks memory and makes drain_background O(jobs ever).
        with self._lock, contextlib.suppress(ValueError):
            self._jobs.remove(future)
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001 - a failed job must not kill the worker
            with self._lock:
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("background job failed")

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop accepting work and drain the worker thread."""
        self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=not wait)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "submitted": self.submitted,
                "completed": self.completed,
                "failed": self.failed,
                "last_error": self.last_error,
                "max_chunk_s": self.max_chunk_s,
                "max_workers": self.max_workers,
                "closed": self._closed,
            }


def _accepts_budget(fn: Callable[..., Any]) -> bool:
    """Whether ``fn`` declares a ``budget`` keyword (or ``**kwargs``)."""
    try:
        import inspect

        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    for param in params:
        if param.name == "budget":
            return True
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False
