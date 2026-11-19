r"""Event group formation and late-arrival revision (P5).

Stage 6 of the real-time pipeline.  This module assembles
:class:`~wattmosaic_engine.contracts.EventGroup` objects from three kinds of
sub-event:

* reader steps (:class:`~wattmosaic_engine.contracts.MeasurementEvent`, via their
  immutable P4 :class:`~wattmosaic_engine.contracts.EventEvidence`);
* HA events (:class:`~wattmosaic_engine.contracts.HAEvent`), including ring-held
  unlinked events (DEC-17 — persisted only once their group becomes evidence);
* smart-plug steps (a reader step carrying ``PLUG_STEP`` provenance).

Each sub-event keeps its **own** timestamp; two transitions of one entity are
two sub-events (the ``_collect_transitions`` collapse is gone, C5/C6).

Grouping is union-find (design §4).  Two sub-events join a group when any of
these holds:

1. they share a ``context_id`` or one's ``context_parent_id`` equals the
   other's ``context_id``, **and** their onsets are within
   ``context_max_spread_s`` (context is not proof of simultaneity, HB §6.6);
2. their onset intervals overlap after widening by ``tolerance_s``;
3. two reader steps have the same sign, their amplitudes fit a parent against
   children on a CONFIRMED or PROVISIONAL CONTAINS edge (co-movement).

Lifecycle and timers are driven by the injected clock (no wall clock, no DB
inside the pure helpers).  ``on_ready(group)`` fires at MATCHING and
``on_revised(group)`` fires when a late sub-event creates a revision after
PROVISIONAL/CONFIRMED.  Every revision (including the first) is persisted
through S16 ``put_group_revision``; when a group first becomes evidence the
ring-held unlinked HA events it references are persisted too (DEC-17).

Production code never imports :mod:`wattmosaic_engine.runtime.fakes`.  The
:class:`TopologyView` used for co-movement is the only topology dependency;
tests supply the K08 fake, production supplies T1's ``TopologyGraph``.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..contracts import (
    EdgeLifecycleV3,
    EventEvidence,
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    HAEvent,
    MeasurementEvent,
    ReaderDelta,
    SubEvent,
    SubEventKind,
    VersionTriple,
)
from ..runtime.interfaces import TopologyView
from .candidate_ring import CandidateRing

__all__ = [
    "CONFIRMED_LIFECYCLES",
    "DEFAULT_CONTEXT_MAX_SPREAD_S",
    "DEFAULT_QUIET_S",
    "DEFAULT_TOLERANCE_S",
    "GroupConfig",
    "GroupPersistence",
    "GroupSink",
    "GroupingEngine",
    "build_ha_sub_event",
    "build_reader_sub_event",
    "co_movement_reason",
    "context_reason",
    "form_groups",
    "make_group_id",
    "temporal_reason",
]

# Only these edge lifecycles may supply topology evidence for co-movement
# (design §4: CONFIRMED structure, PROVISIONAL soft alternatives).
CONFIRMED_LIFECYCLES = (EdgeLifecycleV3.CONFIRMED, EdgeLifecycleV3.PROVISIONAL)

DEFAULT_CONTEXT_MAX_SPREAD_S = 60.0
DEFAULT_TOLERANCE_S = 5.0
DEFAULT_QUIET_S = 60.0

GroupSink = Callable[[EventGroup], None]
GroupPersistence = Callable[[EventGroup], None]
HAPersistence = Callable[[HAEvent], None]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _seconds(value: float, name: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{name} must be a finite, non-negative number of seconds")
    return seconds


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GroupConfig:
    """All tunables of group formation and lifecycle (design §4).

    ``context_max_spread_s`` is DEC-15/HB §6.6's 60 s bound: sharing a context
    beyond it is *not* enough to group.  ``tolerance_s`` widens onset intervals
    (cadence-aware; P6 may raise it for slow readers).  ``quiet_s`` is the quiet
    period that lets a fully-explained group reach CONFIRMED.
    """

    context_max_spread_s: float = DEFAULT_CONTEXT_MAX_SPREAD_S
    tolerance_s: float = DEFAULT_TOLERANCE_S
    quiet_s: float = DEFAULT_QUIET_S

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_max_spread_s", _seconds(self.context_max_spread_s, "context_max_spread_s"))
        object.__setattr__(self, "tolerance_s", _seconds(self.tolerance_s, "tolerance_s"))
        object.__setattr__(self, "quiet_s", _seconds(self.quiet_s, "quiet_s"))


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def make_group_id(sub_event_ids: Iterable[str]) -> str:
    """Stable group id: hash of the sorted member sub-event ids (design §4)."""
    canonical = "\0".join(sorted(str(item) for item in sub_event_ids))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"grp-{digest[:16]}"


def build_ha_sub_event(event: HAEvent) -> SubEvent:
    """One HA change -> one sub-event at the event's own ``observed_at``."""
    kind = SubEventKind.HA_ATTRIBUTE if event.attributes_delta else SubEventKind.HA_STATE
    return SubEvent(
        sub_event_id=f"sub-ha-{event.event_id}",
        kind=kind,
        ref=EvidenceRef(kind="ha_event", id=event.event_id),
        observed_at=_aware(event.observed_at),
        timestamp_uncertainty_s=0.0,
    )


def build_reader_sub_event(step: MeasurementEvent, evidence_id: str) -> SubEvent:
    """One reader step -> one sub-event referencing its immutable P4 evidence."""
    uncertainty = max(
        0.0,
        (_aware(step.onset_hi) - _aware(step.onset_lo)).total_seconds(),
    )
    return SubEvent(
        sub_event_id=f"sub-evd-{evidence_id}",
        kind=SubEventKind.READER_STEP,
        ref=EvidenceRef(kind="event_evidence", id=evidence_id),
        observed_at=_aware(step.onset_lo),
        timestamp_uncertainty_s=uncertainty,
    )


def context_reason(a: HAEvent, b: HAEvent, max_spread_s: float = DEFAULT_CONTEXT_MAX_SPREAD_S) -> str | None:
    """``"context_id"``/``"context_parent"`` when two HA events share lineage
    *and* are close enough in time; ``None`` otherwise (HB §6.6)."""
    if a.context_id is not None and a.context_id == b.context_id:
        relation = "context_id"
    elif (
        a.context_parent_id is not None
        and a.context_parent_id == b.context_id
        or b.context_parent_id is not None
        and b.context_parent_id == a.context_id
    ):
        relation = "context_parent"
    else:
        return None
    spread = abs((_aware(a.observed_at) - _aware(b.observed_at)).total_seconds())
    # Strict bound: two changes exactly ``max_spread_s`` apart are *not*
    # grouped on context alone (the decoy in the task's acceptance bullets).
    return relation if spread < _seconds(max_spread_s, "max_spread_s") else None


def temporal_reason(
    a: MeasurementEvent,
    b: MeasurementEvent,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> str | None:
    """``"temporal_overlap"`` when two widened onset intervals overlap."""
    tol = timedelta(seconds=_seconds(tolerance_s, "tolerance_s"))
    a_lo, a_hi = _aware(a.onset_lo) - tol, _aware(a.onset_hi) + tol
    b_lo, b_hi = _aware(b.onset_lo) - tol, _aware(b.onset_hi) + tol
    return "temporal_overlap" if a_lo <= b_hi and b_lo <= a_hi else None


def ha_temporal_reason(
    event: HAEvent,
    other: HAEvent | MeasurementEvent,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> str | None:
    """``"temporal_overlap"`` when an HA change and another sub-event overlap.

    Design §4 rule 2 applies to *any* two sub-events, not only to two reader
    steps: an HA change is a point interval at its ``observed_at`` and a reader
    step is its ``[onset_lo, onset_hi]``; both are widened by ``tolerance_s``.
    This is what puts the HA command and the reader steps that confirm it in
    one event group (HB §6.6), so candidate generation sees both (HB §17 step 4).
    """
    tol = timedelta(seconds=_seconds(tolerance_s, "tolerance_s"))
    at = _aware(event.observed_at)
    a_lo, a_hi = at - tol, at + tol
    if isinstance(other, HAEvent):
        b_at = _aware(other.observed_at)
        b_lo, b_hi = b_at - tol, b_at + tol
    else:
        b_lo, b_hi = _aware(other.onset_lo) - tol, _aware(other.onset_hi) + tol
    return "temporal_overlap" if a_lo <= b_hi and b_lo <= a_hi else None


def _related(reader_a: str, reader_b: str, topology: TopologyView) -> bool:
    """True when two readers are joined by a CONFIRMED/PROVISIONAL CONTAINS edge.

    A reader is related to any node on its containment ancestry (e.g. ``Main``
    contains ``Subpanel`` which contains ``Circuit A``), so an ancestor observing
    a child's step is topology evidence even across intermediate parents.
    """
    contains = {
        (edge.source_endpoint_id, edge.target_endpoint_id)
        for edge in topology.edges(CONFIRMED_LIFECYCLES)
        if getattr(edge.relation, "value", edge.relation) == "contains"
    }
    if (reader_a, reader_b) in contains or (reader_b, reader_a) in contains:
        return True
    # Ancestor via a chain of CONTAINS edges (either direction).
    for ancestor, descendant in (reader_a, reader_b), (reader_b, reader_a):
        seen = {ancestor}
        frontier = [ancestor]
        while frontier:
            current = frontier.pop()
            for parent, child in contains:
                if child == current and parent not in seen:
                    if parent == descendant:
                        return True
                    seen.add(parent)
                    frontier.append(parent)
    return False


def co_movement_reason(
    parent: MeasurementEvent,
    children: Sequence[MeasurementEvent],
    topology: TopologyView,
) -> str | None:
    """``"reader_comovement"`` when a parent step is explained by same-sign
    child steps on related readers (design §4, rule 3)."""
    if not children:
        return None
    parent_delta = parent.delta_w
    if parent_delta is None:
        return None
    child_sum = sum(child.delta_w or 0.0 for child in children)
    if parent_delta == 0.0 and child_sum == 0.0:
        return None
    if parent_delta * child_sum <= 0:  # different signs
        return None
    related = [child for child in children if _related(parent.reader_id, child.reader_id, topology)]
    if not related:
        return None
    # Equal amplitude OR parent ≈ sum of children (co-movement budget).
    if math.isclose(parent_delta, child_sum, rel_tol=0.2, abs_tol=1e-9):
        return "reader_comovement"
    if any(math.isclose(parent_delta, child.delta_w or 0.0, rel_tol=0.2, abs_tol=1e-9) for child in related):
        return "reader_comovement"
    return None


class _UnionFind:
    """Tiny union-find over sub-event indices with collected reasons."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.reasons: dict[int, set[str]] = {i: set() for i in range(size)}

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int, reason: str) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri == rj:
            self.reasons[ri].add(reason)
            return
        if ri > rj:
            ri, rj = rj, ri
        self.parent[rj] = ri
        self.reasons[ri] |= self.reasons.pop(rj, set())
        self.reasons[ri].add(reason)


@dataclass(frozen=True, slots=True)
class _Step:
    """A reader step paired with the immutable evidence id P4 assigned it."""

    event: MeasurementEvent
    evidence_id: str

    @property
    def reader_id(self) -> str:
        return self.event.reader_id


def _node_onsets(sub: SubEvent | _Step) -> tuple[datetime, datetime]:
    if isinstance(sub, _Step):
        return _aware(sub.event.onset_lo), _aware(sub.event.onset_hi)
    return sub.observed_at, sub.observed_at + timedelta(seconds=sub.timestamp_uncertainty_s)


def form_groups(
    *,
    ha_events: Sequence[HAEvent] = (),
    reader_steps: Sequence[_Step | MeasurementEvent] = (),
    topology: TopologyView | None = None,
    config: GroupConfig | None = None,
    versions: VersionTriple,
) -> list[EventGroup]:
    """Pure union-find grouping over a set of sub-events.

    Returns one :class:`EventGroup` per connected component with at least one
    sub-event.  The caller may pass :class:`_Step` values (to bind P4 evidence
    ids) or bare :class:`MeasurementEvent` values (evidence id derived from the
    event id).  Every sub-event keeps its own timestamp.
    """
    config = config or GroupConfig()
    steps = [
        step if isinstance(step, _Step) else _Step(step, f"evd-{step.event_id}")
        for step in reader_steps
    ]
    # A single reader step is a legitimate group (fridge-only, AT bullet 5).
    nodes: list[tuple[str, SubEvent, HAEvent | None, _Step | None]] = []
    for event in ha_events:
        nodes.append(("ha", build_ha_sub_event(event), event, None))
    for step in steps:
        nodes.append(("reader", build_reader_sub_event(step.event, step.evidence_id), None, step))

    if not nodes:
        return []

    uf = _UnionFind(len(nodes))
    # HA <-> HA: context lineage (and temporal proximity).
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            a_kind, _, a_ev, a_step = nodes[i]
            b_kind, _, b_ev, b_step = nodes[j]
            reason: str | None = None
            if a_kind == "ha" and b_kind == "ha" and a_ev is not None and b_ev is not None:
                reason = context_reason(a_ev, b_ev, config.context_max_spread_s)
                if reason is None:
                    reason = ha_temporal_reason(a_ev, b_ev, config.tolerance_s)
            elif a_kind == "reader" and b_kind == "reader" and a_step and b_step:
                reason = temporal_reason(a_step.event, b_step.event, config.tolerance_s)
            elif a_ev is not None and b_step is not None:
                reason = ha_temporal_reason(a_ev, b_step.event, config.tolerance_s)
            elif b_ev is not None and a_step is not None:
                reason = ha_temporal_reason(b_ev, a_step.event, config.tolerance_s)
            if reason is not None:
                uf.union(i, j, reason)

    # Reader co-movement: a parent step against the children it explains.
    if topology is not None:
        reader_indices = [i for i, n in enumerate(nodes) if n[3] is not None]
        for i in reader_indices:
            parent = nodes[i][3]  # type: ignore[index]
            for j in reader_indices:
                if i == j:
                    continue
                child = nodes[j][3]  # type: ignore[index]
                if parent is None or child is None:
                    continue
                reason = co_movement_reason(parent.event, [child.event], topology)
                if reason is not None:
                    uf.union(i, j, reason)
        # Also group a parent with a *set* of its children whose sum explains it.
        reader_steps_only = [nodes[i][3] for i in reader_indices]
        for i in reader_indices:
            parent = nodes[i][3]  # type: ignore[index]
            if parent is None:
                continue
            others = [s for s in reader_steps_only if s is not parent]
            if co_movement_reason(parent.event, [s.event for s in others], topology) is not None:
                for child in others:
                    uf.union(
                        i,
                        reader_indices[reader_steps_only.index(child)],
                        "reader_comovement",
                    )

    components: dict[int, list[int]] = {}
    for i in range(len(nodes)):
        components.setdefault(uf.find(i), []).append(i)

    groups: list[EventGroup] = []
    for indices in components.values():
        if not indices:
            continue
        root = uf.find(indices[0])
        sub_events = [nodes[i][1] for i in indices]
        sub_events.sort(key=lambda s: (s.observed_at, s.sub_event_id))
        group_id = make_group_id(sub.sub_event_id for sub in sub_events)
        opened = min(sub.observed_at for sub in sub_events)
        reasons = sorted(uf.reasons.get(root, set()))
        deltas: list[ReaderDelta] = []
        for i in indices:
            step = nodes[i][3]
            if step is None:
                continue
            delta = step.event.delta_w
            # A missing measurement stays None, never 0.0 (HB §18).
            completeness = "complete" if delta is not None else "missing"
            deltas.append(
                ReaderDelta(
                    reader_id=step.reader_id,
                    delta_w=delta,
                    sigma_w=step.event.sigma_w,
                    completeness=completeness,  # type: ignore[arg-type]
                    evidence_ref=EvidenceRef(kind="event_evidence", id=step.evidence_id),
                )
            )
        groups.append(
            EventGroup(
                group_id=group_id,
                revision=1,
                status=EventGroupStatus.DETECTED,
                opened_at=opened,
                sub_events=sub_events,
                reader_deltas=deltas,
                grouping_reasons=reasons,
                versions=versions,
            )
        )
    groups.sort(key=lambda g: (g.opened_at, g.group_id))
    return groups


# --------------------------------------------------------------------------- #
# Stateful engine (clock injected by the caller; storage injected too)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Entry:
    """One live or recently-closed group plus the inputs that formed it."""

    group: EventGroup
    ha_events: list[HAEvent]
    steps: list[_Step]
    last_activity: datetime
    terminal: bool = False


class GroupingEngine:
    """Drive the HB §12.3 lifecycle from injected events and clock.

    Parameters
    ----------
    versions:
        The ``VersionTriple`` stamped on every revision.
    repository:
        Anything exposing S16 ``put_group_revision`` (the real
        ``EvidenceRepository`` or a test double).  Every revision — including
        the first — is written here, append-only.
    topology:
        A :class:`TopologyView` for co-movement (K08 fake in tests, T1 in
        production).
    ring:
        Optional P1 candidate ring.  Ring-held *unlinked* HA events that join a
        group are persisted when that group first becomes evidence (DEC-17).
    persist_ha_event:
        Optional sink for those unlinked events (e.g. S16 ``put_ha_event``).
    on_ready / on_revised:
        Callbacks; ``on_ready`` fires once at MATCHING, ``on_revised`` fires with
        each REVISED group after PROVISIONAL/CONFIRMED.
    config:
        Grouping tunables (spread / tolerance / quiet period).
    """

    def __init__(
        self,
        *,
        versions: VersionTriple,
        repository: Any,
        topology: TopologyView | None = None,
        ring: CandidateRing | None = None,
        persist_ha_event: HAPersistence | None = None,
        on_ready: GroupSink | None = None,
        on_revised: GroupSink | None = None,
        config: GroupConfig | None = None,
        put_group_revision: GroupPersistence | None = None,
    ) -> None:
        self.versions = versions
        self.repository = repository
        self._put_group = put_group_revision or repository.put_group_revision
        self.topology = topology
        self.ring = ring
        self.persist_ha_event = persist_ha_event
        self.on_ready = on_ready
        self.on_revised = on_revised
        self.config = config or GroupConfig()
        self._entries: dict[str, _Entry] = {}
        self._closed: dict[str, _Entry] = {}
        # Append-only: a group revision is written to S16 exactly once.  Status
        # transitions mutate the in-memory group in place (same revision), so
        # they never re-insert a row (which would violate the PK).  A REVISED
        # group has revision+1 and is therefore persisted as a new row.
        self._persisted: set[tuple[str, int]] = set()
        self._lifecycle: dict[tuple[str, int], tuple[str, datetime | None]] = {}
        self._update_lifecycle = getattr(repository, "update_group_lifecycle", None)

    # -- introspection ----------------------------------------------------- #
    def groups(self, status: EventGroupStatus | None = None) -> list[EventGroup]:
        """All known live groups (and re-opened revisions), newest last."""
        result = [entry.group for entry in self._entries.values()]
        if status is not None:
            result = [group for group in result if group.status == status]
        return sorted(result, key=lambda g: (g.opened_at, g.group_id))

    def current(self, group_id: str) -> EventGroup | None:
        entry = self._entries.get(group_id)
        return None if entry is None else entry.group

    def ha_events(self, group_id: str) -> tuple[HAEvent, ...]:
        """The HA events behind a live group's HA sub-events (read-only).

        A sub-event only references its HA event by id; the event loop needs the
        entity id and instant to resolve ``ha_entity_links`` (DEC-08).
        """
        entry = self._entries.get(group_id)
        return () if entry is None else tuple(entry.ha_events)

    def _persist(self, group: EventGroup) -> None:
        key = (group.group_id, group.revision)
        lifecycle = (str(getattr(group.status, "value", group.status)), group.closed_at)
        if key in self._persisted:
            # Same revision, new lifecycle state (MATCHING -> PROVISIONAL ->
            # CONFIRMED with closed_at): record the transition, never re-insert.
            if self._lifecycle.get(key) != lifecycle and self._update_lifecycle is not None:
                self._update_lifecycle(group)
                self._lifecycle[key] = lifecycle
            return
        self._put_group(group)
        self._persisted.add(key)
        self._lifecycle[key] = lifecycle
        # DEC-17: when a group first becomes evidence, persist the ring-held
        # unlinked HA events it references (linked events are persisted by P1).
        if self.ring is not None and self.persist_ha_event is not None:
            referenced = {sub.ref.id for sub in group.sub_events if sub.ref.kind == "ha_event"}
            for event in self.ring:
                if not event.linked and event.event_id in referenced:
                    self.persist_ha_event(event)

    def _merge(self, group: EventGroup, *, now: datetime) -> _Entry:
        """Insert or merge a freshly-formed group into the live set."""
        now = _aware(now)
        existing = self._entries.get(group.group_id)
        if existing is None:
            entry = _Entry(
                group=group,
                ha_events=[],
                steps=[],
                last_activity=now,
            )
            self._entries[group.group_id] = entry
            existing = entry
        else:
            prior = existing.group
            seen = {sub.sub_event_id for sub in prior.sub_events}
            merged_sub_events = [*prior.sub_events]
            for sub in group.sub_events:
                if sub.sub_event_id not in seen:
                    merged_sub_events.append(sub)
                    seen.add(sub.sub_event_id)
            merged_sub_events.sort(key=lambda s: (s.observed_at, s.sub_event_id))
            existing.group = prior.model_copy(update={"sub_events": merged_sub_events})
        self._update_deltas(existing)
        return existing

    def _update_deltas(self, entry: _Entry) -> None:
        deltas: list[ReaderDelta] = []
        for step in entry.steps:
            delta = step.event.delta_w
            deltas.append(
                ReaderDelta(
                    reader_id=step.reader_id,
                    delta_w=delta,
                    sigma_w=step.event.sigma_w,
                    completeness="complete" if delta is not None else "missing",  # type: ignore[arg-type]
                    evidence_ref=EvidenceRef(kind="event_evidence", id=step.evidence_id),
                )
            )
        entry.group = entry.group.model_copy(update={"reader_deltas": deltas})

    # -- intake ------------------------------------------------------------ #
    def on_ha_event(
        self,
        event: HAEvent,
        *,
        now: datetime,
        context_parent_id: str | None = None,
    ) -> EventGroup:
        """Add one HA event, grouping by context / overlap with live groups."""
        now = _aware(now)
        if context_parent_id is not None and event.context_parent_id is None:
            event = event.model_copy(update={"context_parent_id": context_parent_id})
        sub = build_ha_sub_event(event)

        # Attach to an existing live group by context (or an open, close group).
        target: _Entry | None = None
        reason: str | None = None
        for entry in self._entries.values():
            for other in entry.ha_events:
                reason = context_reason(event, other, self.config.context_max_spread_s)
                if reason is not None:
                    target = entry
                    break
            if target is not None:
                break
        if target is None:
            # Design §4 rule 2: an HA change whose instant overlaps a live
            # group's sub-event (another HA change or a reader step that was
            # delivered first) joins that group on temporal overlap.
            target, reason = self._temporal_target(event)
        if target is not None:
            target.ha_events.append(event)
            existing_ids = {s.sub_event_id for s in target.group.sub_events}
            if sub.sub_event_id not in existing_ids:
                target.group = target.group.model_copy(
                    update={"sub_events": [*target.group.sub_events, sub]}
                )
            if reason is not None:
                target.group = target.group.model_copy(
                    update={"grouping_reasons": sorted(set(target.group.grouping_reasons) | {reason})}
                )
            target.last_activity = now
            self._refresh_ready(target)
            return target.group

        group = form_groups(
            ha_events=[event],
            reader_steps=[],
            topology=self.topology,
            config=self.config,
            versions=self.versions,
        )[0]
        entry = self._merge(group, now=now)
        entry.ha_events.append(event)
        self._persist(entry.group)
        return entry.group

    def on_reader_step(
        self,
        step: MeasurementEvent,
        *,
        now: datetime,
        evidence_id: str | None = None,
        context_parent_id: str | None = None,
    ) -> EventGroup:
        """Add one reader step, joining an open group by overlap / co-movement."""
        now = _aware(now)
        wrapped = _Step(step, evidence_id or f"evd-{step.event_id}")

        # Prefer a live group whose onset overlaps, or whose reader is
        # topology-related *and* co-temporal (a related reader's step an hour
        # later is a different physical event, not the same one seen twice);
        # then a group whose HA change overlaps this step (design §4 rule 2);
        # otherwise make a fresh (reader-only) group.
        target: _Entry | None = None
        ha_reason: str | None = None
        related_window = timedelta(seconds=self.config.context_max_spread_s)
        for entry in self._entries.values():
            if not self._joinable(entry):
                continue
            for other in entry.steps:
                overlaps = temporal_reason(step, other.event, self.config.tolerance_s) is not None
                related = (
                    self.topology is not None
                    and abs(_aware(step.onset_lo) - _aware(other.event.onset_lo)) <= related_window
                    and _related(step.reader_id, other.reader_id, self.topology)
                )
                if related or overlaps:
                    target = entry
                    break
            if target is not None:
                break
        if target is None:
            for entry in self._entries.values():
                # An HA-only group closed UNRESOLVED (no watts yet) is re-opened
                # by the reader step that confirms it: P3 confirms a step only
                # after ``confirm_s`` plus a few samples, which can exceed the
                # HA group's ``quiet_s`` (design §4 late data).
                reopenable = (
                    entry.group.status == EventGroupStatus.UNRESOLVED and not entry.steps
                )
                if not (self._joinable(entry) or reopenable):
                    continue
                for event in entry.ha_events:
                    ha_reason = ha_temporal_reason(event, step, self.config.tolerance_s)
                    if ha_reason is not None:
                        target = entry
                        break
                if target is not None:
                    break
        if target is None:
            group = form_groups(
                reader_steps=[wrapped],
                topology=self.topology,
                config=self.config,
                versions=self.versions,
            )[0]
            entry = self._merge(group, now=now)
        else:
            entry = target
            if entry.group.status == EventGroupStatus.UNRESOLVED:
                entry.group = entry.group.model_copy(
                    update={"status": EventGroupStatus.DETECTED, "closed_at": None}
                )
            existing_ids = {s.sub_event_id for s in entry.group.sub_events}
            new_sub = build_reader_sub_event(step, wrapped.evidence_id)
            if new_sub.sub_event_id not in existing_ids:
                entry.group = entry.group.model_copy(
                    update={"sub_events": [*entry.group.sub_events, new_sub]}
                )
            reasons = set(entry.group.grouping_reasons)
            if ha_reason is not None:
                reasons.add(ha_reason)
            if self.topology is not None:
                for other in entry.steps:
                    reason = co_movement_reason(step, [other.event], self.topology)
                    if reason is not None:
                        reasons.add(reason)
            entry.group = entry.group.model_copy(update={"grouping_reasons": sorted(reasons)})
            entry.last_activity = now
        entry.steps.append(wrapped)
        self._update_deltas(entry)

        # Transition DETECTED -> MATCHING when the group has reader evidence.
        if entry.group.status == EventGroupStatus.DETECTED and entry.group.reader_deltas:
            entry.group = entry.group.model_copy(update={"status": EventGroupStatus.MATCHING})
            self._persist(entry.group)
            if self.on_ready is not None:
                self.on_ready(entry.group)
        elif target is not None:
            self._refresh_ready(entry)

        # A fully-explained group with all windows closed and quiet becomes
        # CONFIRMED (DEC-14/§12.3).  HA-only groups stay DETECTED.
        if (
            entry.group.status == EventGroupStatus.MATCHING
            and self._all_closed(entry, now)
            and (now - entry.last_activity) >= timedelta(seconds=self.config.quiet_s)
        ):
            entry.group = entry.group.model_copy(
                update={"status": EventGroupStatus.CONFIRMED, "closed_at": now}
            )
            self._persist(entry.group)
        return entry.group

    def _all_closed(self, entry: _Entry, now: datetime) -> bool:
        """True when no sub-event's uncertainty interval is still open."""
        for sub in entry.group.sub_events:
            end = sub.observed_at + timedelta(seconds=sub.timestamp_uncertainty_s)
            if end > now:
                return False
        return True

    @staticmethod
    def _joinable(entry: _Entry) -> bool:
        """A fresh sub-event may join only a group that is still open.

        CONFIRMED/UNRESOLVED groups are closed: a late sample reaches them only
        through :meth:`on_late_reader_step` (a new revision, AT-18), never by
        silently growing the closed revision in place.
        """
        return not entry.terminal and entry.group.status in (
            EventGroupStatus.DETECTED,
            EventGroupStatus.MATCHING,
            EventGroupStatus.PROVISIONAL,
            EventGroupStatus.REVISED,
        )

    def _temporal_target(self, event: HAEvent) -> tuple[_Entry | None, str | None]:
        """The first open group with a sub-event overlapping ``event`` in time."""
        tolerance = self.config.tolerance_s
        for entry in self._entries.values():
            if not self._joinable(entry):
                continue
            for other in [*entry.ha_events, *(step.event for step in entry.steps)]:
                reason = ha_temporal_reason(event, other, tolerance)
                if reason is not None:
                    return entry, reason
        return None, None

    def _refresh_ready(self, entry: _Entry) -> None:
        """Re-deliver a group already handed to inference after it grew.

        ``on_ready`` hands the event loop the group as it was at its first
        reader step.  Nested readers (Circuit A, Subpanel, Main — HB §17 step
        3) and the HA change confirming them arrive one by one; each one that
        joins a MATCHING/PROVISIONAL group must reach inference too, or the
        published result keeps only the first reader's delta (HB §7.6:
        matching is not a one-time assignment).  Same revision: nothing
        immutable is edited.
        """
        if self.on_ready is None:
            return
        if entry.group.status in (EventGroupStatus.MATCHING, EventGroupStatus.PROVISIONAL):
            self.on_ready(entry.group)

    def on_provisional(self, evidence: EventEvidence) -> EventGroup | None:
        """P4 hook: a reader's first post-sample makes its group PROVISIONAL."""
        for entry in self._entries.values():
            if any(step.evidence_id == evidence.evidence_id for step in entry.steps):
                if entry.group.status in (
                    EventGroupStatus.DETECTED,
                    EventGroupStatus.MATCHING,
                ):
                    entry.group = entry.group.model_copy(
                        update={"status": EventGroupStatus.PROVISIONAL}
                    )
                    self._persist(entry.group)
                return entry.group
        return None

    # -- late data / revision ---------------------------------------------- #
    def on_late_reader_step(
        self,
        step: MeasurementEvent,
        *,
        now: datetime,
        target_group_id: str | None = None,
        evidence_id: str | None = None,
    ) -> EventGroup | None:
        """A late reader sample attaches to one (closed) group as a revision.

        Only the target group is touched (AT-18): unrelated concurrent groups
        keep their identity, revision and on-disk bytes.  A prior revision is
        never edited — a new ``revision+1`` row is appended via S16.
        """
        now = _aware(now)
        entry = self._entries.get(target_group_id) if target_group_id else None
        if entry is None:
            # Find the nearest open window whose onset bounds contain this step.
            onset = _aware(step.onset_lo)
            candidates = [
                e
                for e in self._entries.values()
                if e.group.status
                in (EventGroupStatus.PROVISIONAL, EventGroupStatus.CONFIRMED, EventGroupStatus.MATCHING)
                and any(
                    _aware(s.event.onset_lo) <= onset + self.config.tolerance_s * 2
                    for s in e.steps
                )
            ]
            if not candidates:
                return None
            entry = sorted(
                candidates,
                key=lambda e: min(
                    (abs((_aware(s.event.onset_lo) - onset).total_seconds()) for s in e.steps),
                    default=float("inf"),
                ),
            )[0]

        return self._revise(entry, step, now=now, evidence_id=evidence_id)

    def _revise(
        self,
        entry: _Entry,
        step: MeasurementEvent,
        *,
        now: datetime,
        evidence_id: str | None = None,
    ) -> EventGroup:
        prior = entry.group
        if prior.status not in (
            EventGroupStatus.MATCHING,
            EventGroupStatus.PROVISIONAL,
            EventGroupStatus.CONFIRMED,
            EventGroupStatus.REVISED,
        ):
            # A DETECTED/UNRESOLVED group has no interpretation to revise; the
            # late sub-event simply joins it as a new sub-event.
            wrapped = _Step(step, evidence_id or f"evd-{step.event_id}")
            entry.steps.append(wrapped)
            entry.group = prior.model_copy(
                update={"sub_events": [*prior.sub_events, build_reader_sub_event(step, wrapped.evidence_id)]}
            )
            self._update_deltas(entry)
            self._persist(entry.group)
            return entry.group

        wrapped = _Step(step, evidence_id or f"evd-{step.event_id}")
        new_sub = build_reader_sub_event(step, wrapped.evidence_id)
        revised = prior.model_copy(
            update={
                "revision": prior.revision + 1,
                "status": EventGroupStatus.REVISED,
                "sub_events": [*prior.sub_events, new_sub],
                "closed_at": None,
                "grouping_reasons": sorted(set(prior.grouping_reasons) | {"late_data"}),
            }
        )
        entry.steps.append(wrapped)
        entry.group = revised
        self._update_deltas(entry)
        self._persist(revised)
        if self.on_revised is not None:
            self.on_revised(revised)
        return revised

    # -- clock-driven lifecycle -------------------------------------------- #
    def tick(self, now: datetime) -> list[EventGroup]:
        """Advance the injected clock: close quiet windows, resolve unknowns."""
        now = _aware(now)
        changed: list[EventGroup] = []
        for entry in list(self._entries.values()):
            group = entry.group
            quiet_enough = (now - entry.last_activity) >= timedelta(seconds=self.config.quiet_s)
            if group.status == EventGroupStatus.MATCHING and self._all_closed(entry, now) and quiet_enough:
                entry.group = group.model_copy(
                    update={"status": EventGroupStatus.CONFIRMED, "closed_at": now}
                )
                self._persist(entry.group)
                changed.append(entry.group)
            elif group.status in (EventGroupStatus.DETECTED, EventGroupStatus.PROVISIONAL) and quiet_enough:
                # No reader evidence ever arrived (or the unknown alternative
                # won): there are no watts to publish, so the group is UNRESOLVED.
                entry.group = group.model_copy(
                    update={"status": EventGroupStatus.UNRESOLVED, "closed_at": now}
                )
                self._persist(entry.group)
                changed.append(entry.group)
        return changed
