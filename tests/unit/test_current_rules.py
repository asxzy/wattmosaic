"""E6 §3: raw-current vetoes, set-KCL and rating classes (pure, aligned)."""

from __future__ import annotations

from wattmosaic_engine.electrical.current_rules import (
    analyse_current,
    child_above_parent,
    current_containment_veto,
    set_kcl_ok,
)


def _current(values: list[float]) -> list[float]:
    return values


def test_branch_limit_replaces_twelve_amp_only_rule() -> None:
    n = 600  # 10 min on the 30 s grid
    below = analyse_current({"x": _current([11.0] * n)})
    # Both 5-min and peak below the limit: not a high class.
    assert below.branch_tag["x"] is False
    assert below.max_5min_a["x"] <= 12.0
    # 5-min above 12 A OR peak above 16 A tags not_branch_circuit.
    sustained = analyse_current({"x": _current([13.0] * n)})
    assert sustained.branch_tag["x"] is True
    peak = analyse_current({"x": _current([17.0] + [1.0] * (n - 1))})
    assert peak.branch_tag["x"] is True
    assert peak.peak_a["x"] >= 16.0


def test_current_containment_veto_catches_a_reversed_ct() -> None:
    n = 2 * 200  # 200 minutes
    report = analyse_current(
        {"parent": _current([10.0] * n), "child": _current([11.0] * n)},
        parents={"child": ["parent"]},
    )
    # |I_child| = 11 > 1.03*10 + 0.3 = 10.6 on every minute: veto.
    assert current_containment_veto(report, "parent", "child") is True
    # A true pair stays below the limit.
    good = analyse_current(
        {"parent": _current([10.0] * n), "child": _current([8.0] * n)},
        parents={"child": ["parent"]},
    )
    assert current_containment_veto(good, "parent", "child") is False


def test_set_current_kcl_flags_a_double_counted_set() -> None:
    n = 2 * 1500  # 1500 minutes on the 30 s grid
    report = analyse_current(
        {
            "parent": _current([30.0] * n),
            "a": _current([20.0] * n),
            "b": _current([20.0] * n),
        }
    )
    assert set_kcl_ok(report, "parent", ["a"]) is True
    assert set_kcl_ok(report, "parent", ["a", "b"]) is False


def test_child_above_parent_veto() -> None:
    n = 100
    report = analyse_current(
        {"parent": _current([10.0] * n), "child": _current([20.0] * n)}
    )
    assert child_above_parent(report, "parent", "child") is True
    assert child_above_parent(report, "child", "parent") is False


def test_rating_class_and_role_hint() -> None:
    n = 600
    report = analyse_current(
        {
            "service": _current([70.0] * n),  # high class, median >= 1 A
            "appliance": _current([70.0] * (n // 2) + [0.0] * (n // 2)),
            "branch": _current([5.0] * n),
        }
    )
    assert report.rating_class["service"] == "highest"
    assert report.role_hint["service"] == "feeder"
    assert report.role_hint["branch"] == "branch"
    assert "rating_class" in report.as_reader_dict("service")
    assert "role_hint" in report.as_reader_dict("service")


def test_current_absent_is_a_silent_skip() -> None:
    report = analyse_current({})
    assert current_containment_veto(report, "p", "c") is False
    assert set_kcl_ok(report, "p", ["c"]) is True
    assert child_above_parent(report, "p", "c") is False
