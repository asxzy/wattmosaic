"""The cached latest-inference-results read: correctness, invalidation, speed.

``Database.latest_inference_results()`` used to run a full-table GROUP BY over
``inference_results`` on every call.  The panel fires it from both ``/snapshot``
and ``/devices`` in a single poll and the overview repeats that every 8 s, so the
same 343k-row scan ran several times per refresh.  It is now cached until the
next committed inference write (or a prune/reset).

Two kinds of evidence live here:

* deterministic structural proof -- a large table is scanned at most once across
  many reads (counted by wrapping ``Database._reader``, which is opened only on a
  cache miss), and every write/prune/reset forces exactly one fresh scan;
* a measured before/after timing comparison on a synthetic >=20k-row table, with
  a generous margin so CI jitter cannot turn it into a false failure.

``InferenceResult`` is the production contract model, so a stale cache that
shadowed a newer write would be caught by the correctness assertions below.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wattmosaic_engine.contracts import InferenceResult, Source
from wattmosaic_engine.storage.database import Database

BASE = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)

RAW_QUERY = """SELECT * FROM inference_results WHERE id IN
(SELECT MAX(id) FROM inference_results GROUP BY device_id) ORDER BY device_id"""


def _result(device_id: str, power_w: float, offset: int = 0) -> InferenceResult:
    return InferenceResult(
        device_id=device_id,
        timestamp=BASE + timedelta(seconds=offset),
        power_w=power_w,
        source=Source.DIRECT_MEASURED,
        confidence=0.8,
    )


def _scan_uncached(db: Database) -> list[dict[str, Any]]:
    """The old behaviour: issue the full-table query directly, bypassing the cache."""
    with db._reader() as conn:  # noqa: SLF001 - test intentionally reaches into the cache seam
        return [dict(r) for r in conn.execute(RAW_QUERY).fetchall()]


# --------------------------------------------------------------------------- #
# Correctness
# --------------------------------------------------------------------------- #


def test_returns_newest_result_per_device(tmp_path: Path) -> None:
    db = Database(tmp_path / "correctness.db")
    try:
        # device A: 10 W then 30 W; device B: 5 W only.  Insert order, not
        # timestamp, decides "newest" because id is the autoincrement key.
        db.insert_inference_results(
            [
                _result("dev_a", 10.0, 0),
                _result("dev_b", 5.0, 1),
                _result("dev_a", 30.0, 2),
                _result("dev_a", 20.0, 3),  # older timestamp, newer id -> newest
            ]
        )
        rows = db.latest_inference_results()
        by_device = {r["device_id"]: r for r in rows}
        assert set(by_device) == {"dev_a", "dev_b"}
        assert by_device["dev_a"]["power_w"] == 20.0
        assert by_device["dev_b"]["power_w"] == 5.0
        # The cached answer equals a fresh, uncached scan of the same rows.
        assert by_device == {r["device_id"]: r for r in _scan_uncached(db)}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Cache invalidation
# --------------------------------------------------------------------------- #


def test_new_write_is_not_shadowed_by_stale_cache(tmp_path: Path) -> None:
    db = Database(tmp_path / "invalidation.db")
    try:
        db.insert_inference_results([_result("dev_a", 1.0, 0)])
        first = db.latest_inference_results()
        assert {r["device_id"]: r["power_w"] for r in first} == {"dev_a": 1.0}
        # Serve a second read from the cache so we know a snapshot is held.
        assert db.latest_inference_results() == first

        # A newer result for the same device must replace the cached one.
        db.insert_inference_results([_result("dev_a", 99.0, 1)])
        after = db.latest_inference_results()
        assert {r["device_id"]: r["power_w"] for r in after} == {"dev_a": 99.0}
    finally:
        db.close()


def test_a_new_device_appears_after_write(tmp_path: Path) -> None:
    db = Database(tmp_path / "new_device.db")
    try:
        db.insert_inference_results([_result("dev_a", 1.0, 0)])
        assert {r["device_id"] for r in db.latest_inference_results()} == {"dev_a"}

        db.insert_inference_results([_result("dev_b", 2.0, 1)])
        assert {r["device_id"] for r in db.latest_inference_results()} == {
            "dev_a",
            "dev_b",
        }
    finally:
        db.close()


def test_prune_and_reset_invalidate_the_cache(tmp_path: Path) -> None:
    db = Database(tmp_path / "lifecycle.db")
    try:
        # Old row is outside the 1-day raw horizon; new row is current.
        old = _result("dev_old", 1.0, 0)
        old.timestamp = BASE.replace(year=2020)
        current = _result("dev_new", 2.0, 1)
        current.timestamp = datetime.now(UTC)
        db.insert_inference_results([old, current])
        before = {r["device_id"] for r in db.latest_inference_results()}
        assert before == {"dev_old", "dev_new"}

        # Prune removes the only row for dev_old; its cached entry must vanish.
        db.prune(raw_days=1)
        after_prune = {r["device_id"] for r in db.latest_inference_results()}
        assert after_prune == {"dev_new"}

        # Reset wipes the database: nothing may be served from the old cache.
        db.reset()
        assert db.latest_inference_results() == []
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Structural proof: one scan per generation, not one per call
# --------------------------------------------------------------------------- #


def test_many_reads_scan_only_once_until_a_write(tmp_path: Path) -> None:
    db = Database(tmp_path / "miss_count.db")
    try:
        db.insert_inference_results([_result("dev_a", 1.0, 0)])

        original_reader = db._reader  # noqa: SLF001
        opens = 0

        def counting_reader(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            nonlocal opens
            opens += 1
            return original_reader(*args, **kwargs)

        db._reader = counting_reader  # type: ignore[method-assign]  # noqa: SLF001
        for _ in range(25):
            assert {r["device_id"] for r in db.latest_inference_results()} == {"dev_a"}
        assert opens == 1, "repeated reads must be served from the cache"

        # One write -> exactly one more scan on the next read.
        db.insert_inference_results([_result("dev_a", 2.0, 1)])
        db.latest_inference_results()
        assert opens == 2, "a committed write must invalidate the cache"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Measured before/after timing on a synthetic >=20k-row table
# --------------------------------------------------------------------------- #


def test_cached_read_is_faster_than_full_scan(tmp_path: Path) -> None:
    """Measure the real query (before) against the cached read (after).

    The full scan touches all 30k synthetic rows; the cached read is just a lock
    acquisition and a list return.  A 5x margin is generous enough to survive CI
    jitter while still failing if the cache regressed to re-scanning per call.
    """
    db = Database(tmp_path / "timing.db")
    try:
        rows_per_device = 1000
        devices = 30
        payload = [
            _result(f"dev_{i % devices:03d}", float(i), i)
            for i in range(rows_per_device * devices)
        ]
        db.insert_inference_results(payload)
        assert db._conn.execute("SELECT COUNT(*) FROM inference_results").fetchone()[0] >= 20_000  # noqa: SLF001

        # "Before": time the raw full-table query directly, best of 5.
        scan_best = min(_time_it(lambda: _scan_uncached(db), runs=5))

        # "After": prime the cache, then time the public cached read, best of 5.
        db.latest_inference_results()
        cached_best = min(_time_it(db.latest_inference_results, runs=5))

        # Correctness is not sacrificed for speed: the cached answer is the same
        # newest-per-device map the raw query returns.
        assert {r["device_id"]: r["power_w"] for r in db.latest_inference_results()} == {
            r["device_id"]: r["power_w"] for r in _scan_uncached(db)
        }
        assert cached_best * 5 < scan_best, (
            f"cached read {cached_best * 1e3:.3f} ms was not >=5x faster than "
            f"full scan {scan_best * 1e3:.3f} ms"
        )
    finally:
        db.close()


def _time_it(fn: Any, *, runs: int) -> list[float]:  # noqa: ANN401
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples
