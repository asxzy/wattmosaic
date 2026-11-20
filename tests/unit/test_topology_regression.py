"""M4: differenced-series regression, duplicate meters and hold resampling."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from wattmosaic_engine.electrical.topology_regression import (
    DUP_MAX_OFFSET_W,
    DUP_MIN_ACTIVE_SAMPLES,
    GRID_STEP_S,
    REG_MIN_CHILD_DIFF_W,
    REG_MIN_SAMPLES,
    align,
    diff_regression,
    duplicate_meters,
    rank_parents,
    resample_hold,
)

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


def _other_loads(rng: random.Random, n: int, sigma: float = 25.0, k: int = 3) -> list[float]:
    """Base noise plus random-walk steps from ``k`` other loads."""
    series = [rng.gauss(0.0, sigma) for _ in range(n)]
    for _ in range(k):
        walk = 0.0
        for i in range(n):
            walk += rng.gauss(0.0, sigma)
            series[i] += walk
    return series


def _child(rng: random.Random, n: int) -> list[float]:
    """Sum of independent on/off loads; each step toggles one 100-400 W load."""
    loads = [100.0, 150.0, 200.0, 300.0, 400.0]
    states = [0] * len(loads)
    series: list[float] = []
    for _ in range(n):
        states[rng.randrange(len(loads))] ^= 1
        series.append(sum(load for load, on in zip(loads, states, strict=True) if on))
    return series


def test_resample_hold_default_holds_forever() -> None:
    times = [T0 + timedelta(seconds=20)]
    values = [5.0]
    out = resample_hold(times, values, start=T0, end=T0 + timedelta(seconds=41))
    assert out == [None, None, 5.0, 5.0, 5.0]
    assert len(out) == math.ceil(41 / GRID_STEP_S)


def test_resample_hold_max_hold_and_multiple_samples() -> None:
    times = [T0, T0 + timedelta(seconds=20)]
    values = [10.0, 20.0]
    held = resample_hold(times, values, start=T0, end=T0 + timedelta(seconds=41))
    assert held == [10.0, 10.0, 20.0, 20.0, 20.0]
    limited = resample_hold(
        times, values, start=T0, end=T0 + timedelta(seconds=41), max_hold_s=15.0
    )
    assert limited == [10.0, 10.0, 20.0, 20.0, None]


def test_resample_hold_last_sample_none_and_length() -> None:
    times = [T0, T0 + timedelta(seconds=20)]
    values = [10.0, None]
    out = resample_hold(times, values, start=T0, end=T0 + timedelta(seconds=41))
    assert out == [10.0, 10.0, None, None, None]
    assert len(out) == 5


def test_align_sorts_rows_and_equal_length() -> None:
    n = 100
    a_rows = [(T0 + timedelta(seconds=i * 10), float(i)) for i in range(n)]
    b_rows = list(reversed(a_rows))
    out = align(
        {"a": a_rows, "b": b_rows},
        start=T0,
        end=T0 + timedelta(seconds=n * 10),
    )
    assert set(out) == {"a", "b"}
    assert len(out["a"]) == len(out["b"]) == n
    assert out["a"] == out["b"]


def test_diff_regression_true_parent_and_unrelated() -> None:
    n = 800
    rng = random.Random(42)
    child = _child(rng, n)
    # Build the parent as child + independent noise from three other loads.
    other = _other_loads(random.Random(7), n)
    parent = [child[i] + other[i] for i in range(n)]

    fit = diff_regression(parent, child)
    assert fit is not None
    assert 0.85 <= fit.slope <= 1.15
    assert fit.r > 0.5
    assert fit.n >= REG_MIN_SAMPLES

    unrelated = _other_loads(random.Random(99), n)
    bad = diff_regression(unrelated, child)
    assert bad is not None
    assert bad.r < 0.2


def test_diff_regression_too_few_samples_and_constant_child() -> None:
    rng = random.Random(5)
    child = _child(rng, 10)
    parent = [c + rng.gauss(0, 5) for c in child]
    assert diff_regression(parent, child) is None  # fewer than REG_MIN_SAMPLES

    n = 100
    child_const = [100.0] * n
    parent2 = [rng.gauss(0, 10) for _ in range(n)]
    assert diff_regression(parent2, child_const) is None


def test_diff_regression_respects_thresholds() -> None:
    # A child whose steps are all below the threshold yields no fit even with
    # many samples.
    rng = random.Random(11)
    child = [rng.uniform(0, 20) for _ in range(200)]
    parent = [c + rng.gauss(0, 1) for c in child]
    assert diff_regression(parent, child, min_child_diff_w=REG_MIN_CHILD_DIFF_W) is None


def test_rank_parents_true_parent_first() -> None:
    n = 800
    rng = random.Random(21)
    child = _child(rng, n)
    other = _other_loads(random.Random(22), n)
    true_parent = [child[i] + other[i] for i in range(n)]
    decoy1 = _other_loads(random.Random(23), n)
    decoy2 = _other_loads(random.Random(24), n)

    series = {"child": child, "p1": decoy1, "p2": true_parent, "p3": decoy2}
    ranked = rank_parents("child", ["p1", "p2", "p3", "child", "missing"], series)
    assert ranked
    assert ranked[0][0] == "p2"
    assert all(pid != "child" for pid, _ in ranked)
    assert all(pid != "missing" for pid, _ in ranked)


def test_duplicate_meters_offset_and_rejections() -> None:
    n = 200
    rng = random.Random(31)
    a = _other_loads(rng, n, sigma=60.0, k=2)
    b = [x + 60.0 + rng.gauss(0, 2.0) for x in a]
    series = {"a": a, "b": b}

    found = duplicate_meters(series)
    assert len(found) == 1
    a_name, b_name, r, offset = found[0]
    assert (a_name, b_name) == ("a", "b")
    assert r >= 0.98
    assert abs(offset - 60.0) < 5.0
    assert abs(offset) <= DUP_MAX_OFFSET_W
    assert len(a) >= DUP_MIN_ACTIVE_SAMPLES

    # Slope 0.5 fails the [0.9, 1.1] slope gate even though r is high.
    assert duplicate_meters({"a": a, "c": [0.5 * x for x in a]}) == []

    # Unrelated series: low correlation.
    unrelated = _other_loads(random.Random(77), n, sigma=60.0, k=2)
    assert duplicate_meters({"a": a, "d": unrelated}) == []

    # Parent vs child with extra independent loads: not a duplicate.
    extra = _other_loads(random.Random(88), n, sigma=60.0, k=2)
    parent = [a[i] + extra[i] for i in range(n)]
    assert duplicate_meters({"child": a, "parent": parent}) == []
