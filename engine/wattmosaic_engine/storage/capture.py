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


def _batch_select_sql(cutoff_iso: str | None) -> tuple[str, tuple[object, ...]]:
    """The bounded SELECT whose rows a single batch deletes.

    With a cutoff, order by ``received_at, id`` so ``idx_ct_samples_time``
    answers both the range filter and the order: SQLite walks the index in age
    order and stops as soon as no row older than the cutoff is left.  With no
    cutoff there is no age key to seek, so the integer primary key (rows are
    appended in time order) gives the oldest-first window instead.

    Kept as a separate helper so the EXPLAIN-QUERY-PLAN test can assert the
    chosen access path against exactly the SQL the purge executes.
    """
    if cutoff_iso is None:
        return "SELECT id FROM ct_samples ORDER BY id LIMIT ?", ()
    return (
        "SELECT id FROM ct_samples WHERE received_at < ? ORDER BY received_at, id LIMIT ?",
        (cutoff_iso,),
    )


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
    ``batch_rows`` rows with the lowest age whose ``received_at < cutoff``
    (every row when ``cutoff is None``), commits and releases the lock.  The
    ascending-age window uses ``idx_ct_samples_time`` when a cutoff is present;
    the oldest-appended ``id`` window is used when there is none.

    The pass stops when a batch deletes fewer than ``batch_rows`` (``done=True``),
    or when ``max_batches`` transactions or ``time_budget_s`` seconds have
    elapsed (``done=False``; a later call resumes).  On any SQL error the open
    transaction is rolled back (guarded by ``in_transaction`` so an automatic
    SQLite rollback cannot mask the original error), the lock released and the
    exception re-raised so the caller can degrade gracefully.  A missing table
    is a no-op; if it disappears after earlier batches committed, the rows
    already deleted are reported rather than discarded.

    ``cutoff`` is converted with :func:`~wattmosaic_engine.storage.repositories.iso`
    to the same ISO-8601 UTC string the tier prune compares against.
    """
    if batch_rows < 1:
        raise ValueError("batch_rows must be >= 1")
    if max_batches is not None and max_batches < 1:
        raise ValueError("max_batches must be None or >= 1")

    cutoff_iso = iso(cutoff)
    select_sql, select_params = _batch_select_sql(cutoff_iso)
    delete_sql = f"DELETE FROM ct_samples WHERE id IN ({select_sql})"
    delete_params: tuple[object, ...] = (*select_params, batch_rows)

    start = monotonic()
    deleted = 0
    batches = 0

    while True:
        with lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cursor = conn.execute(delete_sql, delete_params)
                    removed = cursor.rowcount
                    conn.execute("COMMIT")
                except Exception:
                    # Guard the rollback: if SQLite already rolled the
                    # transaction back, a second ROLLBACK would raise and hide
                    # the original failure from the caller.
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                # The table can vanish after earlier batches committed.  Report
                # the progress this call already made instead of losing it.
                return PurgeResult(deleted, batches, True)

        deleted += removed
        batches += 1

        # A short final batch proves no eligible row is left.
        if removed < batch_rows:
            return PurgeResult(deleted, batches, True)
        if max_batches is not None and batches >= max_batches:
            return PurgeResult(deleted, batches, False)
        if time_budget_s is not None and monotonic() - start >= time_budget_s:
            return PurgeResult(deleted, batches, False)
