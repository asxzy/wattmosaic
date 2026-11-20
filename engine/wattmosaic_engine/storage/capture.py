"""Batched, independently committed purge of the raw ``ct_samples`` store.

The upgrade path can leave several gigabytes of canonical CT readings in
``ct_samples`` (ADR-0042).  A single ``DELETE`` over that many rows would build
one huge WAL transaction and hold the writer lock for far too long.  This module
drains the table in bounded batches, each in its own short ``BEGIN IMMEDIATE``
transaction, releasing the lock between batches so the realtime writer loop can
interleave (design.md §3.2, DEC-37).

The function is deliberately pure storage: callers pass ``db._conn`` and
``db._lock`` (the same seam ``_prune_evidence`` uses) and an optional injected
``monotonic`` clock, so behaviour is deterministic under test and this module
never imports ``core/``, ``realtime/`` or ``inference/``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .repositories import iso

__all__ = ["PurgeResult", "purge_ct_samples"]


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Outcome of one purge call.

    ``done`` is ``True`` when the last batch deleted fewer than ``batch_rows``
    (no eligible row remains) or the table is missing; it is ``False`` when a
    ``max_batches`` or ``time_budget_s`` limit stopped the pass early, so the
    caller knows to schedule another maintenance tick.
    """

    deleted: int
    batches: int
    done: bool


def purge_ct_samples(
    conn: sqlite3.Connection,
    lock: threading.RLock | threading.Lock,
    *,
    cutoff: datetime | None,
    batch_rows: int = 5000,
    max_batches: int | None = 20,
    time_budget_s: float | None = 2.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> PurgeResult:
    """Delete aged ``ct_samples`` rows in bounded, committed batches.

    Each batch acquires ``lock``, opens ``BEGIN IMMEDIATE``, deletes at most
    ``batch_rows`` rows with the lowest ``id`` whose ``received_at < cutoff``
    (every row when ``cutoff is None``), commits and releases the lock.  The
    ascending-``id`` window uses the integer primary key so the scan stays
    ordered even though the age filter is on ``received_at``; rows are appended
    in time order, so lowest ``id`` is the oldest.

    The pass stops when a batch deletes fewer than ``batch_rows`` (``done=True``),
    or when ``max_batches`` transactions or ``time_budget_s`` seconds have
    elapsed (``done=False``; a later call resumes).  On any SQL error the open
    transaction is rolled back, the lock released and the exception re-raised so
    the caller can degrade gracefully.  A missing table is a no-op
    (:class:`PurgeResult` with ``done=True``), which keeps startup against an old
    or partially migrated database safe.

    ``cutoff`` is converted with :func:`~wattmosaic_engine.storage.repositories.iso`
    to the same ISO-8601 UTC string the tier prune compares against.
    """
    if batch_rows < 1:
        raise ValueError("batch_rows must be >= 1")
    if max_batches is not None and max_batches < 1:
        raise ValueError("max_batches must be None or >= 1")

    cutoff_iso = iso(cutoff)
    if cutoff_iso is None:
        where = ""
        params: tuple[object, ...] = ()
    else:
        where = " WHERE received_at < ?"
        params = (cutoff_iso,)

    delete_sql = (
        "DELETE FROM ct_samples WHERE id IN ("
        "SELECT id FROM ct_samples" + where + " ORDER BY id LIMIT ?)"
    )
    insert_params: tuple[object, ...] = (*params, batch_rows)

    start = monotonic()
    deleted = 0
    batches = 0

    while True:
        with lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cursor = conn.execute(delete_sql, insert_params)
                    removed = cursor.rowcount
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            except sqlite3.OperationalError as exc:
                # A missing table (e.g. a fresh or pre-migration database) is a
                # no-op; re-raise every other operational error.  If the failure
                # happened after BEGIN, ROLLBACK above already cleaned up.
                if "no such table" not in str(exc).lower():
                    raise
                return PurgeResult(0, 0, True)

        deleted += removed
        batches += 1

        # A short final batch proves no eligible row is left.
        if removed < batch_rows:
            return PurgeResult(deleted, batches, True)
        if max_batches is not None and batches >= max_batches:
            return PurgeResult(deleted, batches, False)
        if time_budget_s is not None and monotonic() - start >= time_budget_s:
            return PurgeResult(deleted, batches, False)
