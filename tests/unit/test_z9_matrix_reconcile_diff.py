"""Z9 — the matrix reconciler diffs stored mapping versions (HB §17 step 10).

The latent M2 bug: ``Reconciler._mapping_edges().values()`` was called on the
*tuple* returned by the real ``MappingView.edges()``, so no edge was ever
inspected; and only the *current* version's edges were read, so a retired edge
(dropped from the new version's edge-id list) was never found.  A promotion
therefore left the old subject x reader cells live in the current matrix.

These unit tests build a stored two-version mapping history (S15 rows) and a
``mapping_view`` whose ``edges()`` returns the real tuple shape, then assert
that the reconciler retires the old cell, keeps its history, and restores it
when a rollback makes the old edge current again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from wattmosaic_engine.contracts import (
    EdgeLifecycleV3,
    MappingStatus,
    VersionTriple,
)
from wattmosaic_engine.mapping import MatrixStore, Reconciler

T0 = datetime(2026, 4, 1, tzinfo=UTC)


def _clock(start: datetime | None = None) -> Any:
    current = start or T0

    def read() -> datetime:
        nonlocal current
        now = current
        current = current + timedelta(seconds=1)
        return now

    return read


class _Edge:
    """A minimal MappingEdge-shaped object (the real contracts also work)."""

    def __init__(self, subject: str, endpoint: str, lifecycle: EdgeLifecycleV3) -> None:
        self.subject_kind = "device"
        self.subject_id = subject
        self.endpoint_id = endpoint
        self.lifecycle = lifecycle


class _View:
    def __init__(self, edges: tuple[_Edge, ...]) -> None:
        self._edges = edges

    def edges(self) -> tuple[_Edge, ...]:
        # The real ``_FrozenMappingView.edges()`` returns a tuple, not a dict.
        return self._edges


class _StoredVersions:
    """A VersionService-shaped fake backed by stored S15 version rows."""

    def __init__(self, edges_by_version: dict[str, tuple[_Edge, ...]]) -> None:
        self._edges = edges_by_version
        self._current_id = list(edges_by_version)[-1]
        self.triple = VersionTriple(
            topology_version_id="topo-1",
            mapping_version_id=self._current_id,
            model_set_id="mset-1",
        )

    def current(self) -> VersionTriple:
        return self.triple

    def mapping_view(self, version_id: str) -> _View:
        return _View(self._edges[version_id])

    def _mapping_versions(self) -> list[dict[str, Any]]:
        ids = list(self._edges)
        return [
            {
                "version_id": version_id,
                "parent_version_id": ids[index - 1] if index else None,
                "status": "active" if version_id == self._current_id else "superseded",
            }
            for index, version_id in enumerate(ids)
        ]

    def set_current(self, version_id: str) -> None:
        self._current_id = version_id
        self.triple = self.triple.model_copy(update={"mapping_version_id": version_id})


def _store_with_kitchen_cell() -> MatrixStore:
    store = MatrixStore(clock=_clock())
    cell = ("device", "dishwasher", "kitchen_ckt")
    store.apply_samples(cell, power_w=1775.0, at=T0, event_group_id="evt-1")
    store.set_status(cell, MappingStatus.CONFIRMED.value, at=T0)
    return store


def test_promotion_retires_the_old_edge_cell_and_keeps_its_history() -> None:
    store = _store_with_kitchen_cell()
    versions = _StoredVersions(
        {
            "map-1": (_Edge("dishwasher", "kitchen_ckt", EdgeLifecycleV3.RETIRED),),
            "map-2": (_Edge("dishwasher", "laundry_ckt", EdgeLifecycleV3.CONFIRMED),),
        }
    )
    recon = Reconciler(store, versions, clock=_clock())
    delta = recon.reconcile("mapping_promotion")

    key = ("device", "dishwasher", "kitchen_ckt")
    assert key in delta.retired
    assert store.cell(key).mapping.mapping_status == MappingStatus.RETIRED.value
    assert store.cell(key).power.estimated_power_w is None
    # The historical observation is never rewritten.
    assert any(
        row.power.estimated_power_w == 1775.0 for row in store.history(key)
    )
    assert store.audit_log and store.audit_log[0]["reason"] == "mapping_promotion"


def test_retire_is_idempotent() -> None:
    store = _store_with_kitchen_cell()
    versions = _StoredVersions(
        {
            "map-1": (_Edge("dishwasher", "kitchen_ckt", EdgeLifecycleV3.RETIRED),),
            "map-2": (_Edge("dishwasher", "laundry_ckt", EdgeLifecycleV3.CONFIRMED),),
        }
    )
    recon = Reconciler(store, versions, clock=_clock())
    first = recon.reconcile("mapping_promotion")
    assert first.retired
    history_len = len(store.history(("device", "dishwasher", "kitchen_ckt")))
    second = recon.reconcile("mapping_promotion")
    assert second.retired == ()
    assert len(store.history(("device", "dishwasher", "kitchen_ckt"))) == history_len
    assert len(store.audit_log) == 1


def test_rollback_restores_the_old_edge_cell() -> None:
    store = _store_with_kitchen_cell()
    versions = _StoredVersions(
        {
            "map-1": (_Edge("dishwasher", "kitchen_ckt", EdgeLifecycleV3.RETIRED),),
            "map-2": (_Edge("dishwasher", "laundry_ckt", EdgeLifecycleV3.CONFIRMED),),
        }
    )
    recon = Reconciler(store, versions, clock=_clock())
    recon.reconcile("mapping_promotion")
    assert (
        store.cell(("device", "dishwasher", "kitchen_ckt")).mapping.mapping_status
        == MappingStatus.RETIRED.value
    )

    # A rollback creates a new version copying map-1's edge set.
    versions._edges["map-3"] = (
        _Edge("dishwasher", "kitchen_ckt", EdgeLifecycleV3.CONFIRMED),
    )
    versions.set_current("map-3")
    delta = recon.reconcile("mapping_rollback")

    key = ("device", "dishwasher", "kitchen_ckt")
    assert key in delta.upserted
    assert store.cell(key).mapping.mapping_status == MappingStatus.CONFIRMED.value
    assert store.cell(key).power.estimated_power_w == 1775.0
