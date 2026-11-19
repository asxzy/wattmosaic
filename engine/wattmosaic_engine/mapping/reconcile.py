"""Deterministic, audited matrix reconciler (M2, design §8 / HB §10.4 / §14).

The reconciler answers exactly one question — "what does a topology or mapping
promotion invalidate?" — without rewriting any historical observation.  It is
scheduled on topology/mapping promotion and daily, and it:

* re-evaluates PROVISIONAL cells (they become CONFIRMED once their mapping edge
  is CONFIRMED, and stay PROVISIONAL while the edge is soft);
* applies SUSPECTED_CHANGE from D1 lifecycles;
* retires cells whose mapping edge is RETIRED;
* writes one audit row describing the transition.

It is **idempotent**: running it twice with the same inputs produces no further
cell versions and no additional audit rows, because the set of transitions is a
pure function of the current cell state.  It is also deterministic: a ``Clock``
is injected, so replay of the same inputs at the same time is byte-identical.

The structure is ported from the legacy pure ``reconcile_topology_change``
(``disaggregation/mapping.py:782-958``), but uses one version type (the
``VersionTriple`` / string version ids of ``analysis/E-…`` T13) instead of
comparing strings against integers.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from datetime import UTC, datetime
from typing import Any

from wattmosaic_engine.contracts import EdgeLifecycleV3, MappingStatus
from wattmosaic_engine.runtime.interfaces import Clock, MatrixDelta, VersionService

from .store import CellKey, MatrixStore

__all__: list[str] = ["Reconciler"]


def _value(item: Any) -> str:
    return str(item.value) if hasattr(item, "value") else str(item)


class Reconciler:
    """Idempotent, audited reconciliation of the matrix cell store."""

    def __init__(
        self,
        store: MatrixStore,
        versions: VersionService | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.versions = versions
        self.clock: Clock = clock or (lambda: datetime(2026, 1, 1, tzinfo=UTC))

    def reconcile(self, reason: str) -> MatrixDelta:
        """Run one deterministic pass; return the cells it changed."""
        at = self.clock()
        retired: list[CellKey] = []
        upserted: list[CellKey] = []

        for key in sorted(self.store._cells):
            cell = self.store.cell(key)
            status = _value(cell.mapping.mapping_status)
            new_status: str | None = None
            clear_power = False

            target = self._edge_lifecycle(key)
            if target == EdgeLifecycleV3.RETIRED.value:
                new_status = MappingStatus.RETIRED.value
                clear_power = True
            elif target == EdgeLifecycleV3.SUSPECTED_CHANGE.value:
                new_status = MappingStatus.SUSPECTED_CHANGE.value
            elif (
                status == MappingStatus.PROVISIONAL.value
                and target == EdgeLifecycleV3.CONFIRMED.value
            ):
                new_status = MappingStatus.CONFIRMED.value

            if new_status is None or new_status == status:
                continue

            self.store.set_status(
                key, new_status, at=at, reason=reason, clear_power=clear_power
            )
            (retired if new_status == MappingStatus.RETIRED.value else upserted).append(key)

        # Idempotence: a second pass changes no cells, so no audit row is added
        # when there is nothing to report.
        if upserted or retired:
            self.store.append_audit(
                {
                    "reason": reason,
                    "at": at.isoformat(),
                    "upserted": [list(key) for key in upserted],
                    "retired": [list(key) for key in retired],
                    "version_ids": self._version_ids(),
                }
            )
        return MatrixDelta(
            upserted=tuple(upserted),
            retired=tuple(retired),
            reason=reason,
        )

    # -- lifecycle lookup --------------------------------------------------- #

    def _mapping_edges(self) -> MappingABC[Any, Any]:
        if self.versions is None:
            return {}
        try:
            view = self.versions.mapping_view(
                self.versions.current().mapping_version_id
            )
            edges = getattr(view, "edges", None)
            return edges() if callable(edges) else {}
        except (KeyError, ValueError, TypeError):
            return {}

    def _edge_lifecycle(self, key: CellKey) -> str | None:
        row_kind, row_id, reader_id = key
        if row_kind not in ("device", "component", "unknown_profile"):
            return None
        for edge in self._mapping_edges().values():
            if str(edge.subject_id) == row_id and str(edge.endpoint_id) == reader_id:
                return _value(getattr(edge, "lifecycle", None))
        return None

    def _version_ids(self) -> dict[str, str]:
        if self.versions is None:
            return {}
        triple = self.versions.current()
        return {
            "topology_version_id": triple.topology_version_id,
            "mapping_version_id": triple.mapping_version_id,
            "model_set_id": triple.model_set_id,
        }
