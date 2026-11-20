"""P5: balanced 240 V legs must not get a specific crossing made STRUCTURAL.

When two legs move both mains identically, steps alone cannot say which leg
sits under which main: both crossings stay PROVISIONAL (soft) and the decision
records ``leg_unidentified``.  An asymmetric, one-main-only step identifies the
crossing and allows STRUCTURAL as before.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

from wattmosaic_engine.electrical import topology_resolve as tr

from tests.unit.test_topology_resolve import PROVISIONAL, _edge, _life, _noise, _steps

T0 = datetime(2026, 11, 20, tzinfo=UTC)


def _balanced(n: int = 1200, seed: int = 7) -> dict[str, list[float | None]]:
    """Both legs step together, so both mains see the identical step.

    Both mains can carry either leg alone *and both legs together*, so capacity
    provides no asymmetric evidence.  The legs are still detected as a pair
    (both draw power together), but neither crossing is identifiable.
    """
    rng = random.Random(seed)
    tub = _steps(rng, n, [0.0, 0.0, 0.0, 1500.0], 0.05)
    base_a = _steps(rng, n, [1500.0, 2500.0, 4000.0], 0.08)
    base_b = _steps(rng, n, [2000.0, 3200.0, 5000.0], 0.08)
    return {
        "leg_1": _noise(rng, tub, 3.0),
        "leg_2": _noise(rng, tub, 3.0),
        "main_a": _noise(rng, [b + t for b, t in zip(base_a, tub, strict=True)], 5.0),
        "main_b": _noise(rng, [b + t for b, t in zip(base_b, tub, strict=True)], 5.0),
    }


def _asymmetric(n: int = 1200, seed: int = 22) -> dict[str, list[float | None]]:
    """One leg also has a step that moves only main_a (asymmetric evidence)."""
    rng = random.Random(seed)
    shared = _steps(rng, n, [0.0, 0.0, 300.0], 0.05)
    leg_1_only = _steps(rng, n, [0.0, 0.0, 2000.0], 0.08)
    # main_a has its OWN independent steps (not in leg_1), so the reverse fit
    # leg_1->main_a is clearly worse: main_a - leg_1 still varies a lot.
    base_a = _steps(rng, n, [700.0, 2200.0, 3600.0], 0.1)
    base_b = _steps(rng, n, [300.0, 700.0, 1100.0], 0.08)
    return {
        "leg_1": _noise(rng, [a + b for a, b in zip(shared, leg_1_only, strict=True)], 3.0),
        "leg_2": _noise(rng, shared, 3.0),
        "main_a": _noise(
            rng, [a + b + c for a, b, c in zip(shared, leg_1_only, base_a, strict=True)], 4.0
        ),
        "main_b": _noise(rng, [b + c for b, c in zip(shared, base_b, strict=True)], 4.0),
    }


def test_balanced_legs_keep_both_crossings_provisional() -> None:
    series = _balanced()
    edges = [
        _edge("main_a", "leg_1", PROVISIONAL, support=8),
        _edge("main_b", "leg_1", PROVISIONAL, support=8),
        _edge("main_a", "leg_2", PROVISIONAL, support=8),
        _edge("main_b", "leg_2", PROVISIONAL, support=8),
    ]
    result = tr.resolve(edges, series)
    for main in ("main_a", "main_b"):
        for leg in ("leg_1", "leg_2"):
            assert _life(result.edges, main, leg) == "provisional", (main, leg)
    assert any("leg_unidentified" in text for text in result.transitions)
    assert result.leg_unidentified


def test_asymmetric_one_main_step_allows_structural() -> None:
    series = _asymmetric()
    edges = [
        _edge("main_a", "leg_1", PROVISIONAL, support=8),
        _edge("main_b", "leg_1", PROVISIONAL, support=8),
    ]
    result = tr.resolve(edges, series)
    # The leg that moved only main_a can be identified: its edge can confirm.
    assert _life(result.edges, "main_a", "leg_1") == "structural"
