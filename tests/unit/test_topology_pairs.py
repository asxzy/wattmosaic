"""E4 §1: pure pair analysis on synthetic aligned numpy series.

These tests use neutral reader ids: the quantities must come from the declared
series and structural children, never from names or scenario knowledge.
"""

from __future__ import annotations

import numpy as np
from wattmosaic_engine.electrical.topology_pairs import (
    CONTAIN_DECISIVE_INFORMATIVE,
    CONTAIN_MIN_INFORMATIVE,
    KCL_MIN_SAMPLES,
    MIRROR_MIN,
    STEP_WLIFT_MIN,
    analyse_pairs,
)


def _steps(n: int, times: list[int], step: float, base: float = 100.0) -> np.ndarray:
    values = np.full(n, base, dtype=np.float64)
    for i in range(n):
        values[i] = base + step * sum(1 for t in times if t < i)
    return values


def test_true_child_has_high_lift_and_busy_feed_has_near_zero() -> None:
    # A child steps 12 times; the true parent steps with it.  A busy feed also
    # steps 12 times, at unrelated times, so its chance-corrected lift ~ 0.
    n = 2400
    times = [200 + 180 * i for i in range(12)]
    child = _steps(n, times, 200.0)
    true_parent = _steps(n, times, 200.0)
    rng = np.random.default_rng(7)
    busy_times = sorted(rng.choice(np.arange(100, n - 100), size=12, replace=False).tolist())
    busy = _steps(n, busy_times, 200.0)
    analysis = analyse_pairs(
        {"p": true_parent, "c": child, "busy": busy},
        {},
        {},
    )
    true_stats = analysis.stats_for_or_compute("p", "c")
    busy_stats = analysis.stats_for_or_compute("busy", "c")
    assert true_stats is not None and busy_stats is not None
    assert true_stats.n >= 8
    assert true_stats.wlift >= STEP_WLIFT_MIN
    assert true_stats.match >= 0.5
    assert abs(busy_stats.wlift) < STEP_WLIFT_MIN


def test_mirroring_finds_a_child_hidden_under_siblings() -> None:
    # The child's steps are masked in the parent's own level by a sibling that
    # steps the other way at the same times.  Subtracting the structural sibling
    # reveals them, so the mirror lift is high.
    n = 2400
    times = [200 + 180 * i for i in range(12)]
    child = _steps(n, times, 200.0)
    sibling = _steps(n, times, 200.0)
    parent = _steps(n, times, 200.0) - _steps(n, times, 200.0) + 1000.0
    analysis = analyse_pairs(
        {"p": parent, "c": child, "sibling": sibling},
        {},
        {"p": frozenset({"sibling"})},
    )
    stats = analysis.stats_for_or_compute("p", "c")
    assert stats is not None
    assert stats.mirror is not None
    assert stats.mirror >= MIRROR_MIN


def test_containment_counts_one_violation_but_not_a_one_bucket_skew() -> None:
    n = 4000
    # The parent spends 10 % of its time at 640 W, so the 5th percentile is
    # 640; the child at 650 W is informative in every bucket yet still inside
    # 640 + 15 + 0.02·640 = 668 at the parent's low buckets.
    parent = np.full(n, 1000.0)
    parent[::10] = 640.0
    child = np.full(n, 650.0)
    # One bucket where the child exceeds the parent's local window + tolerance.
    child[1000] = 1300.0
    # A one-bucket lead at another time: the parent matches one bucket later,
    # inside the 3-bucket max window, so it is not a violation.
    child[2000] = 930.0
    parent[2001] = 1400.0
    analysis = analyse_pairs({"p": parent, "c": child}, {}, {})
    stats = analysis.stats_for_or_compute("p", "c")
    assert stats is not None
    assert stats.informative_n >= CONTAIN_DECISIVE_INFORMATIVE
    assert stats.violations == 1


def test_kcl_flags_a_double_counted_set() -> None:
    n = 2 * KCL_MIN_SAMPLES
    parent = np.full(n, 1000.0)
    child = np.full(n, 700.0)
    analysis = analyse_pairs({"p": parent, "c": child}, {}, {})
    # One 700 W child under a 1000 W parent fits (1000 >= 700 - slack).
    fitting, fitting_n = analysis.kcl("p", ["c"])
    assert fitting_n >= KCL_MIN_SAMPLES
    assert fitting >= 0.99
    # Two such children double count the parent: almost every minute violates.
    broken, broken_n = analysis.kcl("p", ["c", "c2"])
    # The absent child has no series, so KCL ignores it (absent is not zero).
    assert broken_n == fitting_n
    broken_series = dict(analysis.arrays)
    broken_series["c2"] = child.copy()
    analysis2 = analyse_pairs(broken_series, {}, {})
    fraction, samples = analysis2.kcl("p", ["c", "c2"])
    assert samples >= KCL_MIN_SAMPLES
    assert fraction < 0.99
    assert not analysis2.fits_kcl("p", ["c", "c2"])


def test_scale_one_over_f_honoured() -> None:
    # A ×2 child reports 400 while the parent steps 200.  At scale 1/2 the
    # child's delta is 200, so it matches with high lift; at scale 1 it does not.
    n = 2400
    times = [200 + 180 * i for i in range(12)]
    child = _steps(n, times, 400.0)
    parent = _steps(n, times, 200.0)
    scaled = analyse_pairs({"p": parent, "c": child}, {"c": 0.5}, {})
    unscaled = analyse_pairs({"p": parent, "c": child}, {}, {})
    s = scaled.stats_for_or_compute("p", "c")
    u = unscaled.stats_for_or_compute("p", "c")
    assert s is not None and u is not None
    assert s.wlift >= STEP_WLIFT_MIN
    assert u.wlift < s.wlift


def test_leaf_zero_floor_veto() -> None:
    # A candidate parent that stays at/below 5 W while the child draws at least
    # 30 W in several buckets is not carrying that child.  Only leaf parents
    # (no structural or provisional children) are vetoed.
    n = 4000
    parent = np.full(n, 2.0)
    child = np.full(n, 300.0)
    child[100 : 100 + CONTAIN_MIN_INFORMATIVE + 10] = 300.0
    analysis = analyse_pairs({"p": parent, "c": child}, {}, {})
    stats = analysis.stats_for("p", "c")
    assert stats is not None and stats.zero_floor

    # A parent that already has a child is not a leaf: the veto does not fire.
    with_child = analyse_pairs(
        {"p": parent, "c": child, "other": child.copy()}, {}, {"p": frozenset({"other"})}
    )
    other = with_child.stats_for("p", "c")
    assert other is not None and not other.zero_floor


def test_lift_gate_admits_busy_main_child_and_refuses_coincidence() -> None:
    """Regression: a raw match floor of 0.5 blocked true children of busy mains.

    On the engine's 30 s grid true pairs on a busy main match 0.40-0.48 of the
    time (10-day reference replay); the rarity-weighted lift is what separates
    them from coincidental feeds, so the raw floor is 0.3.
    """
    from wattmosaic_engine.electrical.topology_pairs import (
        STEP_LIFT_MIN_STEPS,
        PairStats,
        lift_gate_passes,
    )

    busy_main_child = PairStats(wlift=0.29, match=0.40, n=1050)
    coincidence = PairStats(wlift=0.04, match=0.62, n=900)
    too_few_steps = PairStats(wlift=0.0, match=0.0, n=STEP_LIFT_MIN_STEPS - 1)
    assert lift_gate_passes(busy_main_child) is True
    assert lift_gate_passes(coincidence) is False
    assert lift_gate_passes(PairStats(wlift=0.5, match=0.2, n=50)) is False
    assert lift_gate_passes(too_few_steps) is True
    assert lift_gate_passes(None) is True
