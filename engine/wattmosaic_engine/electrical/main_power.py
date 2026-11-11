"""Main incoming power with sign preservation and duplicate-feed resolution.

Rules
-----
* ``P_main = sum(P_mains)`` only when the feeds are *provably additive*: their
  declared phases are disjoint (typically one ``L1`` feed and one ``L2`` feed,
  the two legs of one split-phase service).
* Two feeds on the **same** phase cannot both be independent additions to one
  total.  One conductor is fed by one source, so two CTs reporting the same leg
  are either the same wire measured twice (duplicate) or one is downstream of
  the other (nested).  Summing them counts the house twice.  When that happens
  and no ``parent_ct_id`` declares which is which, the total is resolved by
  *measurement*: the largest complete feed group is the outermost feed and the
  rest are reported as excluded, with a note saying the relation is undeclared.
  Refusing outright would leave the household with no number at all; guessing a
  parent would fabricate a wiring fact.  Reporting the outermost observed feed
  is the one answer the measurements support.
* Negative power is preserved.  It means export / reverse flow.  Nothing here
  clamps to zero.
* A missing main is reported per channel; the total is computed over the mains
  that are usable and ``complete`` becomes ``False``.  The caller must not
  present a partial total as the whole-home power.
* Polarity inversion is applied per channel before summation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from wattmosaic_engine.contracts import Phase, Polarity
from wattmosaic_engine.electrical.topology import TopologyGraph

#: A split-phase panel feed is published as two CTs whose ids differ only in the
#: leg suffix.  The same grouping the nesting detector uses, so a feed is judged
#: as one meter point in both places.
_LEG_SUFFIX = re.compile(r"^(?P<stem>.+)_l[12]$")

#: Slack in watts when deciding whether one feed group contains another.  CT
#: resolution and sampling skew mean the outer feed is not exactly >= the inner
#: one at every instant.
_CONTAINMENT_TOL_W = 30.0


@dataclass(frozen=True, slots=True)
class MainPower:
    total_w: float
    l1_w: float | None = None
    l2_w: float | None = None
    per_channel_w: dict[str, float] = field(default_factory=dict)
    missing_channels: tuple[str, ...] = ()
    excluded_channels: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    complete: bool = True
    has_export: bool = False

    @property
    def usable(self) -> bool:
        return not self.errors and bool(self.per_channel_w)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_w": self.total_w,
            "l1_w": self.l1_w,
            "l2_w": self.l2_w,
            "per_channel_w": dict(self.per_channel_w),
            "missing_channels": list(self.missing_channels),
            "excluded_channels": list(self.excluded_channels),
            "errors": list(self.errors),
            "notes": list(self.notes),
            "complete": self.complete,
            "has_export": self.has_export,
        }


def _read_w(
    readings: Mapping[str, object],
    channel_id: str,
    *,
    channel_polarity: Polarity = Polarity.NORMAL,
) -> float | None:
    """Read a channel value.

    ``ChannelReading`` / ``Measurement`` objects already carry their own
    polarity, so they are used as-is.  A bare float has no polarity metadata,
    so the topology's channel polarity is applied here.
    """
    raw = readings.get(channel_id)
    if raw is None:
        return None
    if hasattr(raw, "signed_w"):
        return raw.signed_w()
    if hasattr(raw, "effective_power_w"):
        return float(raw.effective_power_w())
    if isinstance(raw, Mapping):
        val = raw.get("value_w", raw.get("value"))
        if val is None:
            return None
        pol = str(raw.get("polarization", raw.get("polarity", "normal")))
        return -float(val) if pol.endswith("inverted") else float(val)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        # NaN / inf are missing, not zero.
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return -value if channel_polarity == Polarity.INVERTED else value
    return None


def _leg_stem(ct_id: str) -> str | None:
    match = _LEG_SUFFIX.match(ct_id)
    return None if match is None else match.group("stem")


def _measurement_nodes(mains: Sequence[object]) -> list[list[str]]:
    """Group feed CTs that measure the *same meter point*.

    A split-phase panel feed is published as ``<stem>_l1`` / ``<stem>_l2`` and a
    declared ``paired_ct_id`` pair is one double-pole circuit by declaration.
    Either way the legs together are one feed: a total must use both or neither,
    because adding one panel's L1 to another panel's L2 is not a household total.

    Legs of *different* panels are deliberately never merged even when their
    declared phase labels happen to agree -- on the reference install the
    sub-panel's L2 is mostly fed from the load manager's L1, so the declared
    labels are not a reliable pairing key across panels.
    """
    by_id = {str(ch.ct_id): ch for ch in mains}
    assigned: set[str] = set()
    groups: list[list[str]] = []

    def partner_of(ct_id: str) -> str | None:
        ch = by_id[ct_id]
        partner = str(getattr(ch, "paired_ct_id", "") or "")
        if not partner or partner not in by_id or partner == ct_id:
            return None
        back = str(by_id[partner].paired_ct_id or "")
        return partner if back == ct_id else None

    for ct_id in sorted(by_id):
        if ct_id in assigned:
            continue
        partner = partner_of(ct_id)
        if partner is not None:
            groups.append(sorted({ct_id, partner}))
            assigned.update({ct_id, partner})
            continue
        stem = _leg_stem(ct_id)
        legs = (
            sorted(cid for cid in by_id if cid not in assigned and _leg_stem(cid) == stem)
            if stem is not None
            else []
        )
        if len(legs) == 2:
            groups.append(legs)
            assigned.update(legs)
            continue
        groups.append([ct_id])
        assigned.add(ct_id)
    return groups


def _node_legs(node: Sequence[str], by_id: Mapping[str, object]) -> set[str]:
    """The legs a meter point measures.  ``both``/``unknown`` span both."""
    legs: set[str] = set()
    for cid in node:
        phase = Phase(str(getattr(by_id[cid], "phase", Phase.UNKNOWN)))
        if phase in (Phase.L1, Phase.L2):
            legs.add(phase.value)
        else:
            legs.update({Phase.L1.value, Phase.L2.value})
    return legs


def main_power_w(
    graph: TopologyGraph,
    readings: Mapping[str, object],
    *,
    phase_split_l1: float = 0.5,
) -> MainPower:
    """Sum the feeds of the household that are provably additive, preserving sign.

    Only :meth:`TopologyGraph.effective_feeds` contribute.  A nested main (one
    feed behind another) is not added again, so a panel tree of any depth sums
    correctly.  Aliased channels (two CTs on one conductor) contribute once.

    Feeds are grouped into *measurement nodes* (a split-phase panel feed is one
    node, not two).  Two nodes are additive only when their declared phases are
    disjoint: a 120/240 V service is two legs and the legs add, but two CTs on
    the *same* leg cannot both be part of one total, because one conductor is
    fed from one place.  When such a pair has no declared ``parent_ct_id``, the
    relation is resolved from the measurements -- the outermost node is the one
    that contains the others -- and every excluded feed is named in ``notes``.
    That resolution is reported, never silent, because only the user can confirm
    the wiring.

    A channel's ``measurement_multiplier`` is applied here, so every consumer of
    a main total sees the same scaled value.
    """
    errors: list[str] = []
    nested = graph.nested_mains()
    notes: list[str] = []
    if nested:
        # Reported, not refused: the inner feed is excluded from the sum and the
        # exclusion is visible to the caller.
        detail = ", ".join(f"{inner} under {outer}" for inner, outer in nested)
        notes.append("nested_feeds_not_summed: " + detail)

    aliases = [c.ct_id for c in graph.alias_channels() if graph.is_enabled(c.ct_id)]
    if aliases:
        notes.append("duplicate_feeds_counted_once: " + ", ".join(sorted(aliases)))

    mains = graph.feed_channels()
    if not mains:
        errors.append("no enabled main CT is defined in the topology")
        return MainPower(total_w=0.0, errors=tuple(errors), complete=False)

    by_id = {ch.ct_id: ch for ch in mains}
    nodes = _measurement_nodes(mains)

    values: dict[str, float | None] = {
        ch.ct_id: graph.scale_w(
            ch.ct_id,
            _read_w(readings, ch.ct_id, channel_polarity=Polarity(ch.polarity)),
        )
        for ch in mains
    }
    missing = [cid for cid, v in values.items() if v is None]

    def node_total(node: Sequence[str]) -> float | None:
        vals = [values[cid] for cid in node if values[cid] is not None]
        return float(sum(vals)) if vals else None

    def node_complete(node: Sequence[str]) -> bool:
        return all(values[cid] is not None for cid in node)

    # A node is one meter point (both legs of one panel feed).  Nodes whose
    # declared legs overlap measure the same conductor and cannot both be added.
    node_legs = {i: _node_legs(n, by_id) for i, n in enumerate(nodes)}
    conflict_groups: list[list[int]] = []
    for i in range(len(nodes)):
        merged = [i]
        remaining: list[list[int]] = []
        for group in conflict_groups:
            if any(node_legs[i] & node_legs[j] for j in group):
                merged.extend(group)
            else:
                remaining.append(group)
        remaining.append(sorted(merged))
        conflict_groups = remaining
    conflict_groups.sort()

    excluded: set[str] = set()
    selected: list[list[str]] = []
    partial = False
    for group in conflict_groups:
        members = [nodes[i] for i in group]
        # Every member here is a *top-level* feed: aliases and nested mains were
        # already removed by ``effective_feeds``, so no member is redundant.
        # A member with no reading at all is a feed this total cannot see -- it
        # may carry part of the household (disjoint legs add), or it may be the
        # outermost feed the measured member nests inside.  Which of those it is
        # cannot be decided without the reading, so the sum is not provably the
        # whole-home power.  ``complete`` therefore must be False: the module
        # contract is that a missing main is reported, never silently dropped.
        unmeasured = [n for n in members if node_total(n) is None]
        if unmeasured:
            partial = True
            notes.append(
                "feed_group_unmeasured: "
                + "; ".join("+".join(n) for n in unmeasured)
            )
        usable = [n for n in members if node_total(n) is not None]
        if not usable:
            continue
        # Prefer meter points measured on every leg: a node missing a leg
        # understates the feed and must not win over a complete measurement.
        complete = [n for n in usable if node_complete(n)]
        candidates = complete or usable
        if not complete:
            partial = True
        if len(candidates) == 1:
            selected.append(candidates[0])
            continue
        # Several feeds on the same conductor.  One conductor is fed from one
        # place, so at most one may define the total; the outermost is the one
        # whose measurement covers the others.  That containment is measurement
        # evidence, and the choice is named in ``notes`` -- only the user can
        # confirm the wiring.
        totals = {id(n): float(node_total(n)) for n in candidates}

        def dominates(candidate: Sequence[str], _totals=totals, _candidates=candidates) -> bool:
            total = _totals[id(candidate)]
            for other in _candidates:
                if other is candidate:
                    continue
                slack = max(_CONTAINMENT_TOL_W, 0.02 * abs(total))
                if _totals[id(other)] > total + slack:
                    return False
            return True

        outer = [n for n in candidates if dominates(n)]
        if len(outer) != 1:
            # Indistinguishable by magnitude, or none contains the others.
            # Summing them would double count; picking one would fabricate the
            # panel tree.  Refuse the total rather than state a wrong one.
            errors.append("parallel_mains_without_declared_relation")
            continue
        chosen = outer[0]
        selected.append(chosen)
        dropped = sorted({cid for n in members if n is not chosen for cid in n})
        excluded.update(dropped)
        notes.append(
            "undeclared_feed_relation_resolved_by_measurement: "
            f"{'+'.join(chosen)} is the outermost measured feed; excluded "
            + ", ".join(dropped)
        )

    per_channel: dict[str, float] = {}
    l1: list[float] = []
    l2: list[float] = []
    both: list[float] = []
    for node in selected:
        for cid in node:
            value = values[cid]
            if value is None:
                continue
            per_channel[cid] = value
            phase = Phase(by_id[cid].phase)
            if phase == Phase.L1:
                l1.append(value)
            elif phase == Phase.L2:
                l2.append(value)
            else:
                both.append(value)  # BOTH and UNKNOWN both contribute to the total

    usable_l1 = sum(l1) if l1 else None
    usable_l2 = sum(l2) if l2 else None
    # A BOTH main is already a full-circuit total; split for reporting only.
    both_l1 = sum(both) * phase_split_l1 if both else 0.0
    both_l2 = sum(both) * (1.0 - phase_split_l1) if both else 0.0

    reported_l1 = None if usable_l1 is None else usable_l1 + both_l1
    reported_l2 = None if usable_l2 is None else usable_l2 + both_l2
    total = sum(per_channel.values())
    unknown_phase = [ch.ct_id for ch in mains if Phase(ch.phase) == Phase.UNKNOWN]
    if unknown_phase:
        errors.append("main CT(s) with unknown phase: " + ", ".join(unknown_phase))

    selected_ids = {cid for node in selected for cid in node}
    complete = (
        not errors
        and not [cid for cid in missing if cid in selected_ids]
        and not partial
        and bool(per_channel)
    )
    return MainPower(
        total_w=total,
        l1_w=reported_l1,
        l2_w=reported_l2,
        per_channel_w=per_channel,
        missing_channels=tuple(sorted(missing)),
        excluded_channels=tuple(
            sorted(
                (
                    {c for pair in nested for c in pair}
                    | set(aliases)
                    | excluded
                )
                - set(per_channel)
            )
        ),
        errors=tuple(errors),
        notes=tuple(notes),
        complete=complete,
        has_export=any(v < 0 for v in per_channel.values()),
    )


def phase_power_w(
    graph: TopologyGraph,
    readings: Mapping[str, object],
) -> dict[str, float | None]:
    """Convenience: ``{"l1": ..., "l2": ..., "total": ...}`` from main CTs."""
    mp = main_power_w(graph, readings)
    return {"l1": mp.l1_w, "l2": mp.l2_w, "total": mp.total_w}
