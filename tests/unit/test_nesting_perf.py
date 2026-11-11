"""Performance work on the nesting path: vectorised kernels, filters, threading.

These tests exist to make each optimisation *falsifiable*.  A speed-up that
silently changes an answer is worse than the slowness it removes, so every test
here compares the fast path against a deliberately naive reference
implementation of the same predicate -- the loop that shipped before.

The reference implementations are local copies, not imports, so they keep
working as a specification after the production code changes.
"""

from __future__ import annotations

import asyncio
import math
import random
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.electrical.nesting import (
    ACTIVITY_MIN_W,
    DuplicateEvidence,
    containment,
    duplicate_evidence,
    infer_nesting,
)
from wattmosaic_engine.storage.database import Database

MAIN = {"ct_type": "main"}


# --------------------------------------------------------------------------- #
# Naive reference implementations (the shipped loops, kept as the spec)
# --------------------------------------------------------------------------- #
def _ref_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _ref_median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _ref_containment(
    outer: list[Any], inner: list[Any], *, tol_w: float, min_outer_w: float
) -> tuple[float, int]:
    ok = 0
    comparable = 0
    for o, i in zip(outer, inner, strict=True):
        if not _ref_finite(o) or not _ref_finite(i):
            continue
        o, i = float(o), float(i)
        if o < min_outer_w:
            continue
        comparable += 1
        if i <= o + tol_w:
            ok += 1
    if comparable == 0:
        return 0.0, 0
    return ok / comparable, comparable


def _ref_duplicate(
    a: list[Any], b: list[Any], *, activity_min_w: float
) -> DuplicateEvidence | None:
    joint: list[tuple[float, float]] = []
    either = 0
    for x, y in zip(a, b, strict=True):
        if not _ref_finite(x) or not _ref_finite(y):
            continue
        x, y = float(x), float(y)
        active_x = abs(x) >= activity_min_w
        active_y = abs(y) >= activity_min_w
        if active_x or active_y:
            either += 1
        if active_x and active_y:
            joint.append((x, y))
    if not joint or either == 0:
        return None
    diffs = [abs(x - y) / max(abs(x), abs(y), 1.0) for x, y in joint]
    med_a = _ref_median([x for x, _ in joint])
    med_b = _ref_median([y for _, y in joint])
    return DuplicateEvidence(
        joint_instants=len(joint),
        activity_ratio=len(joint) / either,
        rel_diff=_ref_median(diffs),
        ratio=max(abs(med_a), abs(med_b)) / max(min(abs(med_a), abs(med_b)), 1e-9),
        median_a_w=med_a,
        median_b_w=med_b,
    )


def _ref_closure(
    parent: list[Any], accounted: list[Any], *, tol_w: float, min_parent_w: float
) -> tuple[float, int, float]:
    ok = 0
    comparable = 0
    remainders: list[float] = []
    for p, a in zip(parent, accounted, strict=True):
        if not _ref_finite(p) or not _ref_finite(a):
            continue
        p, a = float(p), float(a)
        if p < min_parent_w:
            continue
        comparable += 1
        remainder = p - a
        remainders.append(remainder)
        if remainder >= -tol_w:
            ok += 1
    if comparable == 0:
        return 0.0, 0, 0.0
    remainders.sort()
    return ok / comparable, comparable, remainders[len(remainders) // 2]


def _random_series(rng: random.Random, n: int, scale: float) -> list[float]:
    """A series with everything the kernels have to agree about.

    Finite readings of both signs, zeros, ``nan``, ``+inf``, ``-inf``, integer
    values (to check the ``int`` branch) and the exact ``activity_min_w``
    boundary, which is where ``>=`` versus ``>`` would silently differ.
    """
    values: list[float] = []
    for _ in range(n):
        roll = rng.random()
        if roll < 0.08:
            values.append(math.nan)
        elif roll < 0.12:
            values.append(math.inf)
        elif roll < 0.16:
            values.append(-math.inf)
        elif roll < 0.20:
            values.append(float(rng.randint(-3, 3)))
        elif roll < 0.24:
            values.append(200.0)  # exactly at the default activity floor
        elif roll < 0.32:
            values.append(0.0)
        else:
            values.append(rng.uniform(-scale, scale))
    return values


# --------------------------------------------------------------------------- #
# (a) the vectorised kernels match the reference loop
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", range(12))
def test_vectorised_duplicate_evidence_matches_the_reference_loop(seed: int) -> None:
    rng = random.Random(seed)
    n = rng.randint(1, 400)
    a = _random_series(rng, n, 3000.0)
    b = _random_series(rng, n, 3000.0)
    fast = duplicate_evidence(a, b)
    slow = _ref_duplicate(a, b, activity_min_w=ACTIVITY_MIN_W)
    assert (fast is None) == (slow is None)
    if fast is None:
        assert slow is None
        return
    assert fast.joint_instants == slow.joint_instants, "integer counts must be exact"
    assert fast.activity_ratio == slow.activity_ratio
    assert fast.median_a_w == slow.median_a_w
    assert fast.median_b_w == slow.median_b_w
    assert fast.rel_diff == pytest.approx(slow.rel_diff, rel=0, abs=1e-12)
    assert fast.ratio == pytest.approx(slow.ratio, rel=0, abs=1e-9)


@pytest.mark.parametrize("seed", range(12))
def test_vectorised_containment_matches_the_reference_loop(seed: int) -> None:
    rng = random.Random(1000 + seed)
    n = rng.randint(1, 400)
    outer = _random_series(rng, n, 4000.0)
    inner = _random_series(rng, n, 5000.0)
    assert containment(outer, inner) == _ref_containment(
        outer, inner, tol_w=30.0, min_outer_w=200.0
    )


@pytest.mark.parametrize("seed", range(8))
def test_vectorised_closure_matches_the_reference_loop(seed: int) -> None:
    from wattmosaic_engine.electrical.nesting import closure_fraction

    rng = random.Random(2000 + seed)
    n = rng.randint(1, 300)
    parent = _random_series(rng, n, 4000.0)
    accounted = _random_series(rng, n, 4000.0)
    # ``None`` models the absent own-load reading closure_fraction must skip.
    accounted = [None if rng.random() < 0.15 else v for v in accounted]
    assert closure_fraction(parent, accounted) == _ref_closure(
        parent, accounted, tol_w=100.0, min_parent_w=200.0
    )


def test_misaligned_series_are_still_rejected_by_every_kernel() -> None:
    """``zip(strict=True)`` used to raise the length error implicitly.

    The vectorised version must keep raising ``ValueError`` explicitly: a silent
    truncation would compare instants that do not belong together.
    """
    from wattmosaic_engine.electrical.nesting import closure_fraction

    with pytest.raises(ValueError):
        duplicate_evidence([1.0, 2.0], [1.0])
    with pytest.raises(ValueError):
        containment([1.0, 2.0], [1.0])
    with pytest.raises(ValueError):
        closure_fraction([1.0, 2.0], [1.0])


def test_non_numeric_values_are_dropped_exactly_like_the_loop() -> None:
    """``_finite`` accepted only ``int``/``float``; the fast path must not widen.

    A string that parses as a number, or ``None``, used to be skipped.  Treating
    either as a reading would invent data, so this is checked directly rather
    than assumed from the dtype branch.
    """
    a = [2400.0, "2400", None, 2400.0, 2400.0]
    b = [2400.0, 2400.0, 2400.0, None, "2400"]
    fast = duplicate_evidence(a, b)
    slow = _ref_duplicate(a, b, activity_min_w=ACTIVITY_MIN_W)
    assert (fast is None) == (slow is None)
    assert fast is not None and slow is not None
    assert fast.joint_instants == slow.joint_instants == 1


# --------------------------------------------------------------------------- #
# (b) a pair where neither node is active yields no evidence
# --------------------------------------------------------------------------- #
def test_a_pair_where_neither_node_is_active_yields_no_duplicate() -> None:
    """Both nodes idle -> ``duplicate_evidence`` returns ``None`` -> no candidate.

    This is the case the hoisted peak filter skips *without scanning*.  The test
    is written so reverting the filter to a scan still must not report a
    duplicate: it pins the observable outcome, and the companion below pins the
    fact that the scan is skipped.
    """
    n = 200
    rng = random.Random(7)
    # Peaks of 5 W and 6 W: far below the 200 W activity floor.
    idle_a = [rng.uniform(0.0, 5.0) for _ in range(n)]
    idle_b = [rng.uniform(0.0, 6.0) for _ in range(n)]
    assert max(idle_a) < ACTIVITY_MIN_W
    assert max(idle_b) < ACTIVITY_MIN_W
    assert duplicate_evidence(idle_a, idle_b) is None

    candidates = infer_nesting(
        {"idle_a": MAIN, "idle_b": MAIN},
        {"idle_a": idle_a, "idle_b": idle_b},
        container_ct_ids=["idle_a", "idle_b"],
    )
    assert [c for c in candidates if c.kind == "duplicate"] == []


def test_the_inactive_pair_is_skipped_without_comparing_series(monkeypatch) -> None:
    """The hoisted peak filter must actually skip the scan, not just its result.

    Negative verification: replace ``duplicate_evidence`` with a counter.  With
    the filter in place an all-idle pair never calls it; remove the filter and
    this count becomes 1.
    """
    import wattmosaic_engine.electrical.nesting as nesting

    calls: list[tuple[int, int]] = []
    real = nesting.duplicate_evidence

    def counting(a, b, **kwargs):
        calls.append((len(a), len(b)))
        return real(a, b, **kwargs)

    monkeypatch.setattr(nesting, "duplicate_evidence", counting)
    rng = random.Random(11)
    idle_a = [rng.uniform(0.0, 5.0) for _ in range(50)]
    idle_b = [rng.uniform(0.0, 6.0) for _ in range(50)]
    nesting.infer_nesting(
        {"idle_a": MAIN, "idle_b": MAIN},
        {"idle_a": idle_a, "idle_b": idle_b},
        container_ct_ids=["idle_a", "idle_b"],
    )
    assert calls == [], (
        "an all-idle pair was scanned: the per-node activity peak filter is not "
        "doing its job"
    )


def test_the_container_filter_runs_before_the_scan(monkeypatch) -> None:
    """Pairs with no container member are discarded before scanning them.

    Negative verification for moving the filter above the ``duplicate_evidence``
    call: two non-container loads never reach it.
    """
    import wattmosaic_engine.electrical.nesting as nesting

    scanned: list[tuple[int, int]] = []
    real = nesting.duplicate_evidence

    def counting(a, b, **kwargs):
        scanned.append((len(a), len(b)))
        return real(a, b, **kwargs)

    monkeypatch.setattr(nesting, "duplicate_evidence", counting)
    rng = random.Random(13)
    load_a = [rng.uniform(1000.0, 1200.0) for _ in range(60)]
    load_b = [rng.uniform(1000.0, 1200.0) for _ in range(60)]
    nesting.infer_nesting(
        {
            "load_a": {"ct_type": "dedicated_device"},
            "load_b": {"ct_type": "dedicated_device"},
        },
        {"load_a": load_a, "load_b": load_b},
        # No containers declared, so neither node can be one and the pair is
        # exactly the "two smart plugs on one appliance" case.
        container_ct_ids=[],
    )
    assert scanned == [], (
        "a pair with no container member was scanned: the container-membership "
        "filter still runs after duplicate_evidence"
    )


# --------------------------------------------------------------------------- #
# (c) the resampled grid keeps candidate identity
# --------------------------------------------------------------------------- #
def _near_duplicate_pair(n: int, *, cadence_s: float) -> tuple[list, list, list]:
    """Two channels on one wire plus a third, sampled on a jittered grid.

    The timestamps are offset by sub-second amounts per channel, which is what
    makes the union of timestamps far larger than either channel's sample count.
    """
    start = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)
    samples_a: list[tuple[datetime, float]] = []
    samples_b: list[tuple[datetime, float]] = []
    samples_c: list[tuple[datetime, float]] = []
    for i in range(n):
        base = start + timedelta(seconds=i * cadence_s)
        watts = 2400.0 + 40.0 * math.sin(i / 7.0)
        samples_a.append((base + timedelta(milliseconds=i % 3), watts))
        samples_b.append((base + timedelta(milliseconds=500 + i % 4), watts * 1.004))
        samples_c.append((base + timedelta(milliseconds=900 + i % 5), watts * 0.2))
    return samples_a, samples_b, samples_c


def test_the_resampled_grid_keeps_the_candidate_pair_set() -> None:
    """Downsampling must not invent or lose a candidate pair.

    The pair set is the part of the answer that matters: it names *which* wire
    feeds which.  Evidence counts (``instants``, ``joint_instants``) describe the
    comparison and legitimately shrink, which is why the production default keeps
    the union grid -- see ``NESTING_RESAMPLE_GRID``.
    """
    from wattmosaic_engine.main import _sample_grid, _sampling_cadence_s

    cadence = 15.7
    samples = _near_duplicate_pair(240, cadence_s=cadence)
    series = {"wire_a": samples[0], "wire_b": samples[1], "load_c": samples[2]}
    union = sorted({stamp for channel in series.values() for stamp, _ in channel})
    modelled = {
        "wire_a": {"ct_type": "main"},
        "wire_b": {"ct_type": "main"},
        "load_c": {"ct_type": "dedicated_device"},
    }

    def pairs(grid: list[datetime]) -> list[tuple[str, str, str]]:
        from wattmosaic_engine.main import _forward_fill

        aligned = {
            cid: _forward_fill(list(channel), grid) for cid, channel in series.items()
        }
        found = infer_nesting(
            modelled,
            aligned,
            container_ct_ids=["wire_a", "wire_b"],
        )
        return [(c.kind, c.child_ct_id, c.parent_ct_id) for c in found]

    resampled = _sample_grid(
        {cid: list(channel) for cid, channel in series.items()},
        cadence_s=_sampling_cadence_s(
            [
                type("Channel", (), {"enabled": True, "expected_interval_s": cadence})()
            ]
        ),
        window_hours=1.0,
    )
    assert resampled, "the guarded grid must not collapse to nothing"
    assert len(resampled) < len(union), "the fixture must actually exercise downsampling"
    assert pairs(resampled) == pairs(union)
    # The duplicate must still be found, not merely "no difference because empty".
    assert ("duplicate", "wire_b", "wire_a") in pairs(resampled)


def test_the_grid_never_merges_readings_faster_than_the_declared_cadence() -> None:
    """Guard rail: a cadence coarser than the real sampling is clamped.

    A channel sampling every 2 s must not be resampled at 15.7 s, which would
    throw away seven of every eight real readings.
    """
    from wattmosaic_engine.main import _sample_grid

    start = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)
    fast = [(start + timedelta(seconds=2 * i), 100.0) for i in range(120)]
    grid = _sample_grid({"fast": fast}, cadence_s=15.7, window_hours=0.1)
    assert len(grid) >= len(fast) * 0.9, (
        f"resampled a 2 s series to {len(grid)} instants: the median-gap guard is gone"
    )


# --------------------------------------------------------------------------- #
# (d) the event loop is not blocked
# --------------------------------------------------------------------------- #
async def test_nesting_runs_off_the_event_loop() -> None:
    """The handler must not compute on the loop thread.

    A slow facade call is used as the probe: while it is running, a concurrent
    ticker has to keep firing.  If ``topology_nesting`` awaited the call inline,
    every tick would be delayed by the whole call duration.
    """
    from wattmosaic_engine.api.facade import StubFacade
    from wattmosaic_engine.api.server import APIServer

    class SlowNestingFacade(StubFacade):
        def nesting_candidates(self, *, hours: float = 12.0) -> dict[str, Any]:
            # Blocking sleep: exactly what a CPU-bound call does to the loop.
            import time as _time

            with _thread_marker() as thread_ids:
                _time.sleep(0.35)
            return {"candidates": [], "instants": 0, "thread": thread_ids}

    # Record which thread performed the work.
    import time as _time

    seen: dict[str, int] = {}

    def _thread_marker():
        import contextlib

        @contextlib.contextmanager
        def _cm():
            seen["thread"] = threading.get_ident()
            yield seen["thread"]

        return _cm()

    server = APIServer(SlowNestingFacade(), host="127.0.0.1", port=0)
    await server.start()
    try:
        import aiohttp

        loop_thread = threading.get_ident()
        ticks = 0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        async with aiohttp.ClientSession() as session:
            base = f"http://127.0.0.1:{server.port}/api/v1"
            task = asyncio.create_task(ticker())
            started = _time.perf_counter()
            async with session.get(f"{base}/topology/nesting?hours=12") as response:
                assert response.status == 200
                await response.json()
            elapsed = _time.perf_counter() - started
            stop.set()
            await task
    finally:
        await server.stop()

    assert elapsed >= 0.3, "the stub must actually be slow, or this proves nothing"
    # A blocked loop fires roughly two ticks in 350 ms; a free one fires dozens.
    assert ticks >= 8, (
        f"only {ticks} loop ticks during a 350 ms nesting request: the call is "
        "blocking the event loop"
    )
    assert seen["thread"] != loop_thread, (
        "nesting_candidates ran on the event-loop thread; wrap it in asyncio.to_thread"
    )


def test_the_database_connection_allows_a_worker_thread(tmp_path) -> None:
    """``check_same_thread=False`` plus the lock is what makes ``to_thread`` legal.

    Without it SQLite raises ``ProgrammingError`` when a worker thread touches the
    connection, so the endpoint would fail rather than merely block.
    """
    db = Database(tmp_path / "threaded.db")
    try:
        _topology_with_one_channel(db)
        errors: list[BaseException] = []

        def read_in_thread() -> None:
            try:
                db.ct_samples_between(
                    datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
                )
                db.latest_inference_results()
                db.latest_ct_samples()
            except BaseException as exc:  # noqa: BLE001 - the point is to surface it
                errors.append(exc)

        thread = threading.Thread(target=read_in_thread)
        thread.start()
        thread.join()
        assert errors == [], f"cross-thread access failed: {errors[0]!r}"
    finally:
        db.close()


def _topology_with_one_channel(db: Database) -> None:
    from wattmosaic_engine.contracts import CTChannel, CTType, Topology

    channel = CTChannel(
        ct_id="main_a", name="Main A", ct_type=CTType.MAIN, entity_id="sensor.main_a"
    )
    db.save_topology(Topology(channels=[channel], devices=[]), note="test")


# --------------------------------------------------------------------------- #
# (e) the narrow projection returns the same rows
# --------------------------------------------------------------------------- #
def test_the_narrow_projection_returns_the_same_rows(tmp_path) -> None:
    """``SELECT <4 columns>`` must yield exactly the rows ``SELECT *`` did.

    Same count, same values, same order, same window filter, same ct_id filter.
    The dropped columns (``id``, ``entity_id``, ``unit``, ...) must not be read by
    any caller; the caller reads ``ct_id``, ``value_w``, ``state_timestamp`` and
    ``received_at``.
    """
    db = Database(tmp_path / "projection.db")
    try:
        _topology_with_one_channel(db)
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        rows = []
        for index in range(50):
            stamp = start + timedelta(seconds=15 * index)
            rows.append(
                {
                    "ct_id": "main_a" if index % 2 else "main_b",
                    "entity_id": f"sensor.{index}",
                    "value_w": float(index * 10),
                    "raw_value": float(index),
                    "unit": "W",
                    "state_timestamp": stamp,
                    "received_at": stamp,
                }
            )
        db.insert_ct_samples(rows)

        window_start = start - timedelta(seconds=1)
        window_end = start + timedelta(seconds=15 * 49)
        # The shipped query, reconstructed here, so the comparison is against the
        # behaviour rather than against our own new code.
        with db._lock:
            legacy = db._conn.execute(
                "SELECT * FROM ct_samples WHERE received_at >= ? AND received_at <= ? "
                "AND ct_id IN (?) ORDER BY received_at ASC",
                ("2025-12-31T23:59:59+00:00", "2026-01-01T00:12:15+00:00", "main_a"),
            ).fetchall()
        legacy_rows = [dict(row) for row in legacy]

        narrowed = db.ct_samples_between(
            window_start, window_end, ct_ids=["main_a"]
        )

        assert len(narrowed) == len(legacy_rows) > 0
        for narrow_row, legacy_row in zip(narrowed, legacy_rows, strict=True):
            assert set(narrow_row) == {
                "ct_id",
                "value_w",
                "state_timestamp",
                "received_at",
            }, "the projection must not grow back to SELECT *"
            for column in narrow_row:
                assert narrow_row[column] == legacy_row[column]
        # Filtering by the other channel returns the complement, not everything.
        other = db.ct_samples_between(window_start, window_end, ct_ids=["main_b"])
        assert len(other) == len(legacy_rows)
        assert {row["ct_id"] for row in other} == {"main_b"}
    finally:
        db.close()


def test_the_projection_is_not_order_dependent_for_its_caller() -> None:
    """The dropped ``ORDER BY`` must not be needed by the nesting caller.

    ``nesting_candidates`` sorts each channel's series itself, so the SQL order is
    irrelevant.  This asserts that, so a future caller cannot quietly rely on an
    order the query no longer promises.
    """
    import inspect

    from wattmosaic_engine import main as main_module

    forward_source = inspect.getsource(main_module._forward_fill)
    assert "sorted(series)" in forward_source, (
        "the caller must sort the series itself now that ct_samples_between does not"
    )
