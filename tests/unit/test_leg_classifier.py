"""E5 §4: synthetic voltage-sag leg classification and branch-current tag.

Neutral reader ids only: the leg must come from the declared voltage series,
never from a name or scenario knowledge.
"""

from __future__ import annotations

import numpy as np
from wattmosaic_engine.electrical.leg_classifier import (
    analyse_legs,
    cross_leg_veto,
    prefer_branch_tag,
    same_leg_parent,
)


def _references(n: int = 400) -> tuple[list[float], list[float]]:
    """Two references with anti-correlated residuals (one physical split)."""
    base = np.sin(np.arange(n) / 20.0) * 0.1
    return (list(120.0 + base), list(120.0 - base))


def _steps_power(n: int, bands: list[tuple[int, int]] | None = None) -> list[float]:
    bands = bands or [(20, 40), (60, 80), (100, 120), (140, 160), (180, 200), (220, 240)]
    power = [0.0] * n
    for a, b in bands:
        power[a:b] = [1000.0] * (b - a)
    return power


def test_references_cluster_into_two_legs() -> None:
    v_l, v_r = _references()
    report = analyse_legs(power={"load": _steps_power(400)}, voltage={"r1": v_l, "r2": v_r})
    assert set(report.references.values()) == {"L", "R"}
    assert report.common_mode_removed is True


def test_load_that_sags_one_reference_is_that_leg() -> None:
    v_l, v_r = _references()
    power = _steps_power(400)
    for a, b in [(20, 40), (60, 80), (100, 120), (140, 160), (180, 200), (220, 240)]:
        for i in range(a, b):
            v_l[i] -= 2.0
    report = analyse_legs(power={"load": power}, voltage={"r1": v_l, "r2": v_r})
    # r1 is the reference that sags; the load's leg matches it.
    assert report.leg["load"] == report.references["r1"]
    assert report.leg_n["load"] >= 5


def test_equal_sag_on_both_references_is_both() -> None:
    v_l, v_r = _references()
    power = _steps_power(400)
    for a, b in [(20, 40), (60, 80), (100, 120), (140, 160), (180, 200), (220, 240)]:
        for i in range(a, b):
            v_l[i] -= 2.0
            v_r[i] -= 2.0
    report = analyse_legs(power={"load": power}, voltage={"r1": v_l, "r2": v_r})
    assert report.leg["load"] == "both"


def test_classification_needs_five_steps() -> None:
    v_l, v_r = _references()
    power = _steps_power(400, bands=[(20, 40), (60, 80)])
    for a, b in [(20, 40), (60, 80)]:
        for i in range(a, b):
            v_l[i] -= 2.0
    report = analyse_legs(power={"load": power}, voltage={"r1": v_l, "r2": v_r})
    assert report.leg["load"] == "unknown"
    assert report.leg_n["load"] < 5


def test_cross_leg_veto_caps_an_edge() -> None:
    assert cross_leg_veto("L", "R", 12, 12) is True
    assert cross_leg_veto("L", "R", 3, 12) is False  # too few steps
    assert cross_leg_veto("both", "R", 30, 30) is False
    assert cross_leg_veto("L", "L", 30, 30) is False


def test_branch_tag_fires_above_12_a() -> None:
    n = 600
    current_high = [13.0] * 300 + [1.0] * 300
    current_low = [5.0] * n
    report = analyse_legs(
        power={"x": [0.0] * n},
        voltage={"r1": [120.0] * n, "r2": [120.0] * n},
        current={"high": current_high, "low": current_low},
    )
    assert report.branch_tag["high"] is True
    assert report.branch_tag["low"] is False


def test_own_voltage_group_never_decides_the_readers_leg() -> None:
    """E6 §5: a reader whose voltage entity sits in the L group but whose own
    steps sag R must be classified R.  The group label is only a reference
    anchor; the leg comes from the reader's own ΔP against the ΔV of the
    references (current-driven sag), never from its voltage entity's group.
    """
    v_l, v_r = _references()
    power = _steps_power(400)
    # The reader's own voltage entity is a copy of the L reference (it sits in
    # the L group), but its load sags R.
    reader_voltage = list(v_l)
    for a, b in [(20, 40), (60, 80), (100, 120), (140, 160), (180, 200), (220, 240)]:
        for i in range(a, b):
            v_r[i] -= 2.0  # the physical R reference sags
    report = analyse_legs(
        power={"load": power},
        voltage={"ref_l": v_l, "ref_r": v_r, "load_copy_of_l": reader_voltage},
    )
    assert report.references["load_copy_of_l"] == "L"  # its voltage group is L
    assert report.leg["load"] == "R"  # but its own ΔP sags R


def test_same_leg_parent_and_branch_tie_break() -> None:
    assert same_leg_parent("L", {"a": "L", "b": "R"}) == "a"
    assert same_leg_parent("L", {"a": "L", "b": "L"}) is None
    assert same_leg_parent("both", {"a": "L", "b": "R"}) is None
    assert prefer_branch_tag(["a", "b"], {"a": False, "b": True}) == ["b", "a"]
