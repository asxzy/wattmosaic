"""Sparse current matrix and its append-only history (M2, design §8 / HB §9–10).

:class:`MatrixStore` persists the current sparse matrix together with an
append-only history, so a historical ``cells_as_of(t)`` query returns the state
that was live at ``t`` and never rewrites evidence.  It is the single funnel for
matrix writes: every change — an event-driven incremental update, a reconciler
transition, or the simple sample counter used by AT-11 — goes through
:meth:`apply_cells` / :meth:`apply_samples`, which append exactly one history row
per changed cell.

Persistence is delegated to the S15 :class:`~wattmosaic_engine.storage.repositories.mapping.MappingRepository`
when one is injected: its ``upsert_cells`` writes the current row and the history
row in one ``BEGIN IMMEDIATE`` transaction (no half-updated matrix is ever
observable).  Without a repository the store is a pure in-memory value, which is
what makes the deterministic tests and the AT-11 counter cheap to run.  No wall
clock is read here: a ``Clock`` is injected (a deterministic step clock is the
default so repeated runs produce identical timestamps).

Null policy (HB §18): an absent power or confidence is ``None``, never ``0``.
Event counts are integers (they are counts, not measurements) and are
deduplicated by ``event_group_id`` — 100 samples of one event are one event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from wattmosaic_engine.contracts import (
    CellEvents,
    CellPower,
    ConfidenceVectorV3,
    MappingCell,
    Observability,
)
from wattmosaic_engine.runtime.interfaces import Clock

__all__: list[str] = [
    "CellEventsV3",
    "MatrixStore",
]

CellKey = tuple[str, str, str]


class CellEventsV3(CellEvents):
    """The v3 event block with the independent-event counter of HB §10.3.

    Extends the K05 :class:`CellEvents` instead of redefining it: the base
    counters (``matched_event_count`` etc.) keep their meaning, and
    ``independent_event_count`` is the number of *distinct* ``event_group_id``
    values ever associated with the cell (not the number of samples).
    """

    independent_event_count: int = 0
    matched_event_ids: tuple[str, ...] = ()


class _StepClock:
    """A deterministic clock: fixed origin, advancing by one step per read."""

    def __init__(self, start: datetime | None = None, step: timedelta | None = None) -> None:
        self._current = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._step = step or timedelta(seconds=1)

    def __call__(self) -> datetime:
        now = self._current
        self._current = now + self._step
        return now


@dataclass(frozen=True)
class _CellState:
    """The mutable-free state behind one current cell."""

    base: MappingCell
    event_ids: tuple[str, ...] = ()
    sample_count: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None

    def decorated(self, at: datetime) -> MappingCell:
        """Return the public cell with event/confidence/timestamp blocks refreshed."""
        independent = len(self.event_ids)
        events = CellEventsV3(
            matched_event_count=max(self.sample_count, independent),
            confirmed_match_count=independent,
            provisional_match_count=0,
            avg_response_delay_ms=None,
            response_signature=None,
            last_matched_at=self.last_at,
            independent_event_count=independent,
            matched_event_ids=self.event_ids,
        )
        confidence = self.base.confidence.model_copy(
            update={
                "event_association": (
                    min(1.0, independent / 5.0) if independent > 0 else None
                )
            }
        )
        first = self.first_at or at
        last = self.last_at or at
        timestamps = self.base.timestamps.model_copy(
            update={"first_observed_at": first, "last_observed_at": last, "updated_at": at}
        )
        return self.base.model_copy(
            update={
                "event_association": events,
                "confidence": confidence,
                "timestamps": timestamps,
            }
        )


@dataclass(frozen=True)
class _HistoryRow:
    at: datetime
    cell: MappingCell
    reason: str
    event_group_id: str | None


class MatrixStore:
    """The sparse current matrix plus its append-only history (M2).

    Parameters
    ----------
    repo:
        An optional S15 mapping repository.  When present every batch is
        persisted through its one-transaction ``upsert_cells``.
    clock:
        Injected UTC clock.  Defaults to a deterministic step clock (no wall
        clock is ever read by this module).
    """

    def __init__(self, repo: Any | None = None, clock: Clock | None = None) -> None:
        self._repo = repo
        self._clock: Clock = clock or _StepClock()
        self._cells: dict[CellKey, _CellState] = {}
        self._history: list[_HistoryRow] = []
        self.audit_log: list[dict[str, Any]] = []

    # -- reads -------------------------------------------------------------- #

    @staticmethod
    def key(cell: tuple[str, str, str] | MappingCell) -> CellKey:
        """Normalise a cell reference to ``(row_kind, row_id, reader_id)``."""
        if isinstance(cell, MappingCell):
            return (str(cell.row_kind), str(cell.row_id), str(cell.reader_id))
        if len(cell) == 2:  # (row_id, reader_id) shorthand -> device row
            return ("device", str(cell[0]), str(cell[1]))
        return (str(cell[0]), str(cell[1]), str(cell[2]))

    def has(self, cell: tuple[str, str, str] | MappingCell) -> bool:
        return self.key(cell) in self._cells

    def cell(self, cell: tuple[str, str, str] | MappingCell) -> MappingCell:
        """The current state of one cell (a blank cell when it is unknown)."""
        key = self.key(cell)
        state = self._cells.get(key)
        if state is None:
            stamp = self._clock()
            return _CellState(base=_blank_cell(key, stamp)).decorated(stamp)
        return state.decorated(self._clock())

    def cells(self) -> list[MappingCell]:
        """Every current cell, in a stable order."""
        stamp = self._clock()
        return [self._cells[key].decorated(stamp) for key in sorted(self._cells)]

    def history(self, cell: tuple[str, str, str] | MappingCell) -> list[MappingCell]:
        """Append-only history of one cell, oldest first."""
        key = self.key(cell)
        return [row.cell for row in self._history if self.key(row.cell) == key]

    def cells_as_of(self, at: datetime) -> list[MappingCell]:
        """The state of every cell at ``at`` (HB §9.9 historical query).

        A version is live at ``at`` when it was appended at or before ``at``.
        The latest such version wins per cell.  This never consults the current
        view, so old values survive later updates untouched.
        """
        latest: dict[CellKey, MappingCell] = {}
        for row in self._history:
            if row.at > at:
                continue
            latest[self.key(row.cell)] = row.cell
        return [latest[key] for key in sorted(latest)]

    # -- writes ------------------------------------------------------------- #

    def apply_cells(
        self,
        cells: Any,
        *,
        at: datetime | None = None,
        reason: str = "",
        event_group_id: str | None = None,
    ) -> tuple[CellKey, ...]:
        """Upsert a batch of recomputed cells, appending one history row each."""
        stamp = at or self._clock()
        written: list[CellKey] = []
        persistable: list[MappingCell] = []
        for cell in cells:
            key = self.key(cell)
            previous = self._cells.get(key)
            event_ids = list(previous.event_ids) if previous is not None else []
            if event_group_id is not None and event_group_id not in event_ids:
                event_ids.append(event_group_id)
            sample_count = (previous.sample_count if previous is not None else 0) + (
                1 if event_group_id is not None else 0
            )
            first_at = previous.first_at if previous is not None else None
            if first_at is None:
                first_at = stamp
            state = _CellState(
                base=cell,
                event_ids=tuple(event_ids),
                sample_count=sample_count,
                first_at=first_at,
                last_at=stamp,
            )
            decorated = state.decorated(stamp)
            self._cells[key] = state
            self._history.append(
                _HistoryRow(stamp, decorated, reason, event_group_id)
            )
            persistable.append(decorated)
            written.append(key)
        if self._repo is not None and persistable:
            mapping_version = persistable[0].provenance.mapping_version_id
            self._repo.upsert_cells(
                [_for_persistence(cell) for cell in persistable],
                mapping_version_id=mapping_version,
                at=stamp,
            )
        return tuple(written)

    def apply_samples(
        self,
        cell: tuple[str, str, str] | MappingCell,
        *,
        n: int = 1,
        event_group_id: str | None = None,
        at: datetime | None = None,
        power_w: float | None = None,
    ) -> CellKey:
        """Record ``n`` samples for one cell under one ``event_group_id``.

        Independent-event counting is deduplicated by group id (HB §10.3):
        calling this 100 times with the same id adds 100 samples but one
        independent event.  ``power_w``, when supplied, is the current
        contribution; ``None`` means no contribution is known (never 0).
        """
        stamp = at or self._clock()
        key = self.key(cell)
        previous = self._cells.get(key)
        event_ids = list(previous.event_ids) if previous is not None else []
        if event_group_id is not None and event_group_id not in event_ids:
            event_ids.append(event_group_id)
        base = previous.base if previous is not None else _blank_cell(key, stamp)
        if power_w is not None:
            base = base.model_copy(
                update={
                    "power": base.power.model_copy(
                        update={"estimated_power_w": power_w}
                    )
                }
            )
        state = _CellState(
            base=base,
            event_ids=tuple(event_ids),
            sample_count=(previous.sample_count if previous is not None else 0)
            + max(0, int(n)),
            first_at=(previous.first_at if previous is not None else stamp),
            last_at=stamp,
        )
        decorated = state.decorated(stamp)
        self._cells[key] = state
        self._history.append(
            _HistoryRow(stamp, decorated, "sample", event_group_id)
        )
        if self._repo is not None:
            self._repo.upsert_cells(
                [_for_persistence(decorated)],
                mapping_version_id=decorated.provenance.mapping_version_id,
                at=stamp,
            )
        return key

    def set_status(
        self,
        cell: tuple[str, str, str] | MappingCell,
        status: str,
        *,
        at: datetime | None = None,
        reason: str = "reconcile",
        clear_power: bool = False,
    ) -> CellKey:
        """Set a cell's mapping status (used by the reconciler)."""
        stamp = at or self._clock()
        key = self.key(cell)
        previous = self._cells.get(key)
        if previous is None:
            previous = _CellState(base=_blank_cell(key, stamp))
        power = previous.base.power
        if clear_power:
            power = power.model_copy(update={"estimated_power_w": None})
        mapping = previous.base.mapping.model_copy(update={"mapping_status": status})
        base = previous.base.model_copy(update={"mapping": mapping, "power": power})
        state = _CellState(
            base=base,
            event_ids=previous.event_ids,
            sample_count=previous.sample_count,
            first_at=previous.first_at or stamp,
            last_at=stamp,
        )
        decorated = state.decorated(stamp)
        self._cells[key] = state
        self._history.append(_HistoryRow(stamp, decorated, reason, None))
        if self._repo is not None:
            self._repo.upsert_cells(
                [_for_persistence(decorated)],
                mapping_version_id=decorated.provenance.mapping_version_id,
                at=stamp,
            )
        return key

    def append_audit(self, row: dict[str, Any]) -> None:
        """Append one reconciliation audit row (never edit an earlier row)."""
        self.audit_log.append(dict(row))


def _for_persistence(cell: MappingCell) -> dict[str, Any]:
    """Flatten a cell for S15 so subclass fields are not lost on serialization.

    S15's ``_cell_fields`` dumps a :class:`MappingCell` with its base-class
    serializer; our :class:`CellEventsV3` adds ``independent_event_count`` and
    ``matched_event_ids``, which that serializer would drop.  Passing an
    explicit nested dict preserves those fields in ``event_association_json``.
    """
    raw = cell.model_dump(mode="json")
    raw["event_association"] = cell.event_association.model_dump(mode="json")
    return raw


def _blank_cell(key: CellKey, at: datetime) -> MappingCell:
    """A minimal honest cell for a key that has only sample evidence so far."""
    from wattmosaic_engine.contracts import (
        CellMapping,
        CellProvenance,
        CellTimestamps,
        CellVisual,
        EstimateStatus,
        IdentifiabilityStatus,
        MappingStatus,
    )

    row_kind, row_id, reader_id = key
    return MappingCell(
        row_kind=row_kind,  # type: ignore[arg-type]
        row_id=row_id,
        reader_id=reader_id,
        mapping=CellMapping(
            observability=Observability.INFERRED,
            relation_type=None,
            mapping_status=MappingStatus.LEARNING,
            valid_from=at,
        ),
        power=CellPower(measurement_status=EstimateStatus.UNKNOWN),
        identifiability=IdentifiabilityStatus.INSUFFICIENT_EVIDENCE,
        event_association=CellEventsV3(),
        confidence=ConfidenceVectorV3(),
        provenance=CellProvenance(
            topology_version_id="", mapping_version_id="", model_version_id=None
        ),
        timestamps=CellTimestamps(
            first_observed_at=at, last_observed_at=at, updated_at=at
        ),
        visual=CellVisual.UNKNOWN,
    )
