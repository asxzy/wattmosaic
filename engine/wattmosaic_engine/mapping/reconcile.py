"""Deterministic, audited matrix reconciler (M2, design §8 / HB §10.4 / §14).

The reconciler answers exactly one question — "what does a topology or mapping
promotion invalidate?" — without rewriting any historical observation.  It is
scheduled on topology/mapping promotion and daily, and it:

* re-evaluates PROVISIONAL cells (they become CONFIRMED once their mapping edge
  is CONFIRMED, and stay PROVISIONAL while the edge is soft);
* applies SUSPECTED_CHANGE from D1 lifecycles;
* retires cells whose mapping edge is RETIRED — including the edges a promotion
  dropped from the active version (HB §17 step 10: a plug relocation leaves the
  old subject x reader cell otherwise live);
* restores a retired cell when a rollback makes its edge current again;
* writes one audit row describing the transition.

The version diff is read from the stored S14/S15 versions (a version's edge set
is frozen at proposal time), never from a single live view: the previous
version's edges are needed to know what a promotion retired, and the newly
promoted version's edges to know what is current.  The cells keep their
append-only history (``matrix_cell_versions``); a retired cell is removed from
the *current* sparse view so it no longer appears in the ``/matrix`` payload,
but every historical value remains queryable (HB §9.9).

It is **idempotent**: running it twice with the same inputs produces no further
cell versions and no additional audit rows, because the set of transitions is a
pure function of the stored versions and current cell state.  It is also
deterministic: a ``Clock`` is injected, so replay of the same inputs at the same
time is byte-identical.

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

        # Read the previous and the newly promoted version from the stored
        # versions, so an edge dropped by a promotion is still visible.  A
        # rollback (old edge current again) is the mirror case.
        retired_sources, restored_sources = self._source_changes()

        # A retired source may have no live cell left (an earlier pass already
        # removed it); still check its history so an idempotent pass changes no
        # cells.  Likewise a restored source may need a cell re-created from its
        # history when a rollback follows a retirement.
        candidate_keys = set(self.store._cells) | retired_sources | restored_sources
        for key in sorted(candidate_keys):
            live = key in self.store._cells
            status = (
                _value(self.store.cell(key).mapping.mapping_status)
                if live
                else MappingStatus.RETIRED.value
            )
            new_status: str | None = None
            clear_power = False
            restore_power: float | None = None

            # A version diff retires a cell whose (subject, reader) source edge
            # was dropped, and restores one whose edge became current again.
            if key in retired_sources:
                new_status = MappingStatus.RETIRED.value
                clear_power = True
            elif (
                key in restored_sources
                and status
                in (
                    MappingStatus.RETIRED.value,
                    MappingStatus.SUSPECTED_CHANGE.value,
                )
                and self.store.history(key)  # only a previously-live cell is restored
            ):
                new_status = MappingStatus.CONFIRMED.value
                restore_power = self._last_known_power(key)

            target = self._edge_lifecycle(key)
            if new_status is None:
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

            if new_status == MappingStatus.RETIRED.value:
                # Append one RETIRED history row, then drop the cell from the
                # current sparse view so /matrix no longer serves it.  Its
                # history is untouched (HB §17 step 10 / HB §9.9).
                if live:
                    self.store.set_status(
                        key, new_status, at=at, reason=reason, clear_power=clear_power
                    )
                self.store.retire(key, reason=reason)
                retired.append(key)
            else:
                self.store.set_status(
                    key,
                    new_status,
                    at=at,
                    reason=reason,
                    clear_power=clear_power,
                    restore_power=restore_power,
                )
                upserted.append(key)

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

    # -- version diff ------------------------------------------------------- #

    def _source_changes(self) -> tuple[set[CellKey], set[CellKey]]:
        """Return (retired, restored) cell keys from the stored version diff.

        A cell is keyed by ``(row_kind, row_id, reader_id)``; a mapping edge's
        ``(subject_kind, subject_id, endpoint_id)`` maps onto that key.  An edge
        present in the previous version but absent from the current one is a
        retirement; the reverse is a restore (a rollback that reactivates the
        old edge set under a new version id).
        """
        if self.versions is None:
            return set(), set()
        try:
            versions = self._version_rows()
            current_id = str(self.versions.current().mapping_version_id)
            current = self._edge_set(current_id)
            # The immediately preceding stored version is the one this pass
            # superseded; use the parent link when available.
            previous_id = self._previous_version_id(versions, current_id)
            previous = self._edge_set(previous_id) if previous_id else set()
        except (KeyError, ValueError, TypeError):
            return set(), set()
        # ``_edge_set`` already yields cell keys.
        return previous - current, current - previous

    def _version_rows(self) -> list[dict[str, Any]]:
        getter = getattr(self.versions, "_mapping_versions", None)
        if callable(getter):
            return list(getter())
        return []

    def _previous_version_id(
        self, versions: list[dict[str, Any]], current_id: str
    ) -> str | None:
        by_id = {str(row.get("version_id")): row for row in versions}
        row = by_id.get(current_id)
        if row is None:
            return None
        parent = row.get("parent_version_id")
        if parent is not None and str(parent) in by_id:
            return str(parent)
        # No parent link: fall back to the preceding stored version number.
        ordered = sorted(versions, key=lambda item: int(item.get("number", 0) or 0))
        ids = [str(item.get("version_id")) for item in ordered]
        index = ids.index(current_id) if current_id in ids else -1
        return ids[index - 1] if index > 0 else None

    def _edge_set(self, version_id: str) -> frozenset[tuple[str, str, str]]:
        view = self.versions.mapping_view(version_id)
        getter = getattr(view, "edges", None)
        edges = getter() if callable(getter) else ()
        # ``MappingView.edges()`` returns a *tuple* of contracts (not a dict —
        # the old ``.values()`` bug).  Iterate it directly.
        out: set[tuple[str, str, str]] = set()
        for edge in edges or ():
            key = self._edge_key(edge)
            if key is not None:
                out.add(key)
        return frozenset(out)

    @staticmethod
    def _edge_key(edge: Any) -> CellKey | None:
        kind = _value(getattr(edge, "subject_kind", "device"))
        if kind not in ("device", "component", "unknown_profile"):
            return None
        subject_id = getattr(edge, "subject_id", None)
        endpoint_id = getattr(edge, "endpoint_id", None)
        if subject_id is None or endpoint_id is None:
            return None
        return (kind, str(subject_id), str(endpoint_id))

    def _last_known_power(self, key: CellKey) -> float | None:
        """The most recent non-null power of ``key`` in cell history.

        A rollback restores the cell's honest last observation before it was
        retired; an absent value stays ``None`` (HB §18), never 0.
        """
        powers = [
            row.power.estimated_power_w
            for row in self.store.history(key)
            if getattr(row.power, "estimated_power_w", None) is not None
        ]
        return powers[-1] if powers else None

    # -- lifecycle lookup --------------------------------------------------- #

    def _mapping_edges(self) -> tuple[Any, ...]:
        if self.versions is None:
            return ()
        try:
            view = self.versions.mapping_view(
                self.versions.current().mapping_version_id
            )
            edges = getattr(view, "edges", None)
            result = edges() if callable(edges) else ()
            # The real ``MappingView.edges()`` returns a tuple of contracts (the
            # old ``.values()`` bug assumed a dict); tolerate both shapes so an
            # older dict-returning double keeps working.
            if isinstance(result, MappingABC):
                return tuple(result.values())
            return tuple(result)
        except (KeyError, ValueError, TypeError):
            return ()

    def _edge_lifecycle(self, key: CellKey) -> str | None:
        row_kind, row_id, reader_id = key
        if row_kind not in ("device", "component", "unknown_profile"):
            return None
        for edge in self._mapping_edges():
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
