"""M2: deterministic, audited reconciler (HB §14 / §10.4, AT-15/AT-16 ideas).

The reconciler re-evaluates PROVISIONAL cells, applies SUSPECTED_CHANGE,
retires cells whose mapping edge is RETIRED, and writes an audit row.  It is
idempotent: a second pass changes nothing and writes no extra audit row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EdgeLifecycleV3,
    MappingEdge,
    MappingStatus,
    Observability,
    VersionTriple,
)
from wattmosaic_engine.mapping import MatrixStore, Reconciler

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


def _clock(start: datetime | None = None) -> Any:
    current = start or T0

    def read() -> datetime:
        nonlocal current
        now = current
        current = current + timedelta(seconds=1)
        return now

    return read


def _edge(subject: str, reader: str, lifecycle: EdgeLifecycleV3) -> MappingEdge:
    return MappingEdge(
        mapping_edge_id=f"m::{subject}->{reader}",
        subject_kind="device",
        subject_id=subject,
        endpoint_id=reader,
        observability=Observability.DIRECT,
        lifecycle=lifecycle,
        declared_by="inference",
        evidence=EdgeEvidence(),
        valid_from=T0,
    )


class _Versions:
    def __init__(self, edges: list[MappingEdge]) -> None:
        self.triple = VT
        self._edges = edges

    def current(self) -> VersionTriple:
        return self.triple

    def mapping_view(self, version_id: str) -> Any:
        edges = {e.mapping_edge_id: e for e in self._edges}

        class View:
            def edges(self) -> Any:
                return edges

        return View()


def store_with(status: str, power: float | None = 45.0) -> MatrixStore:
    store = MatrixStore(clock=_clock())
    cell = ("device", "light-1", "Circuit A")
    store.apply_samples(cell, power_w=power, at=T0)
    store.set_status(cell, status, at=T0)
    return store


def test_provisional_becomes_confirmed_when_edge_confirmed() -> None:
    store = store_with(MappingStatus.PROVISIONAL.value)
    versions = _Versions([_edge("light-1", "Circuit A", EdgeLifecycleV3.CONFIRMED)])
    delta = Reconciler(store, versions, clock=_clock()).reconcile("topology_promotion")
    cell = ("device", "light-1", "Circuit A")
    assert store.cell(cell).mapping.mapping_status == MappingStatus.CONFIRMED.value
    assert delta.upserted == (cell,)
    assert len(store.audit_log) == 1


def test_retired_edge_retires_cell_and_clears_power() -> None:
    store = store_with(MappingStatus.CONFIRMED.value)
    versions = _Versions([_edge("light-1", "Circuit A", EdgeLifecycleV3.RETIRED)])
    delta = Reconciler(store, versions, clock=_clock()).reconcile("plug_relocation")
    cell = ("device", "light-1", "Circuit A")
    assert store.cell(cell).mapping.mapping_status == MappingStatus.RETIRED.value
    assert store.cell(cell).power.estimated_power_w is None
    assert delta.retired == (cell,)


def test_suspected_change_from_lifecycle_is_applied() -> None:
    store = store_with(MappingStatus.CONFIRMED.value)
    versions = _Versions([_edge("light-1", "Circuit A", EdgeLifecycleV3.SUSPECTED_CHANGE)])
    Reconciler(store, versions, clock=_clock()).reconcile("suspected")
    cell = ("device", "light-1", "Circuit A")
    assert store.cell(cell).mapping.mapping_status == MappingStatus.SUSPECTED_CHANGE.value


def test_reconcile_is_idempotent_and_writes_one_audit_row() -> None:
    store = store_with(MappingStatus.CONFIRMED.value)
    versions = _Versions([_edge("light-1", "Circuit A", EdgeLifecycleV3.RETIRED)])
    recon = Reconciler(store, versions, clock=_clock())
    first = recon.reconcile("plug_relocation")
    assert first.retired
    assert len(store.audit_log) == 1
    history_len = len(store.history(("device", "light-1", "Circuit A")))

    second = recon.reconcile("plug_relocation")
    assert second.upserted == ()
    assert second.retired == ()
    assert len(store.audit_log) == 1
    assert len(store.history(("device", "light-1", "Circuit A"))) == history_len


def test_reconcile_writes_audit_row_with_version_ids() -> None:
    store = store_with(MappingStatus.PROVISIONAL.value)
    versions = _Versions([_edge("light-1", "Circuit A", EdgeLifecycleV3.CONFIRMED)])
    Reconciler(store, versions, clock=_clock()).reconcile("daily")
    assert len(store.audit_log) == 1
    audit = store.audit_log[0]
    assert audit["reason"] == "daily"
    assert audit["version_ids"]["mapping_version_id"] == "map-1"
