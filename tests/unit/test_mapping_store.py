"""M2: matrix store — sparse current cells, append-only history, AT-11 counting.

Deterministic: a step clock is injected, no wall clock is read and no real DB is
required.  Absent values are ``None``, never ``0`` (HB §18).  Independent-event
counts are deduplicated by ``event_group_id`` (HB §10.3): 100 samples of one
event count as one event.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import (
    MappingStatus,
    Observability,
)
from wattmosaic_engine.mapping import CellEventsV3, MatrixStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _clock(start: datetime | None = None) -> object:
    current = start or T0

    def read() -> datetime:
        nonlocal current
        now = current
        current = current + timedelta(seconds=1)
        return now

    return read


def test_independent_event_count_dedupes_by_group_id() -> None:
    store = MatrixStore(clock=_clock())  # type: ignore[arg-type]
    cell = ("device", "light_1", "ct_a")
    store.apply_samples(cell, n=100, event_group_id="evt-1")
    assert store.cell(cell).event_association.independent_event_count == 1
    for eid in ("evt-2", "evt-3"):
        store.apply_samples(cell, n=10, event_group_id=eid)
    assert store.cell(cell).event_association.independent_event_count == 3
    assert store.cell(cell).confidence.event_association is not None
    assert store.cell(cell).confidence.event_association > 0.0


def test_initial_confidence_is_none_not_zero() -> None:
    store = MatrixStore(clock=_clock())  # type: ignore[arg-type]
    cell = ("device", "light_1", "ct_a")
    store.apply_samples(cell, n=1, event_group_id=None)
    assert store.cell(cell).confidence.event_association is None
    assert isinstance(store.cell(cell).event_association, CellEventsV3)


def test_history_keeps_old_values_and_is_append_only() -> None:
    store = MatrixStore(clock=_clock())  # type: ignore[arg-type]
    cell = ("device", "light_1", "ct_a")
    store.apply_samples(cell, n=1, event_group_id="evt-1", power_w=45.0, at=T0)
    store.apply_samples(
        cell, n=1, event_group_id="evt-2", power_w=55.0, at=T0 + timedelta(minutes=5)
    )
    history = store.history(cell)
    assert len(history) == 2
    assert history[0].power.estimated_power_w == 45.0
    assert history[1].power.estimated_power_w == 55.0
    assert store.cell(cell).power.estimated_power_w == 55.0
    # A later write never rewrites the earlier history entry.
    assert history[0].power.estimated_power_w == 45.0


def test_cells_as_of_returns_historical_state() -> None:
    store = MatrixStore(clock=_clock())  # type: ignore[arg-type]
    cell = ("device", "light_1", "ct_a")
    store.apply_samples(cell, power_w=42.0, at=T0)
    store.apply_samples(cell, power_w=55.0, at=T0 + timedelta(minutes=5))

    old = store.cells_as_of(T0 + timedelta(minutes=2))
    assert len(old) == 1
    assert old[0].power.estimated_power_w == 42.0
    newer = store.cells_as_of(T0 + timedelta(minutes=6))
    assert newer[0].power.estimated_power_w == 55.0
    assert store.cells_as_of(T0 - timedelta(seconds=1)) == []


def test_set_status_updates_current_and_appends_history() -> None:
    store = MatrixStore(clock=_clock())  # type: ignore[arg-type]
    cell = ("device", "light_1", "ct_a")
    store.apply_samples(cell, power_w=45.0, at=T0)
    store.set_status(
        cell,
        MappingStatus.RETIRED.value,
        at=T0 + timedelta(minutes=5),
        clear_power=True,
    )
    assert store.cell(cell).mapping.mapping_status == MappingStatus.RETIRED.value
    assert store.cell(cell).power.estimated_power_w is None
    assert len(store.history(cell)) == 2


def test_blank_cell_is_honest() -> None:
    store = MatrixStore(clock=_clock())  # type: ignore[arg-type]
    missing = store.cell(("device", "never_seen", "ct_x"))
    assert missing.power.estimated_power_w is None
    assert missing.confidence.event_association is None
    assert missing.mapping.observability in (
        Observability.INFERRED.value,
        Observability.INFERRED,
    )
