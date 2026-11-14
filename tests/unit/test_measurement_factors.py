"""Measurement factors: a scaling fact with a validity interval.

The Refoss ×2 case is the motivating one: a monitor reports one leg of a 240 V
load and either publishes the full load or expects the reader to double it.  A
bare ``CTChannel.measurement_multiplier`` float cannot answer *"what factor was
in force when this reading was taken"*, so a historical sample cannot be
reconstructed after the factor changes.

These tests cover the contract (interval validation, ordering) and the storage
helper that later resolves a factor for a timestamp.  They do **not** implement
the registry — that is WP3's ``electrical/factors.py``, written against this
stable surface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.contracts import MeasurementBasis, MeasurementFactor
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _factor(
    *,
    ct_id: str = "panel_l1",
    multiplier: float = 2.0,
    effective_from: datetime = T0,
    effective_to: datetime | None = None,
    applied_by: str = "user",
    declared_by: str = "user",
    note: str | None = None,
) -> MeasurementFactor:
    return MeasurementFactor(
        ct_id=ct_id,
        multiplier=multiplier,
        effective_from=effective_from,
        effective_to=effective_to,
        applied_by=applied_by,
        declared_by=declared_by,
        note=note,
    )


# --------------------------------------------------------------------------- #
# Contract validation
# --------------------------------------------------------------------------- #


def test_multiplier_must_be_strictly_positive() -> None:
    with pytest.raises(ValueError):
        _factor(multiplier=0.0)
    with pytest.raises(ValueError):
        _factor(multiplier=-1.0)


def test_multiplier_must_be_finite() -> None:
    with pytest.raises(ValueError):
        _factor(multiplier=float("inf"))


def test_an_inverted_interval_is_rejected() -> None:
    with pytest.raises(ValueError):
        _factor(effective_to=T0 - timedelta(seconds=1))


def test_a_zero_length_interval_is_rejected() -> None:
    """``effective_to == effective_from`` covers no instant at all."""
    with pytest.raises(ValueError):
        _factor(effective_to=T0)


def test_an_open_interval_is_accepted() -> None:
    f = _factor()
    assert f.effective_to is None
    assert f.applied_by == "user"
    assert f.declared_by == "user"
    assert MeasurementFactor.model_validate(f.model_dump()) == f


def test_applied_by_answers_whether_the_monitor_already_doubled() -> None:
    """The distinction that prevents double application."""
    monitor = _factor(applied_by="monitor", multiplier=1.0)
    user = _factor(applied_by="user", multiplier=2.0)
    unknown = _factor(applied_by="unknown")
    assert monitor.applied_by == "monitor"
    assert user.applied_by == "user"
    assert unknown.applied_by == "unknown"
    with pytest.raises(ValueError):
        _factor(applied_by="someone")


def test_a_factor_carries_its_basis_for_the_reading() -> None:
    """A factor-adjusted reading must say so; a plain one must not."""
    assert MeasurementBasis.FACTOR_ADJUSTED.value == "factor_adjusted"
    assert MeasurementBasis.CT_MEASURED.value == "ct_measured"


def _check_no_overlap(
    existing: list[MeasurementFactor], candidate: MeasurementFactor
) -> None:
    """The validator the DB helper is required to apply (shared helper shape)."""
    from wattmosaic_engine.contracts import validate_factor_intervals

    validate_factor_intervals([*existing, candidate])


def test_overlapping_intervals_for_one_ct_are_rejected() -> None:
    existing = [_factor(effective_from=T0, effective_to=T0 + timedelta(days=30))]
    overlap = _factor(
        effective_from=T0 + timedelta(days=10),
        effective_to=T0 + timedelta(days=40),
    )
    with pytest.raises(ValueError):
        _check_no_overlap(existing, overlap)


def test_adjacent_non_overlapping_intervals_are_accepted() -> None:
    """A factor change at t: the old interval ends exactly where the new begins."""
    first = _factor(
        multiplier=2.0, effective_from=T0, effective_to=T0 + timedelta(days=30)
    )
    second = _factor(
        multiplier=1.0,
        effective_from=T0 + timedelta(days=30),
        effective_to=None,
    )
    # The shared helper raises only for a genuine overlap.
    _check_no_overlap([first], second)
    merged = [first, second]
    assert len(merged) == 2


def test_intervals_on_different_cts_may_overlap() -> None:
    a = _factor(ct_id="ct_a", effective_from=T0)
    b = _factor(ct_id="ct_b", effective_from=T0)
    from wattmosaic_engine.contracts import validate_factor_intervals

    validate_factor_intervals([a, b])  # different ct_id -> not a conflict


def test_open_ended_intervals_overlap_everything_after_them() -> None:
    existing = [_factor(effective_from=T0)]  # never ends
    later = _factor(effective_from=T0 + timedelta(days=90))
    with pytest.raises(ValueError):
        _check_no_overlap(existing, later)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def test_factor_round_trips_through_storage(tmp_path) -> None:
    db = Database(tmp_path / "f.db")
    try:
        db.insert_measurement_factor(
            _factor(
                effective_from=T0,
                effective_to=T0 + timedelta(days=30),
                applied_by="monitor",
                declared_by="import",
                note="Refoss published the whole load",
            )
        )
        rows = db.factors_for_ct("panel_l1")
        assert len(rows) == 1
        row = rows[0]
        assert row["ct_id"] == "panel_l1"
        assert row["multiplier"] == pytest.approx(2.0)
        assert row["effective_from"]
        assert row["effective_to"]
        assert row["applied_by"] == "monitor"
        assert row["declared_by"] == "import"
        assert row["note"] == "Refoss published the whole load"
    finally:
        db.close()


def test_factors_for_ct_are_ordered_by_effective_from(tmp_path) -> None:
    db = Database(tmp_path / "order.db")
    try:
        db.insert_measurement_factor(
            _factor(multiplier=1.0, effective_from=T0 + timedelta(days=30))
        )
        db.insert_measurement_factor(
            _factor(
                multiplier=2.0,
                effective_from=T0,
                effective_to=T0 + timedelta(days=30),
            )
        )
        rows = db.factors_for_ct("panel_l1")
        assert [r["multiplier"] for r in rows] == [2.0, 1.0]

        # A caller resolving a timestamp finds exactly one interval covering it.
        import json

        resolved = [
            r
            for r in rows
            if json.loads(json.dumps(r["effective_from"]))
            <= (T0 + timedelta(days=45)).isoformat()
            and (r["effective_to"] is None or (T0 + timedelta(days=45)).isoformat() < r["effective_to"])
        ]
        assert [r["multiplier"] for r in resolved] == [1.0]
    finally:
        db.close()


def test_a_second_overlapping_factor_for_one_ct_is_refused(tmp_path) -> None:
    """Storage is the last line of defence for the interval invariant."""
    db = Database(tmp_path / "overlap.db")
    try:
        db.insert_measurement_factor(
            _factor(effective_from=T0, effective_to=T0 + timedelta(days=30))
        )
        with pytest.raises(ValueError):
            db.insert_measurement_factor(
                _factor(
                    effective_from=T0 + timedelta(days=1),
                    effective_to=T0 + timedelta(days=60),
                )
            )
        assert len(db.factors_for_ct("panel_l1")) == 1
    finally:
        db.close()
