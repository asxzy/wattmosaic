"""Topology facade: read/replace/confirm, nesting candidates, typed relations.

Owned by T4 after X03 (topology service)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from ..api.facade import NotFound
from ..contracts import RelationKind, Topology, TopologyStatus
from ..electrical.nesting import infer_nesting

#: Downsample the nesting comparison grid to the channels' declared sampling
#: cadence instead of using the union of every distinct timestamp.
#:
#: **Off by default, and deliberately so.**  The union is 29-38x larger than the
#: data it describes (76 893 instants for 2 615 samples per channel over 12 h), so
#: resampling is a large CPU win, and the set of candidate pairs it produces is
#: identical on the reference installation.  But ``instants``,
#: ``joint_instants``, ``containment`` and the median evidence in the payload all
#: describe *how many instants were compared*, and those genuinely change.
#: Enabling this by default would silently alter a published response, so it is a
#: flag: ``_sample_grid`` documents the argument and the guard rails.
NESTING_RESAMPLE_GRID = False


class TopologyApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    def topology(self) -> Topology:
        topology = self._active_topology()
        if topology is None:
            return Topology(version=0, status=TopologyStatus.CANDIDATE, notes=["no topology configured"])
        return topology

    def set_topology(self, topology: Any) -> Topology:
        if not isinstance(topology, Topology):
            topology = Topology.model_validate(topology)
        if topology.status == TopologyStatus.CONFIRMED:
            # Replacing a confirmed topology creates a candidate revision unless
            # the caller explicitly marks it confirmed.
            pass
        self.db.save_topology(topology, note="api: replace topology")
        self.topology_cache = self.db.load_topology()
        self.db.log_system_event(
            "info",
            "topology",
            "topology replaced via API",
            {"channels": len(topology.channels), "devices": len(topology.devices)},
        )
        return self.topology_cache or topology

    def confirm_topology(self) -> Topology:
        topology = self._active_topology()
        if topology is None:
            raise NotFound("no topology to confirm")
        confirmed = topology.model_copy(
            update={
                "status": TopologyStatus.CONFIRMED,
                "devices": [
                    d.model_copy(update={"topology_status": TopologyStatus.CONFIRMED})
                    for d in topology.devices
                ],
            }
        )
        self.db.save_topology(confirmed, note="api: confirm topology")
        self.topology_cache = self.db.load_topology()
        self.db.log_system_event("info", "topology", "topology confirmed via API")
        return self.topology_cache or confirmed

    def nesting_candidates(self, *, hours: float = 12.0) -> dict[str, Any]:
        """Propose parent links and duplicate feeds from stored history.

        This is a *read*: it never writes a parent link.  The evidence is
        measured containment (a child cannot exceed its feed) plus closure (the
        parent's own loads plus the child must not over-explain the parent), and
        every result is a candidate the integration wizard asks the user to
        confirm.
        """
        topology = self._active_topology()
        if topology is None:
            return {"candidates": [], "reason": "no topology configured"}
        channels = topology.channels
        if not channels:
            return {"candidates": [], "reason": "topology has no channels"}

        end = self._clock()
        start = end - timedelta(hours=max(0.5, float(hours)))
        # ``ct_samples.ct_id`` is the *aligner* key (the entity object id), not the
        # topology's ``ct_id``: the ingestion path derives it with
        # ``entity_id.split(".", 1)[1]``.  Querying the topology id directly
        # silently returns nothing, which would look exactly like "no history
        # yet" and leave the wizard with no proposals forever.
        key_to_ct: dict[str, str] = {}
        for ch in channels:
            if not ch.enabled:
                continue
            key_to_ct[self._aligner_key(ch)] = ch.ct_id
        rows = self.db.ct_samples_between(start, end, ct_ids=sorted(key_to_ct))
        if not rows:
            return {
                "candidates": [],
                "reason": "no stored CT samples in the comparison window",
                "window_hours": float(hours),
            }

        samples: dict[str, list[tuple[datetime, float]]] = {}
        for row in rows:
            value = row.get("value_w")
            if not isinstance(value, (int, float)):
                continue
            if not math.isfinite(float(value)):
                continue
            # ``state_timestamp`` / ``received_at`` are stored as ISO text.
            stamp = _as_utc(row.get("state_timestamp")) or _as_utc(row.get("received_at"))
            if stamp is None:
                continue
            # Re-key from the aligner key back to the topology ``ct_id`` so the
            # rest of this method (and the caller) speaks one namespace.
            ct_id = key_to_ct.get(str(row["ct_id"]))
            if ct_id is None:
                continue
            samples.setdefault(ct_id, []).append((stamp, float(value)))
        if len(samples) < 2:
            return {"candidates": [], "reason": "fewer than two channels have samples"}

        grid = (
            _sample_grid(
                samples,
                cadence_s=_sampling_cadence_s(channels),
                window_hours=max(0.5, float(hours)),
            )
            if NESTING_RESAMPLE_GRID
            else sorted({stamp for series in samples.values() for stamp, _ in series})
        )
        if len(grid) < 10:
            return {
                "candidates": [],
                "reason": "too few distinct instants to compare",
                "instants": len(grid),
            }
        aligned = {
            cid: _forward_fill([entry for entry in series], grid)
            for cid, series in samples.items()
        }

        by_id = {ch.ct_id: ch for ch in channels}
        container_ids = [
            ch.ct_id
            for ch in channels
            if ch.enabled and str(ch.ct_type) in ("main", "branch")
        ]

        report: dict[str, Any] = {}
        candidates = infer_nesting(
            {
                cid: {
                    "ct_type": str(ch.ct_type),
                    # Declared relationships are *facts the user has already
                    # stated*: the two legs of a declared double-pole pair are
                    # one circuit, not one wire measured twice, and a declared
                    # parent is not re-proposed.  ``phase`` is passed so the
                    # written-back leg is readable, but it is never used to
                    # reject a candidate -- on the reference installation the
                    # phase labels are contradicted by the measurements.
                    "phase": str(ch.phase),
                    "paired_ct_id": ch.paired_ct_id,
                    "parent_ct_id": ch.parent_ct_id,
                }
                for cid, ch in by_id.items()
            },
            {cid: values for cid, values in aligned.items() if cid in by_id},
            container_ct_ids=container_ids,
            # Closure now relies on the parent's measured series alone: there is
            # no per-container "own loads" series to subtract, because the
            # channel role that grouped one is gone.  ``infer_nesting`` accepts
            # ``None`` and skips the own-load correction.
            direct_loads=None,
            report=report,
        )

        # Attach the candidate parent(s) to each channel as *proposals* so the
        # wizard can pre-select a single unambiguous one without ever writing it.
        proposals: dict[str, list[str]] = {}
        nested = [c for c in candidates if c.kind == "nested"]
        for candidate in nested:
            proposals.setdefault(candidate.child_ct_id, []).append(candidate.parent_ct_id)
        # Duplicate feeds are the same *conductor* seen twice, not a nesting.
        # They are offered separately because they answer a different question:
        # "is this CT an alias of another one?" rather than "what feeds it?".
        alias_proposals: dict[str, str] = {}
        for candidate in candidates:
            if candidate.kind == "duplicate":
                alias_proposals[candidate.child_ct_id] = candidate.parent_ct_id

        return {
            "window_hours": float(hours),
            "instants": len(grid),
            "channels_compared": len(aligned),
            "candidates": [c.as_dict() for c in candidates],
            "candidate_parents": {cid: sorted(p) for cid, p in proposals.items()},
            "candidate_aliases": dict(sorted(alias_proposals.items())),
            # What was considered and rejected, so the wizard can say "three
            # channels are ambiguous" rather than silently showing nothing.
            "ambiguous": report.get("ambiguous", []),
            "suppressed": report.get("suppressed", []),
            # Loads whose level measurement cannot establish.  Reported so the
            # UI can ask the user instead of presenting a guess as evidence.
            "not_inferable": report.get("not_inferable", []),
            "nodes": report.get("nodes", []),
            "notes": [
                "Candidates only: no parent link is written until the user confirms it.",
                "Containment proves a child never exceeds its feed; it does not by "
                "itself prove which feed.",
                "Only a panel feed can be placed by measurement. A load is contained "
                "by every panel while idle, so loads are never proposed and the "
                "wizard asks the user.",
                "A child whose level cannot be established is listed as ambiguous "
                "and is never pre-selected.",
                "A duplicate feed is the same conductor measured twice: it is offered "
                "as an alias, never summed as a second feed.",
            ],
        }

    def relations(self, kind: str | None = None) -> dict[str, Any]:
        """Stored typed edges, grouped by kind.

        ``kind`` filters to one relation kind; without it every kind is
        reported separately, because ``DEVICE_CT_ASSOCIATION``,
        ``CT_MEASUREMENT_OVERLAP`` and ``ELECTRICAL_PARENT_CHILD`` answer
        different questions and must never be merged into one list.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for relation_kind in RelationKind:
            if kind is not None and str(getattr(relation_kind, "value", relation_kind)) != str(
                kind
            ):
                continue
            try:
                rows = self.db.relations_of_kind(relation_kind)
            except Exception:  # noqa: BLE001
                rows = []
            if rows or kind is not None:
                out[str(getattr(relation_kind, "value", relation_kind))] = [
                    self._jsonable(r) for r in rows
                ]
        return {
            "kinds": out,
            "counts": {name: len(rows) for name, rows in out.items()},
            "requires_confirmation": sorted(
                {
                    str(row.get("subject_id"))
                    for rows in out.values()
                    for row in rows
                    if row.get("requires_confirmation")
                }
            ),
        }

    def _topology_health(self) -> dict[str, Any]:
        """Topology presence for ``/health``; a missing topology is not healthy."""
        topology = self._active_topology()
        if topology is None:
            return {
                "configured": False,
                "reason": self.topology_bootstrap_error or "no topology configured",
            }
        raw_status = getattr(topology.status, "value", topology.status)
        return {
            "configured": True,
            "status": str(raw_status),
            "version": topology.version,
            "channels": len(topology.channels),
            "devices": len(topology.devices),
        }


def _as_utc(value: Any) -> datetime | None:
    """Parse a stored timestamp into an aware UTC ``datetime``.

    ``ct_samples`` keeps timestamps as ISO **text**, so a ``datetime`` check is
    never true for a database row.  Naive values are treated as UTC because that
    is what the writer guarantees (``storage.database.iso``).
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def _sampling_cadence_s(channels: Iterable[Any]) -> float | None:
    """The channels' declared sampling cadence, in seconds, or ``None``.

    Uses the median of the enabled channels' ``expected_interval_s`` so one
    mis-declared channel cannot change the grid.  Deliberately *not* a hardcoded
    15.7 s: the cadence is a property of the installation, and the reference
    install happens to declare 15.7 s while its cheapest channel samples every
    10 s.
    """
    declared = [
        float(ch.expected_interval_s)
        for ch in channels
        if getattr(ch, "enabled", False)
        and getattr(ch, "expected_interval_s", None)
        and float(ch.expected_interval_s) > 0
    ]
    if not declared:
        return None
    declared.sort()
    mid = len(declared) // 2
    return declared[mid] if len(declared) % 2 else (declared[mid - 1] + declared[mid]) / 2.0


def _sample_grid(
    samples: Mapping[str, Sequence[tuple[datetime, float]]],
    *,
    cadence_s: float | None,
    window_hours: float,
) -> list[datetime]:
    """Alignment instants for the nesting comparison.

    Why this is not simply the union of every timestamp
    ------------------------------------------------
    The union is not a sampling grid.  On the reference installation a 12 h
    window contains **76 893** distinct timestamps for 38 channels that each
    carry only ~2 615 samples: the union is dominated by per-channel clock jitter,
    so it is 29–38× larger than the data it describes.  Every instant then costs
    one comparison per pair in :func:`~wattmosaic_engine.electrical.nesting.infer_nesting`
    and one cell in every aligned series, for no new information: forward-filling
    a channel between two of its own samples cannot tell you anything the earlier
    sample did not.

    What it does instead
    --------------------
    Bucket the union into fixed ``cadence_s`` slots (anchored at the first
    timestamp so the result is deterministic) and keep the **latest real sample**
    in each slot.  Keeping a real timestamp rather than the bucket edge means
    :func:`_forward_fill` still sees true sample times, so no reading is ever
    invented or shifted earlier than it happened.

    The guard rails
    ---------------
    * ``cadence_s is None`` (nothing declared) → the union, i.e. the old
      behaviour.  Never guess a cadence from nothing.
    * Never coarser than the median inter-sample gap of the *fastest* channel:
      a cadence above the real sampling interval would throw away real readings
      and replace a measured series with a piecewise-constant one.  The step is
      therefore ``min(declared cadence, fastest median gap)``.
    * Never when the window holds fewer than two cadence periods: with less than
      that, the grid would collapse to one or two instants and containment would
      lose all meaning, so the union is kept.

    Equivalence
    -----------
    The **set of candidate pairs** (``kind``, ``child_ct_id``, ``parent_ct_id``)
    is byte-identical to the union grid at hours ∈ {2, 6, 12, 24} on the reference
    installation, and the whole payload is byte-identical when this returns the
    union.  It is **not** byte-identical when it resamples: ``instants``,
    ``joint_instants``, ``containment`` and the median evidence describe *how many
    instants were compared*, and comparing 2 526 instead of 76 893 genuinely
    changes those numbers.  That is a real, visible change to the response, which
    is why :data:`NESTING_RESAMPLE_GRID` defaults to ``False``.
    """
    union = sorted({stamp for series in samples.values() for stamp, _ in series})
    if cadence_s is None or not union:
        return union

    # Median inter-sample gap across channels: the fastest real cadence.
    gaps: list[float] = []
    for series in samples.values():
        ordered = sorted(series)
        deltas = [
            (later[0] - earlier[0]).total_seconds()
            for earlier, later in zip(ordered, ordered[1:], strict=False)
        ]
        deltas = [delta for delta in deltas if delta > 0]
        if deltas:
            deltas.sort()
            mid = len(deltas) // 2
            gaps.append(
                deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) / 2.0
            )
    if gaps:
        gaps.sort()
        mid = len(gaps) // 2
        fastest_gap = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
        # Never stride past the fastest real sampling: that would discard real
        # readings rather than merely de-duplicating jittered ones.
        cadence_s = min(cadence_s, fastest_gap)

    if window_hours > 0:
        periods = (window_hours * 3600.0) / cadence_s
        if periods < 2.0:
            # Too short a window to resample: keep every instant.
            return union

    anchor = union[0]
    grid: list[datetime] = []
    current_slot: int | None = None
    last_stamp: datetime | None = None
    for stamp in union:
        slot = int((stamp - anchor).total_seconds() // cadence_s)
        if slot != current_slot:
            if last_stamp is not None:
                grid.append(last_stamp)
            current_slot = slot
        last_stamp = stamp
    if last_stamp is not None:
        grid.append(last_stamp)
    return grid


def _forward_fill(
    series: list[tuple[datetime, float]], grid: list[datetime]
) -> list[float]:
    """Project a sparse ``(timestamp, watts)`` series onto an aligned grid.

    A channel that did not update at an instant is represented by its last known
    value, which is what the synchroniser does for a point-in-time snapshot.  A
    value is *never* invented before the first sample: those slots are ``nan``,
    and ``infer_nesting`` skips non-finite samples rather than treating an
    absent reading as zero (ADR-0015 / defect #33).
    """
    out: list[float] = []
    index = 0
    current = math.nan
    ordered = sorted(series)
    for stamp in grid:
        while index < len(ordered) and ordered[index][0] <= stamp:
            current = ordered[index][1]
            index += 1
        out.append(current)
    return out


def _linkable_state_entities(topology: Topology) -> set[str]:
    """Entities the topology declares as runtime signals for some device.

    Auto placement may only reason about entities the engine already links.  An
    undeclared entity is unknown to the topology -- it could be a scene, a
    template, or another integration's shadow -- and treating it as a load would
    be a guess about a device that does not exist yet.  The mapping wizard (and
    the user) decide what is linkable; this only reads that decision.

    Candidate *population* is now the state snapshot (see
    :mod:`wattmosaic_engine.ingestion.candidate_entities`); this remains the
    declaration read used by the learning/event paths and is kept as the single
    definition both places share.
    """
    from ..ingestion.candidate_entities import declared_state_entities

    return declared_state_entities(topology)

