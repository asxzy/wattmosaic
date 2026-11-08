"""Candidate panel nesting and duplicate-feed detection from measurements.

Why this module is separate
---------------------------
``CTChannel.parent_ct_id`` describes a *wiring fact*: which feed a CT sits
behind.  A wiring fact cannot be guessed from an entity id, a device name or a
panel label without fabricating it, so discovery never writes one.  What
observation *can* support is a **candidate**, and that is what this module
produces.

What the measurements can and cannot decide
-------------------------------------------
Measured on the reference installation (48 h, 38 channels, 16 containers), the
evidence is sharply asymmetric and this module is built around that asymmetry
rather than pretending it away:

* **A panel feed's level is measurable.**  The outer panel feed contains the
  inner one at 0.9975 of instants; the inner contains the outer at 0.0032.  One
  direction only, so the level is determined.
* **A load's level is not measurable.**  A 9 W lighting circuit is "contained"
  by every panel in the house at 1.000, because both are near idle most of the
  time.  Every containment-, coverage-, closure- and step-response-based rule
  tried on real data left all 32 loads ambiguous or placed them wrong.  So this
  module never proposes a parent for a ``branch`` or ``dedicated_device`` CT.
  The wizard asks the user instead.

The unit of inference is a **measurement node**, not a single CT.  One meter
point can be observed by more than one CT:

* the two legs of a declared double-pole circuit (``paired_ct_id``), which
  together are one conductor pair;
* the two legs of a split-phase panel feed (``<stem>_l1`` / ``<stem>_l2``),
  which together are one panel.

Comparing legs instead of nodes is what made the earlier implementation fail:
the inner panel's L1 declared loads total 796 W against its own 331 W L1 leg
(ratio 2.40) because those loads span *both* legs, so per-leg closure
over-explained the leg by construction, and the inner panel's L2 was only 0.6390
contained by the outer panel's L2 while being 0.9950 contained by the outer
panel's L1 -- the declared ``phase`` labels are contradicted by the
measurements.  Summing both legs removes the problem: the outer feed contains
the inner one at 0.9975.

The evidence
------------
containment
    Fraction of instants where the child never exceeds the parent (plus a
    tolerance).  A branch circuit cannot draw more power than the feed that
    supplies it, so a child that stays inside a parent at every instant is
    *consistent with* being fed by it.  Necessary, not sufficient.
excitation
    The child must actually be doing something.  A circuit that is always near
    zero is trivially contained by everything.
closure
    ``parent - (parent's own loads + child)`` must stay non-negative, and the
    *median* remainder ranks competing parents.  The immediate parent is the one
    the child explains most tightly; a grandparent has the child's siblings left
    over.
duplicate
    Two nodes that are jointly active for essentially the whole time either of
    them is active, and that agree on magnitude at every such instant, are
    watching the same conductors.  They are *not* nested: summing them
    double-counts.  They are reported as a duplicate feed (``alias_of_ct_id``)
    instead.

Three limits are enforced rather than papered over:

* **Activity.**  Two channels that are both near idle agree perfectly on
  everything, so a duplicate is only considered while both carry real load.
* **Declared facts win.**  A declared double-pole pair is two legs of one
  circuit, never one wire measured twice; a declared parent/child pair is a
  nesting, not a duplicate.
* **Ambiguity is reported, not resolved.**  When several parents fit and closure
  cannot separate them, the child is returned as *ambiguous* with the
  alternatives listed.  The wizard never pre-selects it.

Every result carries its evidence and is marked ``candidate``.  Nothing here
mutates a topology; the integration wizard asks the user to confirm.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: A child at or below this fraction of its parent at essentially every instant
#: is consistent with being fed by it.  Set high on purpose: a nested feed is a
#: measurement relationship, not a statistical tendency.
CONTAINMENT_MIN = 0.995

#: Watts of slack, so CT resolution and sampling skew cannot break containment.
CONTAINMENT_TOL_W = 30.0

#: Below this the parent is not carrying enough power for the comparison to
#: mean anything.  Instants are excluded, not counted as failures.
CONTAINMENT_MIN_PARENT_W = 200.0

#: The child must reach this fraction of the parent at least once, otherwise it
#: is an idle circuit that is contained by everything.
EXCITATION_MIN_RATIO = 0.05

#: A channel must carry at least this much for its reading to say anything about
#: *which* wire it is on.  Two channels at 8 W and 9 W agree about nothing.
ACTIVITY_MIN_W = 200.0

#: Fraction of the pair's combined active time during which *both* are active.
#: Two CTs on one wire are active together essentially always.  Two unrelated
#: circuits that are each idle most of the time are rarely active together, and
#: any agreement between them is the agreement of two near-zero numbers.
DUPLICATE_ACTIVITY_RATIO_MIN = 0.5

#: Median disagreement between two measurements of one wire, relative to the
#: larger reading.  Measured on the reference installation: two monitors on the
#: same service conductors disagree by 5.7 % and 9.0 % (median), while the
#: closest pair of genuinely different circuits disagrees by 16.6 %.
DUPLICATE_REL_DIFF_MAX = 0.15

#: Median magnitude ratio bound.  A x2-declared leg reports half its partner and
#: must stay a separate channel for the user to scale, not be collapsed.
DUPLICATE_RATIO_MAX = 1.25

#: Closure: the parent must never be explained away beyond this slack.
CLOSURE_TOL_W = 100.0

#: Fraction of comparable instants where the parent covers its own loads plus
#: the child.  Below this the parent is *over-explained* by the child, so the
#: child cannot be inside it.
CLOSURE_MIN = 0.95

#: A split-phase panel feed is published as two CTs whose ids differ only in the
#: leg suffix.  This is the same ``name_derived`` grouping discovery already uses
#: for double-pole loads, and it is reported as an assumption, never as fact.
_LEG_SUFFIX = re.compile(r"^(?P<stem>.+)_l[12]$")

#: A channel type that may be proposed as a nesting *child*.  Only a panel feed
#: has a level that measurement can establish; see the module docstring.
_NESTABLE_CHILD_TYPES = frozenset({"main"})


@dataclass(frozen=True, slots=True)
class MeasurementNode:
    """One physical meter point, observed by one or more CT channels.

    ``members`` is the set of CTs that measure the *same* conductors.  A node is
    the unit every comparison in this module is made on: comparing legs of one
    panel against legs of another compares four different conductors and produces
    numbers that mean nothing.
    """

    node_id: str
    members: tuple[str, ...]
    ct_type: str
    #: ``"single"``, ``"declared_pair"`` or ``"name_stem"``.  The last is an
    #: assumption derived from the channel ids and is always reported as such.
    grouping: str
    phase: str

    @property
    def is_assumed(self) -> bool:
        return self.grouping == "name_stem"

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "members": list(self.members),
            "ct_type": self.ct_type,
            "grouping": self.grouping,
            "phase": self.phase,
        }


@dataclass(frozen=True, slots=True)
class NestingCandidate:
    """A proposed ``child`` fed by ``parent``, with the evidence for it."""

    child_ct_id: str
    parent_ct_id: str
    kind: str  # "nested" | "duplicate"
    evidence: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "child_ct_id": self.child_ct_id,
            "parent_ct_id": self.parent_ct_id,
            "kind": self.kind,
            "evidence": dict(self.evidence),
            "requires_confirmation": self.requires_confirmation,
            "status": "candidate",
        }


@dataclass(frozen=True, slots=True)
class DuplicateEvidence:
    """How strongly two series look like one wire measured twice."""

    joint_instants: int
    activity_ratio: float
    rel_diff: float
    ratio: float
    median_a_w: float
    median_b_w: float


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def containment(
    outer: Sequence[float],
    inner: Sequence[float],
    *,
    tol_w: float = CONTAINMENT_TOL_W,
    min_outer_w: float = CONTAINMENT_MIN_PARENT_W,
) -> tuple[float, int]:
    """Fraction of comparable instants where ``inner`` stays inside ``outer``.

    Returns ``(fraction, comparable_instants)``.  Instants where the outer
    channel is below ``min_outer_w`` are skipped: at that level the comparison is
    noise, and counting them either way would make the fraction meaningless.
    An empty comparison returns ``(0.0, 0)`` -- no evidence, never "contained".
    """
    if len(outer) != len(inner):
        raise ValueError("series must have the same length")
    ok = 0
    comparable = 0
    for o, i in zip(outer, inner, strict=True):
        if not _finite(o) or not _finite(i):
            continue
        o = float(o)
        i = float(i)
        if o < min_outer_w:
            continue
        comparable += 1
        if i <= o + tol_w:
            ok += 1
    if comparable == 0:
        return 0.0, 0
    return ok / comparable, comparable


def duplicate_evidence(
    a: Sequence[float],
    b: Sequence[float],
    *,
    activity_min_w: float = ACTIVITY_MIN_W,
) -> DuplicateEvidence | None:
    """Measure whether ``a`` and ``b`` are one wire measured twice.

    Only instants where **both** series carry at least ``activity_min_w`` are
    evidence.  This is the guard that mutual containment lacks: at near-idle
    levels two unrelated channels agree to the watt on everything, so every idle
    pair looked like a duplicate.

    Returns ``None`` when there is no joint activity at all -- which is not a
    negative result but the absence of one.
    """
    if len(a) != len(b):
        raise ValueError("series must have the same length")
    joint: list[tuple[float, float]] = []
    either = 0
    for x, y in zip(a, b, strict=True):
        if not _finite(x) or not _finite(y):
            continue
        x = float(x)
        y = float(y)
        active_x = abs(x) >= activity_min_w
        active_y = abs(y) >= activity_min_w
        if active_x or active_y:
            either += 1
        if active_x and active_y:
            joint.append((x, y))
    if not joint or either == 0:
        return None

    diffs = [abs(x - y) / max(abs(x), abs(y), 1.0) for x, y in joint]
    med_a = _median([x for x, _ in joint])
    med_b = _median([y for _, y in joint])
    return DuplicateEvidence(
        joint_instants=len(joint),
        activity_ratio=len(joint) / either,
        rel_diff=_median(diffs),
        ratio=max(abs(med_a), abs(med_b)) / max(min(abs(med_a), abs(med_b)), 1e-9),
        median_a_w=med_a,
        median_b_w=med_b,
    )


def closure_fraction(
    parent: Sequence[float],
    accounted: Sequence[float],
    *,
    tol_w: float = CLOSURE_TOL_W,
    min_parent_w: float = CONTAINMENT_MIN_PARENT_W,
) -> tuple[float, int, float]:
    """Fraction of instants where ``parent - accounted`` is not negative.

    ``accounted`` is the parent's own directly measured loads plus the proposed
    child feed.  Returns ``(fraction, comparable_instants, median_remainder_w)``.

    An instant is only comparable when *both* the parent and the accounted sum
    are finite.  A parent container's own-load series legitimately contains
    ``None`` where none of its loads had a sample yet, and adding that as zero
    would invent a load -- so the instant is skipped, never substituted.
    """
    if len(parent) != len(accounted):
        raise ValueError("series must have the same length")
    ok = 0
    comparable = 0
    remainders: list[float] = []
    for p, a in zip(parent, accounted, strict=True):
        if not _finite(p) or not _finite(a):
            continue
        p = float(p)
        a = float(a)
        if p < min_parent_w:
            continue
        comparable += 1
        remainder = p - a
        remainders.append(remainder)
        if remainder >= -tol_w:
            ok += 1
    if comparable == 0:
        return 0.0, 0, 0.0
    remainders.sort()
    return ok / comparable, comparable, remainders[len(remainders) // 2]


# --------------------------------------------------------------------------- #
# Measurement nodes
# --------------------------------------------------------------------------- #
def build_nodes(channels: Mapping[str, Mapping[str, Any]]) -> dict[str, MeasurementNode]:
    """Group CTs that observe the same conductors into measurement nodes.

    Two groupings are recognised:

    ``declared_pair``
        Reciprocal ``paired_ct_id`` links.  The user has stated that these two
        CTs are the legs of one double-pole circuit, so they are one meter point
        by declaration.

    ``name_stem``
        Two ``main`` CTs whose ids differ only in a trailing ``_l1`` / ``_l2``.
        Discovery publishes a split-phase panel feed as exactly this shape, and
        the same ``name_derived`` grouping is already used for double-pole loads.
        It is still an *assumption*, so the node records it and every candidate
        that depends on it says so.

    Everything else is a node of one member.
    """
    member_to_node: dict[str, str] = {}
    nodes: dict[str, MeasurementNode] = {}

    def add(members: Sequence[str], grouping: str) -> None:
        ordered = tuple(sorted(members))
        node_id = "+".join(ordered)
        first = channels[ordered[0]]
        nodes[node_id] = MeasurementNode(
            node_id=node_id,
            members=ordered,
            ct_type=str(first.get("ct_type") or "unknown"),
            grouping=grouping,
            phase=str(first.get("phase") or "unknown"),
        )
        for member in ordered:
            member_to_node[member] = node_id

    # Declared pairs first: they are facts, and they win over name grouping.
    for ct_id in sorted(channels):
        if ct_id in member_to_node:
            continue
        paired = str(channels[ct_id].get("paired_ct_id") or "")
        if paired and paired in channels and str(channels[paired].get("paired_ct_id") or "") == ct_id:
            add([ct_id, paired], "declared_pair")

    # Split-phase panel legs, by id stem.  Only when exactly two legs exist.
    stems: dict[str, list[str]] = {}
    for ct_id in sorted(channels):
        if ct_id in member_to_node:
            continue
        if str(channels[ct_id].get("ct_type") or "") != "main":
            continue
        match = _LEG_SUFFIX.match(ct_id)
        if match:
            stems.setdefault(match.group("stem"), []).append(ct_id)
    for stem in sorted(stems):
        legs = stems[stem]
        if len(legs) == 2:
            add(legs, "name_stem")

    for ct_id in sorted(channels):
        if ct_id not in member_to_node:
            add([ct_id], "single")

    return nodes


def _node_series(
    nodes: Mapping[str, MeasurementNode],
    series: Mapping[str, Sequence[float]],
) -> dict[str, list[float]]:
    """Sum each node's member series into one watts series.

    A node's members measure the same conductors, so the node reading is their
    sum.  An instant is unknown only when *no* member has a reading; a single
    absent member must not hide the others.
    """
    out: dict[str, list[float]] = {}
    for node_id, node in nodes.items():
        present = [m for m in node.members if m in series]
        if not present:
            continue
        length = len(series[present[0]])
        if any(len(series[m]) != length for m in present):
            raise ValueError("member series must have the same length")
        values: list[float] = []
        for i in range(length):
            total = 0.0
            seen = False
            for member in present:
                raw = series[member][i]
                if _finite(raw):
                    total += float(raw)
                    seen = True
            values.append(total if seen else math.nan)
        out[node_id] = values
    return out


def _node_own_loads(
    node: MeasurementNode,
    direct_loads: Mapping[str, Sequence[float | None]],
) -> list[float | None] | None:
    """Sum a node's members' own directly-measured loads.

    ``direct_loads`` is keyed by container *channel*, because that is how the
    engine groups dedicated CTs.  A node may have two channels, and each may
    hold a different subset of the loads; the node's own-load total is their
    union, which is the sum.
    """
    contributing = [direct_loads[m] for m in node.members if m in direct_loads]
    if not contributing:
        return None
    length = len(contributing[0])
    if any(len(values) != length for values in contributing):
        raise ValueError("direct_load series must have the same length")
    out: list[float | None] = []
    for i in range(length):
        total: float | None = None
        for values in contributing:
            raw = values[i]
            if _finite(raw):
                total = float(raw) + (total or 0.0)
        out.append(total)
    return out


def _declared_pair(a: str, b: str, channels: Mapping[str, Mapping[str, Any]]) -> bool:
    """True when the two channels are the declared legs of one circuit."""
    pa = str(channels.get(a, {}).get("paired_ct_id") or "")
    pb = str(channels.get(b, {}).get("paired_ct_id") or "")
    return (pa and pa == b) or (pb and pb == a)


def _ancestor_of(
    child: str, candidate: str, channels: Mapping[str, Mapping[str, Any]]
) -> bool:
    """True when ``candidate`` is a declared ancestor of ``child``."""
    seen: set[str] = set()
    current = str(channels.get(child, {}).get("parent_ct_id") or "")
    while current and current not in seen:
        if current == candidate:
            return True
        seen.add(current)
        current = str(channels.get(current, {}).get("parent_ct_id") or "")
    return False


def _resolve_alias(cid: str, aliases: Mapping[str, str]) -> str:
    """Follow the alias chain to its canonical channel."""
    seen: set[str] = set()
    while cid in aliases and cid not in seen:
        seen.add(cid)
        cid = aliases[cid]
    return cid


def _leg_matches(parent: MeasurementNode, child_member: str, channels: Mapping[str, Any]) -> str:
    """Pick the parent member on the same declared phase, else the first.

    The phase label is only used to make the *written-back* link readable; it is
    never used as evidence.  Measured containment already decided the level, and
    on the reference installation the phase labels are contradicted by the
    measurements (the inner panel's L2 fits inside the outer panel's L1), so
    trusting them to *reject* a candidate would discard a correct answer.
    """
    phase = str(channels.get(child_member, {}).get("phase") or "unknown")
    for member in parent.members:
        if str(channels.get(member, {}).get("phase") or "unknown") == phase:
            return member
    return parent.members[0]


def infer_nesting(
    channels: Mapping[str, Mapping[str, Any]],
    series: Mapping[str, Sequence[float]],
    *,
    container_ct_ids: Sequence[str] | None = None,
    direct_loads: Mapping[str, Sequence[float | None]] | None = None,
    containment_min: float = CONTAINMENT_MIN,
    excitation_min_ratio: float = EXCITATION_MIN_RATIO,
    closure_min: float = CLOSURE_MIN,
    tol_w: float = CONTAINMENT_TOL_W,
    min_parent_w: float = CONTAINMENT_MIN_PARENT_W,
    report: dict[str, Any] | None = None,
) -> list[NestingCandidate]:
    """Propose nesting and duplicate relationships from aligned measurements.

    ``channels`` maps ct-id to a mapping that may carry ``ct_type``, ``phase``,
    ``paired_ct_id`` and ``parent_ct_id``.

    ``series`` maps ct-id to an equally long, equally indexed sequence of watts.
    Alignment is the caller's responsibility -- comparing readings taken at
    different instants produces a relationship that does not exist.

    ``direct_loads`` optionally maps a *parent* ct-id to the sum of its own
    directly measured loads (not including feeds).  Supplying it enables the
    closure test, which is the only evidence that places a child at a *level*.

    ``report``, when given, is filled with what was considered and rejected, so
    a caller can say "three channels are ambiguous" instead of silently
    returning nothing.  It never influences the returned candidates.

    Only a ``main`` CT is ever proposed as a *child*: a panel feed's level is
    measurable, a load's is not (see the module docstring).  Returns candidates
    sorted for determinism.  Duplicates are reported once per pair, the
    lexicographically larger node aliasing the smaller.
    """
    diagnostics: dict[str, Any] = report if report is not None else {}
    diagnostics.setdefault("ambiguous", [])
    diagnostics.setdefault("suppressed", [])
    diagnostics.setdefault("not_inferable", [])

    candidates: list[NestingCandidate] = []
    ids = [cid for cid in sorted(series) if cid in channels]
    if container_ct_ids is None:
        containers = {
            cid
            for cid in ids
            if str(channels[cid].get("ct_type") or "") in ("main", "branch")
        }
    else:
        containers = {cid for cid in container_ct_ids if cid in series and cid in channels}

    nodes = build_nodes({cid: channels[cid] for cid in ids})
    diagnostics["nodes"] = [node.as_dict() for node in nodes.values()]
    node_series = _node_series(nodes, series)

    # -- duplicates first: they change how every other pair is read ----------
    node_ids = sorted(node_series)
    aliases: dict[str, str] = {}
    duplicate_pairs: list[tuple[str, str, DuplicateEvidence]] = []
    for i, a in enumerate(node_ids):
        node_a = nodes[a]
        for b in node_ids[i + 1 :]:
            node_b = nodes[b]
            if set(node_a.members) & set(node_b.members):
                continue
            # Declared facts are not re-derived.
            if any(_declared_pair(x, y, channels) for x in node_a.members for y in node_b.members):
                continue
            if any(_ancestor_of(x, y, channels) for x in node_a.members for y in node_b.members):
                continue
            if any(_ancestor_of(y, x, channels) for x in node_a.members for y in node_b.members):
                continue
            evidence = duplicate_evidence(node_series[a], node_series[b])
            if evidence is None:
                continue
            if evidence.activity_ratio < DUPLICATE_ACTIVITY_RATIO_MIN:
                # Both nodes are idle most of the time.  Two near-zero readings
                # agree about nothing.
                continue
            if evidence.rel_diff > DUPLICATE_REL_DIFF_MAX:
                continue
            if evidence.ratio > DUPLICATE_RATIO_MAX:
                # One reports a consistent fraction of the other: a circuit
                # multiplier, not the same wire.
                continue
            if not (set(node_a.members) & containers or set(node_b.members) & containers):
                # Two smart plugs on one appliance is not a topology question.
                continue
            canonical, alias = sorted((a, b))
            duplicate_pairs.append((canonical, alias, evidence))
            aliases[alias] = canonical

    for canonical, alias, evidence in duplicate_pairs:
        root = _resolve_alias(canonical, aliases)
        aliases[alias] = root
        parent_node = nodes[root]
        for member in nodes[alias].members:
            candidates.append(
                NestingCandidate(
                    child_ct_id=member,
                    parent_ct_id=_leg_matches(parent_node, member, channels),
                    kind="duplicate",
                    evidence={
                        "activity_ratio": round(evidence.activity_ratio, 4),
                        "median_relative_difference": round(evidence.rel_diff, 4),
                        "magnitude_ratio": round(evidence.ratio, 3),
                        "median_w": [
                            round(evidence.median_a_w, 1),
                            round(evidence.median_b_w, 1),
                        ],
                        "joint_instants": evidence.joint_instants,
                        "child_node": alias,
                        "parent_node": root,
                        "reason": (
                            "both channels are active together whenever either is, "
                            "and report the same magnitude: one wire measured twice, "
                            "not two feeds"
                        ),
                    },
                )
            )

    def canonical_node(node_id: str) -> str:
        return _resolve_alias(node_id, aliases)

    # -- containment: a panel feed that never exceeds a container -------------
    # Every *container* node may be a parent.  Only a ``main`` node may be a
    # child, because that is the only level measurement can establish.
    canonical_containers = sorted({canonical_node(n) for n in node_ids if set(nodes[n].members) & containers})
    proposals: dict[str, list[tuple[str, float, int]]] = {}
    for child in node_ids:
        child_node = nodes[child]
        if child_node.ct_type not in _NESTABLE_CHILD_TYPES:
            diagnostics["not_inferable"].append(
                {
                    "ct_id": child_node.members[0] if child_node.members else child,
                    "node_id": child,
                    "reason": "load_level_not_measurable",
                }
            )
            continue
        if any(str(channels.get(m, {}).get("parent_ct_id") or "") for m in child_node.members):
            # A declared parent is already a fact.  Re-proposing the same link
            # adds nothing, and proposing a different one would contradict it.
            diagnostics["suppressed"].append(
                {
                    "ct_id": child_node.members[0],
                    "node_id": child,
                    "reason": "parent_already_declared",
                }
            )
            continue
        child_values = [float(v) for v in node_series[child] if _finite(v)]
        peak = max(child_values) if child_values else 0.0
        if peak <= 0.0:
            diagnostics["suppressed"].append(
                {"ct_id": child_node.members[0], "node_id": child, "reason": "never_active"}
            )
            continue

        for parent in canonical_containers:
            if parent == canonical_node(child):
                continue
            parent_series = node_series.get(parent)
            if parent_series is None or len(parent_series) != len(node_series[child]):
                continue
            fraction, comparable = containment(
                parent_series, node_series[child], tol_w=tol_w, min_outer_w=min_parent_w
            )
            if comparable == 0 or fraction < containment_min:
                continue
            parent_values = [float(v) for v in parent_series if _finite(v)]
            parent_peak = max(parent_values) if parent_values else 0.0
            if parent_peak <= 0.0 or peak / parent_peak < excitation_min_ratio:
                # An idle circuit is contained by everything; that is not
                # evidence of being fed by it.
                continue
            proposals.setdefault(child, []).append((parent, fraction, comparable))

    # -- place each child at a level, using closure where it exists ----------
    for child in sorted(proposals):
        child_node = nodes[child]
        child_series = node_series[child]
        supporting: list[dict[str, Any]] = []
        for parent, fraction, comparable in proposals[child]:
            parent_node = nodes[parent]
            own = _node_own_loads(parent_node, direct_loads or {})
            closure: tuple[float, int, float] | None = None
            if own is not None and len(own) == len(child_series):
                accounted = [
                    (None if not _finite(o) or not _finite(c) else float(o) + float(c))
                    for o, c in zip(own, child_series, strict=True)
                ]
                closure = closure_fraction(node_series[parent], accounted)
            if closure is not None and closure[1] and closure[0] < closure_min:
                # The parent's own loads plus this child over-explain the
                # parent, so the child cannot be inside it.
                diagnostics["suppressed"].append(
                    {
                        "ct_id": child_node.members[0],
                        "node_id": child,
                        "reason": "closure_rejected",
                        "parent_node": parent,
                        "closure_fraction": round(closure[0], 4),
                    }
                )
                continue
            supporting.append(
                {
                    "parent": parent,
                    "containment": fraction,
                    "instants": comparable,
                    "closure": closure,
                }
            )

        if not supporting:
            continue

        def rank(row: dict[str, Any]) -> tuple[float, int, str]:
            closure = row["closure"]
            if closure is None or not closure[1]:
                return (float("inf"), 0, row["parent"])
            return (closure[2], -closure[1], row["parent"])

        ranked = sorted(supporting, key=rank)
        best = ranked[0]
        closure = best["closure"]
        parent_node = nodes[best["parent"]]

        if len(ranked) > 1 and rank(best)[0] == float("inf"):
            # Several parents fit and nothing distinguishes them: report the
            # options instead of picking one.  A coin flip is not evidence.
            diagnostics["ambiguous"].append(
                {
                    "ct_id": child_node.members[0],
                    "node_id": child,
                    "candidate_parents": sorted(row["parent"] for row in ranked),
                    "candidate_parent_ct_ids": sorted(
                        _leg_matches(nodes[row["parent"]], child_node.members[0], channels)
                        for row in ranked
                    ),
                    "reason": "no closure evidence to choose a level",
                }
            )
            continue

        evidence: dict[str, Any] = {
            "containment": round(best["containment"], 4),
            "instants": best["instants"],
            "child_node": child,
            "parent_node": best["parent"],
            "reason": (
                "child stays inside the parent at every comparable instant and "
                "carries enough load for that to be evidence rather than idling"
            ),
        }
        if closure is not None and closure[1]:
            evidence["closure_fraction"] = round(closure[0], 4)
            evidence["closure_instants"] = closure[1]
            evidence["closure_median_remainder_w"] = round(closure[2], 1)
        if child_node.is_assumed or parent_node.is_assumed:
            evidence["assumed_grouping"] = sorted(
                {
                    node.grouping
                    for node in (child_node, parent_node)
                    if node.is_assumed
                }
            )
        if len(ranked) > 1:
            evidence["other_containers"] = sorted(row["parent"] for row in ranked[1:])
            evidence["containment_ambiguous"] = True

        for member in child_node.members:
            candidates.append(
                NestingCandidate(
                    child_ct_id=member,
                    parent_ct_id=_leg_matches(parent_node, member, channels),
                    kind="nested",
                    evidence=dict(evidence),
                )
            )

    candidates.sort(key=lambda c: (c.kind, c.child_ct_id, c.parent_ct_id))
    return candidates


__all__ = [
    "ACTIVITY_MIN_W",
    "CLOSURE_MIN",
    "CLOSURE_TOL_W",
    "CONTAINMENT_MIN",
    "CONTAINMENT_MIN_PARENT_W",
    "CONTAINMENT_TOL_W",
    "DUPLICATE_ACTIVITY_RATIO_MIN",
    "DUPLICATE_REL_DIFF_MAX",
    "DUPLICATE_RATIO_MAX",
    "DuplicateEvidence",
    "EXCITATION_MIN_RATIO",
    "MeasurementNode",
    "NestingCandidate",
    "build_nodes",
    "closure_fraction",
    "containment",
    "duplicate_evidence",
    "infer_nesting",
]
