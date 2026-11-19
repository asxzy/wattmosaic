"""Configuration-change and self-healing workflow (T7, HB §14 steps 1-10).

This module turns a :class:`~wattmosaic_engine.contracts.ConfigurationEvent` into
a promoted topology/mapping version without ever touching a power-model write
API.  It is **pure**: every clock, repository and collaborator is injected, no
wall clock or database connection is created here, and ``runtime.fakes`` is
never imported.

HB §14 steps implemented here
-----------------------------
1. ``record_*`` produces a SUSPECTED ``ConfigurationEvent`` through the injected
   :class:`VersionService` Protocol (declared or inferred detector output).
2. :meth:`ConfigurationWorkflow.begin` calls ``mark_suspected`` (M2 flips cells
   to SUSPECTED_CHANGE) and collects evidence through T5 / M2 match history.
3. :meth:`ConfigurationWorkflow.observe_confirming_event` accumulates
   independent corroborations; at ``CONFIRMED_EVENTS`` it promotes a candidate
   version through the version service.
4. :meth:`ConfigurationWorkflow.reject_expired` restores lifecycles after
   ``REJECT_AFTER`` when the suspicion was never corroborated.
5. On promotion a reconciliation request (L7) and an audit entry are emitted;
   the model-set id in the current triple is asserted unchanged.

Power-model immutability is asserted in code by :func:`assert_no_model_write`:
the workflow only ever holds a read handle to the model-set id, and the
:class:`_NoModelWrite` guard raises on any attribute that looks like a power
model write API.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from wattmosaic_engine.contracts import (
    ConfigurationChangeKind,
    ConfigurationEvent,
)
from wattmosaic_engine.runtime.interfaces import VersionService

__all__: list[str] = [
    "CONFIRMED_EVENTS",
    "REJECT_AFTER",
    "ConfigurationRecord",
    "ConfigurationWorkflow",
    "NoModelWriteError",
    "assert_no_model_write",
]

#: Number of independent corroborating events required to promote a suspected
#: change (design §5: "a new topology version after N events").
CONFIRMED_EVENTS = 3
#: A suspicion with no corroboration is rejected and its lifecycles restored
#: after this timeout (task step 2).
REJECT_AFTER = timedelta(days=7)

_MODEL_WRITE_NAMES = frozenset(
    {
        "save_model_version",
        "append_version",
        "append_event",
        "set_model_status",
        "write_model",
        "upsert_model",
        "delete_model",
        "promote_model",
        "create_model_version",
        "update_model",
    }
)


class NoModelWriteError(RuntimeError):
    """Raised when code tries to reach a power-model write API from T7."""


class _NoModelWrite:
    """A guard exposing only the read surface of a power-model repository.

    Any attribute whose name looks like a write API raises
    :class:`NoModelWriteError`; even a plain attribute read is denied, because
    T7 never needs the model repository at all — it only reads the immutable
    ``model_set_id`` from the current :class:`VersionTriple`.
    """

    def __getattr__(self, name: str) -> Any:
        if name in _MODEL_WRITE_NAMES or name.startswith(("write_", "set_", "save_", "upsert_")):
            raise NoModelWriteError(f"the self-healing workflow must never call {name}()")
        raise AttributeError(name)


def assert_no_model_write(model_repo: Any) -> None:
    """Assert ``model_repo`` cannot be used to write a power model by T7.

    Production composes :class:`_NoModelWrite` in place of a real repository; a
    caller may pass its own strict double to this function in tests.  The check
    probes the known write-API names and raises :class:`NoModelWriteError` if any
    of them resolves to a callable.
    """
    for name in sorted(_MODEL_WRITE_NAMES):
        try:
            candidate = getattr(model_repo, name)
        except (AttributeError, NoModelWriteError):
            continue
        if callable(candidate):
            raise NoModelWriteError(f"the self-healing workflow must never call {name}()")


@dataclass
class ConfigurationRecord:
    """Mutable workflow state for one SUSPECTED configuration event."""

    event: ConfigurationEvent
    created_at: Any
    edge_ids: tuple[str, ...] = ()
    mapping_edge_ids: tuple[str, ...] = ()
    affected_endpoints: tuple[str, ...] = ()
    candidate_topology_version_id: str | None = None
    candidate_mapping_version_id: str | None = None
    corroborations: int = 0
    independent_sources: set[str] = field(default_factory=set)
    status: str = "suspected"
    resolved_at: Any = None

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def kind(self) -> ConfigurationChangeKind:
        return self.event.kind


class ConfigurationWorkflow:
    """HB §14 workflow over the injected D1/M2/T5 adapters.

    ``versions`` is the :class:`~wattmosaic_engine.runtime.interfaces.VersionService`
    Protocol (the real D1 service in production, a deterministic double in
    tests).  ``clock`` is injected; no wall clock is read here.  ``audit`` and
    ``reconcile`` are optional append-only sinks (callables) so the workflow can
    stay pure while still emitting HB §14 step 3/9 side effects.
    """

    def __init__(
        self,
        versions: VersionService,
        *,
        clock: Any,
        model_repo: Any | None = None,
        audit: Any | None = None,
        reconcile: Any | None = None,
        confirmed_events: int = CONFIRMED_EVENTS,
        reject_after: timedelta = REJECT_AFTER,
    ) -> None:
        self.versions = versions
        self.clock = clock
        self.audit = audit
        self.reconcile = reconcile
        self.confirmed_events = max(1, int(confirmed_events))
        self.reject_after = reject_after
        # T7 never receives a writable model repository.  Guard the seam so a
        # future caller cannot accidentally couple the model lifecycle here.
        assert_no_model_write(model_repo if model_repo is not None else _NoModelWrite())
        self._records: dict[str, ConfigurationRecord] = {}

    # -- step 1: detectors record SUSPECTED events -------------------------- #
    def record(self, event: ConfigurationEvent) -> ConfigurationRecord:
        """Record one declared/inferred SUSPECTED event through D1."""
        self.versions.record_configuration_event(event)
        return self.register(event)

    def register(self, event: ConfigurationEvent) -> ConfigurationRecord:
        """Register an event that D1 has already recorded (no duplicate write)."""
        existing = self._records.get(event.event_id)
        if existing is not None:
            return existing
        record = ConfigurationRecord(event=event, created_at=self.clock())
        self._records[record.event_id] = record
        return record

    def record_inferred(self, change: Any) -> ConfigurationRecord:
        """Record one :class:`InferredChange` as a SUSPECTED ConfigurationEvent.

        This is HB §14 step 1 for measurement-inferred detectors (design §5):
        the T7 workflow turns a detector proposal into a typed event through the
        injected VersionService Protocol.
        """
        from wattmosaic_engine.contracts import (
            ChangeDetection,
            ChangeStatus,
            ConfigurationEvent,
        )

        event = ConfigurationEvent(
            event_id=f"evt-inferred-{getattr(change.kind, 'value', change.kind)}-{change.subject}",
            kind=change.kind,
            detection=ChangeDetection.INFERRED,
            status=ChangeStatus.SUSPECTED,
            affected_endpoint_ids=[change.subject],
            created_at=self.clock(),
        )
        return self.record(event)

    # -- step 2: mark affected lifecycles ----------------------------------- #
    def begin(
        self,
        change_event_id: str,
        *,
        edge_ids: Sequence[str] = (),
        mapping_edge_ids: Sequence[str] = (),
        affected_endpoints: Sequence[str] = (),
    ) -> ConfigurationRecord:
        """Mark affected edges/mapping edges SUSPECTED_CHANGE (HB §14 step 3).

        M2's reconciler reads those lifecycles and flips the affected matrix
        cells to SUSPECTED_CHANGE, degrading downstream confidence.
        """
        record = self._records[change_event_id]
        edge_ids = tuple(edge_ids)
        mapping_edge_ids = tuple(mapping_edge_ids)
        self.versions.mark_suspected(change_event_id, edge_ids, mapping_edge_ids)
        record.edge_ids = edge_ids
        record.mapping_edge_ids = mapping_edge_ids
        record.affected_endpoints = tuple(affected_endpoints)
        # The stored event is updated in the workflow's own append-only ledger;
        # D1's copy is immutable.  We keep a replacement contract here so the
        # affected ids are available to consumers of this record.
        record.event = record.event.model_copy(
            update={
                "affected_edge_ids": list(edge_ids),
                "affected_mapping_edge_ids": list(mapping_edge_ids),
                "affected_endpoint_ids": list(affected_endpoints),
            }
        )
        return record

    # -- steps 5-8: collect evidence, validate, promote --------------------- #
    def observe_confirming_event(
        self,
        change_event_id: str,
        *,
        source: str | None = None,
        candidate_topology_edges: Sequence[Any] | None = None,
        candidate_mapping_edges: Sequence[Any] | None = None,
    ) -> ConfigurationRecord:
        """Add one independent corroboration; promote at the threshold.

        Evidence comes from T5 (topology) and M2 match history (mapping); the
        workflow itself does not recompute it.  A candidate edge set is proposed
        once when the first corroboration arrives, then promoted when
        ``confirmed_events`` independent sources agree.
        """
        record = self._records[change_event_id]
        if record.status != "suspected":
            return record
        record.corroborations += 1
        token = source or f"event-{record.corroborations}"
        record.independent_sources.add(token)
        if record.candidate_topology_version_id is None:
            edges = candidate_topology_edges
            if edges is None:
                edges = self._current_topology_edges()
            record.candidate_topology_version_id = self.versions.propose_topology(
                list(edges), change_event_id
            )
        if record.candidate_mapping_version_id is None:
            edges = candidate_mapping_edges
            if edges is None:
                edges = self._current_mapping_edges()
            record.candidate_mapping_version_id = self.versions.propose_mapping(
                list(edges), change_event_id
            )
        if len(record.independent_sources) < self.confirmed_events:
            return record

        model_before = self.versions.current().model_set_id
        reason = f"HB14: {record.kind} corroborated by {len(record.independent_sources)} events"
        if record.candidate_topology_version_id is not None:
            self.versions.promote_topology(record.candidate_topology_version_id, reason)
        if record.candidate_mapping_version_id is not None:
            self.versions.promote_mapping(record.candidate_mapping_version_id, reason)
        # Rule 20 / AT-15: promoting topology or mapping never changes the model.
        assert self.versions.current().model_set_id == model_before, (
            "the self-healing workflow must never change the power-model version"
        )
        record.status = "confirmed"
        record.resolved_at = self.clock()
        self._emit_promotion(record, reason)
        return record

    def _emit_promotion(self, record: ConfigurationRecord, reason: str) -> None:
        """HB §14 step 9/3: enqueue reconciliation and write an audit entry.

        L7 owns the actual historical replay; T7 only *requests* it for the
        affected time range and subjects and records the request append-only.
        """
        versions = self.versions.current()
        payload = {
            "event_id": record.event_id,
            "kind": str(getattr(record.kind, "value", record.kind)),
            "reason": reason,
            "at": self.clock().isoformat(),
            "affected_endpoint_ids": list(record.affected_endpoints),
            "affected_edge_ids": list(record.edge_ids),
            "affected_mapping_edge_ids": list(record.mapping_edge_ids),
            "versions": {
                "topology_version_id": versions.topology_version_id,
                "mapping_version_id": versions.mapping_version_id,
                "model_set_id": versions.model_set_id,
            },
        }
        if self.reconcile is not None:
            self.reconcile(payload)
        if self.audit is not None:
            self.audit({**payload, "action": "configuration_change_promoted"})

    # -- step 8/10: reject an uncorroborated suspicion ---------------------- #
    def reject_expired(self, change_event_id: str) -> ConfigurationRecord:
        """Reject after the timeout, restoring affected lifecycles (HB §14)."""
        record = self._records[change_event_id]
        if record.status != "suspected":
            return record
        if self.clock() - record.created_at < self.reject_after:
            return record
        # Restoring lifecycles is an append-only act: D1's rollback never
        # reactivates an old row in place.  A mapping suspicion rolls back the
        # mapping lifecycle; a topology-only suspicion keeps its edges as they
        # were, with the SUSPECTED marking superseded by a REJECTED event.
        if record.candidate_mapping_version_id is None and record.mapping_edge_ids:
            # A backend with no historical mapping row has nothing to restore;
            # the rejection is still recorded append-only.
            with contextlib.suppress(KeyError, ValueError, NotImplementedError):
                self.versions.rollback(
                    "mapping", self.versions.current().mapping_version_id
                )
        record.status = "rejected"
        record.resolved_at = self.clock()
        if self.audit is not None:
            self.audit(
                {
                    "action": "configuration_change_rejected",
                    "event_id": record.event_id,
                    "reason": "no corroborating evidence before timeout",
                    "at": record.resolved_at.isoformat(),
                }
            )
        return record

    # -- reads -------------------------------------------------------------- #
    def _current_topology_edges(self) -> list[Any]:
        """A default candidate: the current topology view's edges."""
        try:
            version = self.versions.current().topology_version_id
            view = self.versions.topology_view(version)
            edges = getattr(view, "edges", None)
            return list(edges()) if callable(edges) else []
        except (KeyError, ValueError, TypeError):
            return []

    def _current_mapping_edges(self) -> list[Any]:
        """A default candidate: the current mapping view's edges."""
        try:
            version = self.versions.current().mapping_version_id
            view = self.versions.mapping_view(version)
            edges = getattr(view, "edges", None)
            return list(edges()) if callable(edges) else []
        except (KeyError, ValueError, TypeError):
            return []

    def record_for(self, change_event_id: str) -> ConfigurationRecord | None:
        return self._records.get(change_event_id)

    @property
    def records(self) -> tuple[ConfigurationRecord, ...]:
        return tuple(self._records.values())
