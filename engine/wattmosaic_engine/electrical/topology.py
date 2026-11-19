"""Electrical topology graph: containment, validation, pairing, phases.

Terminology
-----------
* **Main CT** – an incoming line CT (utility service or a panel feed).
* **Branch CT** – measures a circuit that contains other loads/CTs.
* **Containment** – ``child.parent_ct_id == parent``.  A parent's measurement
  physically includes every descendant.  That is *not* double counting as long
  as totals are never produced by adding a parent row and a child row together.

Double-counting rule implemented here
-------------------------------------
A device is *attributed* using exactly one **most specific** CT: its deepest
enclosing branch CT.  Ancestor branch
rows do not receive a second copy of the device's power; instead they carry the
child CT in :attr:`TopologyGraph.nested_channel_ids` and the child's measured
value is subtracted explicitly at solve time (:meth:`MeasurementMatrix.residualise`).
If that child reading is unavailable the ancestor row is excluded with a
documented reason rather than silently double counted.

Main rows are an exception: they legitimately contain every device below them
(that is the physical total), because the total is computed from per-device
values exactly once, never from a sum of CT rows.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from wattmosaic_engine.contracts import (
    CTType,
    EdgeLifecycle,
    Phase,
    TopologyRelation,
)
from wattmosaic_engine.electrical._declared_shapes import (
    ChannelDecl,
    DeclaredTree,
    DeviceDecl,
    EdgeState,
)
from wattmosaic_engine.electrical.factors import FactorRegistry, scale_reading


def __getattr__(name: str) -> Any:
    """Re-export ``detect_configuration_changes`` from its new T7 owner.

    The detector moved to :mod:`wattmosaic_engine.electrical.topology_change` in
    T7; this lazy shim keeps the old import path working until Z2.  It is lazy
    because :mod:`topology_change` itself imports from this module, so a
    module-level import would be circular.
    """
    if name == "detect_configuration_changes":
        from .topology_change import detect_configuration_changes

        return detect_configuration_changes
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass(frozen=True, slots=True)
class Discipline:
    """Resolved electrical placement of one device."""

    device_id: str
    phase: Phase
    double_pole: bool
    most_specific_ct_id: str | None
    enclosing_branch_ct_ids: tuple[str, ...]
    enclosing_main_ct_ids: tuple[str, ...]
    topology_status: EdgeState

    @property
    def is_confirmed(self) -> bool:
        return self.topology_status == EdgeState.CONFIRMED


@dataclass(slots=True)
class TopologyGraph:
    """Validated, indexed view over a :class:`DeclaredTree` contract."""

    topology: DeclaredTree
    phase_split_l1: float = 0.5
    #: Per-sample factor semantics (contract 1.6.0).  When supplied, scaling is
    #: resolved against the interval that covers the sample's timestamp, and a
    #: monitor-applied factor is *not* applied again.  ``None`` keeps the legacy
    #: ``measurement_multiplier`` scalar behaviour for every existing caller.
    factors: FactorRegistry | None = None
    problems: list[str] = field(default_factory=list, init=False)
    channel_ids: list[str] = field(default_factory=list, init=False)
    device_ids: list[str] = field(default_factory=list, init=False)
    children: dict[str, list[str]] = field(default_factory=dict, init=False)
    parent_of: dict[str, str | None] = field(default_factory=dict, init=False)
    paired_with: dict[str, str] = field(default_factory=dict, init=False)
    double_pole_pairs: list[tuple[str, str]] = field(default_factory=list, init=False)
    _ancestors: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _descendants: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _channels: dict[str, ChannelDecl] = field(default_factory=dict, init=False)
    _devices: dict[str, DeviceDecl] = field(default_factory=dict, init=False)
    _nested_mains_seen: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if not 0.0 < self.phase_split_l1 < 1.0:
            raise ValueError("phase_split_l1 must be strictly between 0 and 1")
        self._index()

    # ------------------------------------------------------------------ #
    # Construction / validation
    # ------------------------------------------------------------------ #
    def _index(self) -> None:
        self.problems = []
        self._nested_mains_seen = []
        cmap: dict[str, ChannelDecl] = {}
        for ch in self.topology.channels:
            if ch.ct_id in cmap:
                self.problems.append(f"duplicate CT id: {ch.ct_id}")
                continue
            cmap[ch.ct_id] = ch
        dmap: dict[str, DeviceDecl] = {}
        for dev in self.topology.devices:
            if dev.device_id in dmap:
                self.problems.append(f"duplicate device id: {dev.device_id}")
                continue
            dmap[dev.device_id] = dev

        self._channels = cmap
        self._devices = dmap
        self.channel_ids = list(cmap)
        self.device_ids = list(dmap)
        self.children = {cid: [] for cid in cmap}
        self.parent_of = {}

        for cid, ch in cmap.items():
            if ch.parent_ct_id is None:
                self.parent_of[cid] = None
                continue
            if ch.parent_ct_id not in cmap:
                self.problems.append(
                    f"CT {cid} references unknown parent_ct_id {ch.parent_ct_id}"
                )
                self.parent_of[cid] = None
                continue
            if ch.parent_ct_id == cid:
                self.problems.append(f"CT {cid} is its own parent")
                self.parent_of[cid] = None
                continue
            self.parent_of[cid] = ch.parent_ct_id
            self.children[ch.parent_ct_id].append(cid)

        self._detect_cycles()

        # Ancestor chains, nearest first.
        self._ancestors = {}
        for cid in cmap:
            chain: list[str] = []
            seen = {cid}
            cur = self.parent_of.get(cid)
            while cur is not None:
                if cur in seen:  # pragma: no cover - cycle already reported
                    break
                chain.append(cur)
                seen.add(cur)
                cur = self.parent_of.get(cur)
            self._ancestors[cid] = tuple(chain)

        self._descendants = {}
        for cid in cmap:
            out: list[str] = []
            stack = list(self.children.get(cid, ()))
            seen = {cid}
            while stack:
                cur = stack.pop(0)
                if cur in seen:
                    continue
                seen.add(cur)
                out.append(cur)
                stack.extend(self.children.get(cur, ()))
            self._descendants[cid] = tuple(out)

        self._index_pairs()
        self._validate_channels()
        self._validate_devices()

    def _detect_cycles(self) -> None:
        state: dict[str, int] = {}

        def visit(node: str, trail: list[str]) -> None:
            state[node] = 1
            trail.append(node)
            for child in self.children.get(node, ()):
                if state.get(child) == 1:
                    cycle = " -> ".join([*trail[trail.index(child) :], child])
                    msg = f"cycle in CT topology: {cycle}"
                    if msg not in self.problems:
                        self.problems.append(msg)
                elif state.get(child, 0) == 0:
                    visit(child, trail)
            trail.pop()
            state[node] = 2

        for cid in list(self._channels):
            if state.get(cid, 0) == 0:
                visit(cid, [])

    def _index_pairs(self) -> None:
        pairs: list[tuple[str, str]] = []
        seen: set[frozenset[str]] = set()
        for cid, ch in self._channels.items():
            partner = ch.paired_ct_id
            if not partner:
                continue
            if partner not in self._channels:
                self.problems.append(f"CT {cid} references unknown paired_ct_id {partner}")
                continue
            if partner == cid:
                self.problems.append(f"CT {cid} is paired with itself")
                continue
            other = self._channels[partner]
            if other.paired_ct_id != cid:
                self.problems.append(
                    f"CT pairing is not symmetric: {cid} -> {partner} but "
                    f"{partner} -> {other.paired_ct_id}"
                )
            key = frozenset((cid, partner))
            if key not in seen:
                seen.add(key)
                pairs.append((cid, partner))
            self.paired_with[cid] = partner
        self.double_pole_pairs = pairs

    def _validate_channels(self) -> None:
        for cid, ch in self._channels.items():
            if ch.phase == Phase.UNKNOWN:
                self.problems.append(
                    f"CT {cid} has unknown phase; phase-dependent rows are treated as BOTH"
                )
            if ch.ct_type == CTType.MAIN and ch.parent_ct_id is not None:
                # A main with a parent is a *nested* feed (utility -> panel, or
                # panel -> sub-panel).  This is a supported shape: the engine
                # never sums a nested main, it resolves the outermost feeds.
                # See ``TopologyGraph.effective_feeds``.
                self._nested_mains_seen.append(ch.ct_id)
            if ch.paired_ct_id and ch.phase == Phase.BOTH:
                self.problems.append(
                    f"CT {cid} is paired as one leg of a double-pole load but is marked BOTH"
                )
            if ch.paired_ct_id and ch.phase not in (Phase.L1, Phase.L2, Phase.UNKNOWN):
                self.problems.append(f"CT {cid} paired leg must be L1 or L2")

    def _validate_devices(self) -> None:
        for did, dev in self._devices.items():
            for cid in dev.branch_ct_ids:
                ch = self._channels.get(cid)
                if ch is None:
                    self.problems.append(f"device {did} references unknown branch CT {cid}")
                    continue
                if ch.ct_type == CTType.MAIN:
                    self.problems.append(
                        f"device {did} lists main CT {cid} as a branch CT"
                    )
            if dev.double_pole and dev.phase in (Phase.L1, Phase.L2):
                self.problems.append(
                    f"double-pole device {did} cannot have phase {dev.phase}"
                )
            if dev.phase == Phase.BOTH and not dev.double_pole:
                self.problems.append(
                    f"device {did} has phase BOTH but double_pole=False; "
                    "set double_pole=True for a 240 V load"
                )
            if dev.parent_device_id and dev.parent_device_id not in self._devices:
                self.problems.append(
                    f"device {did} references unknown parent device {dev.parent_device_id}"
                )
            # Most specific CT must exist if any branch CT is declared.
            if dev.branch_ct_ids and self.most_specific_branch_ct(did) is None:
                self.problems.append(
                    f"device {did} declares branch CTs but none are usable/enabled"
                )

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    @property
    def channels(self) -> dict[str, ChannelDecl]:
        return self._channels

    @property
    def devices(self) -> dict[str, DeviceDecl]:
        return self._devices

    def channel(self, ct_id: str) -> ChannelDecl | None:
        return self._channels.get(ct_id)

    def device(self, device_id: str) -> DeviceDecl | None:
        return self._devices.get(device_id)

    def ancestors(self, ct_id: str) -> tuple[str, ...]:
        """Ancestor CTs, nearest first (excluding ``ct_id`` itself)."""
        return self._ancestors.get(ct_id, ())

    def descendants(self, ct_id: str) -> tuple[str, ...]:
        return self._descendants.get(ct_id, ())

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return ancestor in self._ancestors.get(descendant, ())

    def is_enabled(self, ct_id: str) -> bool:
        ch = self._channels.get(ct_id)
        return bool(ch and ch.enabled)

    def main_channels(self) -> list[ChannelDecl]:
        return [c for c in self._channels.values() if c.ct_type == CTType.MAIN and c.enabled]

    def branch_channels(self) -> list[ChannelDecl]:
        return [c for c in self._channels.values() if c.ct_type == CTType.BRANCH and c.enabled]

    def most_specific_branch_group(self, device_id: str) -> tuple[str, ...]:
        """The deepest set of branch CTs that observe the device.

        Normally one CT.  For a double-pole device whose two legs are measured
        by a symmetric pair (``paired_ct_id``) the group contains both legs, so
        neither half of the load is lost.
        """
        dev = self._devices.get(device_id)
        if dev is None:
            return ()
        candidates = [
            cid
            for cid in dev.branch_ct_ids
            if self.is_enabled(cid) and cid in self._channels
        ]
        if not candidates:
            return ()
        # Prefer the deepest CTs that are not ancestors of another candidate.
        deepest = [
            cid
            for cid in candidates
            if not any(
                other != cid and self.is_ancestor(cid, other) for other in candidates
            )
        ]
        pool = deepest or candidates
        ordered = sorted(pool)
        if dev.double_pole:
            # Keep both legs of a verified pair -- but only when the partner is
            # genuinely this device's other leg.  ``ordered`` is already scoped
            # to this device's own branch CTs, so a pair pointing outside it is
            # a cross-device pairing and is left alone.
            legs = [cid for cid in ordered if self.double_pole_leg(cid) in ordered]
            if legs:
                return tuple(legs)
        return (ordered[0],)

    def most_specific_branch_ct(self, device_id: str) -> str | None:
        group = self.most_specific_branch_group(device_id)
        return group[0] if group else None

    def complete_leg_pair(self, device_id: str) -> tuple[str, ...]:
        """Both legs of a verified pair that together observe the whole device.

        Empty unless the device is a double-pole (``BOTH``) load whose
        attribution group is exactly two CTs that are each other's
        ``paired_ct_id`` partner *and* both belong to this device
        (:meth:`double_pole_leg` already enforces ownership and enablement).
        In that case the device is fully measured: whatever the physical split,
        ``y_leg1 + y_leg2`` is its total, so no row may be scaled by a *declared*
        split -- see :meth:`phase_coefficient`.
        """
        if self.device_phase(device_id) != Phase.BOTH:
            return ()
        group = self.attribution_ct_ids(device_id)
        if len(group) != 2:
            return ()
        first, second = group
        if self.double_pole_leg(first) != second or self.double_pole_leg(second) != first:
            return ()
        return group

    def double_pole_leg(self, ct_id: str) -> str | None:
        """The other leg of *this same device*, or ``None``.

        ``paired_ct_id`` is documented as "the other leg of a single double-pole
        load", but the field is free text and nothing upstream enforces it: two
        independent circuits whose CTs happen to be named ``..._ct1``/``..._ct2``
        get paired by the discovery heuristic, and a user can pair any two
        channels by hand.  Summing such a pair makes **each** device report the
        other's power as well, so every consumer of the pair must check that the
        partner actually belongs to the same device before treating it as a leg.

        A partner is only a leg when it exists, is enabled, and names the same
        ``device_id`` (both ``None`` counts as the same, for branch pairs that
        have no device yet).
        """
        channel = self._channels.get(ct_id)
        if channel is None or not channel.paired_ct_id:
            return None
        partner = self._channels.get(channel.paired_ct_id)
        if partner is None or not self.is_enabled(partner.ct_id):
            return None
        if partner.device_id != channel.device_id:
            return None
        return partner.ct_id

    def attribution_ct_ids(self, device_id: str) -> tuple[str, ...]:
        """CTs that carry this device's power *once* (its measurement rows)."""
        return self.most_specific_branch_group(device_id)

    def resolve(self, device_id: str) -> Discipline:
        """Resolve the device's measurement placement (cached by caller)."""
        dev = self._devices.get(device_id)
        if dev is None:
            return Discipline(
                device_id=device_id,
                phase=Phase.UNKNOWN,
                double_pole=False,
                most_specific_ct_id=None,
                enclosing_branch_ct_ids=(),
                enclosing_main_ct_ids=(),
                topology_status=EdgeState.REJECTED,
            )
        group = self.attribution_ct_ids(device_id)
        most_specific = group[0] if group else None
        branch_group = self.most_specific_branch_group(device_id)
        probe = tuple(dict.fromkeys([*group, *branch_group]))
        enclosing_branch: list[str] = []
        enclosing_main: list[str] = []
        for cid in probe:
            for anc in (cid, *self.ancestors(cid)):
                ch = self._channels.get(anc)
                if ch is None:
                    continue
                if ch.ct_type == CTType.BRANCH and anc not in enclosing_branch:
                    enclosing_branch.append(anc)
                elif ch.ct_type == CTType.MAIN and anc not in enclosing_main:
                    enclosing_main.append(anc)
        # The device's declared branch CTs are themselves enclosing branch CTs.
        for cid in branch_group:
            if cid not in enclosing_branch:
                enclosing_branch.insert(0, cid)
        return Discipline(
            device_id=device_id,
            phase=Phase(dev.phase),
            double_pole=bool(dev.double_pole),
            most_specific_ct_id=most_specific,
            enclosing_branch_ct_ids=tuple(enclosing_branch),
            enclosing_main_ct_ids=tuple(enclosing_main),
            topology_status=EdgeState(dev.topology_status),
        )

    def nested_channel_ids(self, ct_id: str) -> tuple[str, ...]:
        """Direct child CTs whose values are measured separately.

        A parent row must subtract these explicitly; otherwise the device power
        under the child would be counted in both rows.
        """
        return tuple(
            cid for cid in self.children.get(ct_id, ()) if self.is_enabled(cid)
        )

    def channel_phase(self, ct_id: str) -> Phase:
        ch = self._channels.get(ct_id)
        return Phase(ch.phase) if ch else Phase.UNKNOWN

    def device_phase(self, device_id: str) -> Phase:
        """The declared phase of a device, else the one its own CTs establish.

        ``DeviceDecl.phase`` is the strongest statement, but it is frequently
        left ``UNKNOWN`` -- discovery never guesses it, and the wizard makes it
        optional.  The channel a device is measured by does state its own leg,
        and that is a declaration about the same conductor, so it is evidence
        for the device rather than a guess:

        * every attribution CT on one leg -> that leg;
        * attribution CTs on both legs -> ``BOTH`` (the pair is the device);
        * anything else (unknown labels, or no attribution CT) -> ``UNKNOWN``.

        Deriving here rather than at each call site keeps the coefficient, the
        matrix and the reporting consistent.
        """
        dev = self._devices.get(device_id)
        if dev is None:
            return Phase.UNKNOWN
        phase = Phase(dev.phase)
        if phase == Phase.UNKNOWN and dev.double_pole:
            return Phase.BOTH
        if phase != Phase.UNKNOWN:
            return phase
        legs: set[Phase] = set()
        for cid in self.attribution_ct_ids(device_id):
            ch = self._channels.get(cid)
            if ch is None:
                continue
            declared = Phase(ch.phase)
            if declared in (Phase.L1, Phase.L2):
                legs.add(declared)
            elif declared == Phase.BOTH:
                legs.update({Phase.L1, Phase.L2})
        if legs == {Phase.L1}:
            return Phase.L1
        if legs == {Phase.L2}:
            return Phase.L2
        if legs == {Phase.L1, Phase.L2}:
            return Phase.BOTH
        return Phase.UNKNOWN

    def leg_coefficient(self, device_id: str, row_phase: Phase | str) -> float:
        """Share of a ``BOTH`` device's power on one side of a leg pair.

        A *complete* pair is fully observed: each leg row sees its own real
        share, so the two equations ``y_L1 = ½x`` and ``y_L2 = ½x`` sum to
        ``x = y_L1 + y_L2`` exactly, whatever the physical asymmetry.  That is
        why the complete case returns ``0.5`` and never the declared split -- a
        declared ``α`` there would scale two readings that are already complete
        (least squares would report ``P·(αs+(1-α)(1-s))/(α²+(1-α)²)``, e.g.
        ``0.862P`` at ``α=0.7`` on a 0.5/0.5 pair, instead of ``P``).

        Only when the device is *not* fully observed -- one leg of a pair, or a
        single-line row -- does the declared split have to apportion it, because
        the row then measures a fraction nobody stated.
        """
        row = Phase(row_phase)
        if self.complete_leg_pair(device_id):
            return 1.0 if row == Phase.BOTH else 0.5
        if row == Phase.L1:
            return self.phase_split_l1
        return 1.0 - self.phase_split_l1

    def phase_coefficient(
        self, device_id: str, row_phase: Phase | str, *, solo: bool = False
    ) -> float:
        """Fraction of a device's total power that appears on ``row_phase``.

        * single-phase device on its own line -> ``1.0``
        * single-phase device on a ``BOTH`` (sum) row -> ``1.0``
        * double-pole device on a ``BOTH`` row -> ``1.0`` (total power)
        * double-pole device whose two legs are both measured (a complete,
          verified pair) -> ``0.5`` per leg, independent of
          ``phase_split_l1``: the pair is already a complete observation, so
          summing the rows recovers the total exactly and the declared split
          must not perturb them
        * double-pole device only partly observed (one leg, or a single line)
          -> ``phase_split_l1`` for L1 and ``1 - phase_split_l1`` for L2
          (default 0.5/0.5, configurable); there the split is the only way to
          recover the unmeasured part
        * a device whose phase nobody declared: ``1.0`` when the row is *its own*
          measurement (``solo``), because there the CT is an observation of the
          whole device and halving it would invent a missing leg; otherwise the
          split, because a shared row's total has to be apportioned somehow.

        The split is only applied when the topology declares the device as
        double-pole / BOTH.  A single-phase device is never arbitrarily split
        across L1 and L2.
        """
        row = Phase(row_phase)
        dev_phase = self.device_phase(device_id)
        if row == Phase.UNKNOWN:
            row = Phase.BOTH
        if dev_phase == Phase.BOTH:
            if row == Phase.BOTH:
                return 1.0
            if row in (Phase.L1, Phase.L2):
                return self.leg_coefficient(device_id, row)
            return 0.0
        if dev_phase == Phase.UNKNOWN:
            # Nobody stated the placement.  On the device's own row the CT is a
            # direct observation of it, so the whole reading belongs to the
            # device -- halving it would fabricate a second leg.  On a
            # containing row the total must be apportioned, and the declared
            # split is the only honest denominator available.
            if solo:
                return 1.0
            return 1.0 if row == Phase.BOTH else self.phase_split_l1
        if row == Phase.BOTH:
            return 1.0
        return 1.0 if row == dev_phase else 0.0

    def device_channels(self, device_id: str) -> tuple[str, ...]:
        """All CTs that physically observe the device (enclosing)."""
        d = self.resolve(device_id)
        out: list[str] = []
        out.extend(d.enclosing_branch_ct_ids)
        out.extend(d.enclosing_main_ct_ids)
        return tuple(dict.fromkeys(out))

    # ------------------------------------------------------------------ #
    # Validation shortcuts
    # ------------------------------------------------------------------ #
    def validate(self) -> list[str]:
        return list(self.problems)

    def is_consistent(self) -> bool:
        return not self.problems

    def unresolved_devices(self) -> list[str]:
        """Devices with no enclosing branch/main CT."""
        out = []
        for did in self.device_ids:
            d = self.resolve(did)
            if not d.enclosing_branch_ct_ids and not d.enclosing_main_ct_ids:
                out.append(did)
        return out

    def nested_mains(self) -> list[tuple[str, str]]:
        """Pairs ``(inner, outer)`` where one main CT contains another.

        Retained for diagnostics and for callers that want to *report* nesting.
        Nesting is no longer an error: :meth:`effective_feeds` resolves it.
        """
        mains = {c.ct_id for c in self.main_channels()}
        pairs: list[tuple[str, str]] = []
        for inner in mains:
            for anc in self.ancestors(inner):
                if anc in mains:
                    pairs.append((inner, anc))
        return pairs

    def multiplier_for(self, ct_id: str, timestamp: datetime | None = None) -> float:
        """The multiplier the engine must apply to one reading of a channel.

        Defaults to 1.0.  Two sources exist and they are not interchangeable:

        * a :class:`FactorRegistry` supplied at construction.  The factor in
          force **at ``timestamp``** decides, and a monitor-applied factor
          resolves to 1.0 because the published value already includes it.
          Without a ``timestamp`` the registry cannot answer honestly (a factor
          change makes "now" ambiguous), so the declared *current* scalar is
          used -- exactly the legacy behaviour.
        * otherwise ``ChannelDecl.measurement_multiplier``.

        The multiplier is *never* inferred from ``phase`` or ``ct_type``: the
        device commonly applies the circuit multiplier itself and already
        publishes the full load, so inferring it would double-count.
        """
        ch = self._channels.get(ct_id)
        if self.factors is not None and timestamp is not None:
            return self.factors.applied_multiplier_at(ct_id, timestamp)
        if ch is None:
            return 1.0
        return float(ch.measurement_multiplier or 1.0)

    def scale_w(
        self,
        ct_id: str,
        value: float | None,
        timestamp: datetime | None = None,
    ) -> float | None:
        """Apply the factor in force to one raw reading.

        Readings are only ever scaled on the way out of this method.  Nothing
        writes the scaled value back into a window or a reading object, so the
        multiplier cannot be applied twice to the same measurement.  With a
        registry the decision is delegated to
        :func:`wattmosaic_engine.electrical.factors.scale_reading`, which keeps
        that "once, on the read path" invariant in one place.
        """
        if value is None:
            return None
        if self.factors is not None and timestamp is not None:
            scaled, _, _ = scale_reading(value, ct_id, timestamp, self.factors)
            return scaled
        m = self.multiplier_for(ct_id)
        return value if m == 1.0 else value * m

    def canonical_channel_id(self, ct_id: str) -> str:
        """Resolve ``alias_of_ct_id`` chains to the channel that owns the wire."""
        seen: set[str] = set()
        current = ct_id
        while True:
            ch = self._channels.get(current)
            target = ch.alias_of_ct_id if ch is not None else None
            if not target or target not in self._channels or target in seen:
                return current
            seen.add(current)
            current = target

    def alias_channels(self) -> list[ChannelDecl]:
        """Channels that measure a wire another channel already measures."""
        return [c for c in self._channels.values() if c.alias_of_ct_id is not None]

    def effective_feeds(self) -> list[str]:
        """The CTs that legitimately define the whole-home total.

        A feed is a ``main`` CT whose *shadow* carries no other main CT: if a
        main sits under another main (any depth), the inner one is already
        counted by the outer and must not be added again.  Aliased channels are
        the same wire measured twice and only the canonical one contributes.

        This is what makes an arbitrarily deep panel tree safe to sum: only the
        topmost main of each independent branch of the tree contributes, and
        parallel feeds at that level are still summed.

        Only enabled channels participate, and only the *canonical* channel of an
        alias chain contributes: an alias is the same wire measured twice.  The
        result is sorted for determinism.
        """
        feeds: list[str] = []
        for ch in self.main_channels():
            if not ch.enabled:
                continue
            # Resolve the whole alias chain, not just one hop: ``a -> b -> c``
            # must contribute once, as ``c``.
            if self.canonical_channel_id(ch.ct_id) != ch.ct_id:
                continue
            # A main nested under another main is already inside that total.
            has_main_ancestor = any(
                anc in self._channels
                and self._channels[anc].ct_type == CTType.MAIN
                and self.is_enabled(anc)
                for anc in self.ancestors(ch.ct_id)
            )
            if not has_main_ancestor:
                feeds.append(ch.ct_id)
        return sorted(feeds)

    def feed_channels(self) -> list[ChannelDecl]:
        """:meth:`effective_feeds` as ``ChannelDecl`` objects, in the same order."""
        return [self._channels[cid] for cid in self.effective_feeds() if cid in self._channels]


def build_graph(
    topology: DeclaredTree,
    *,
    phase_split_l1: float = 0.5,
    factors: FactorRegistry | None = None,
) -> TopologyGraph:
    return TopologyGraph(topology, phase_split_l1=phase_split_l1, factors=factors)


# --------------------------------------------------------------------------- #
# The versioned edge set (spec 04 §11; API contract 09 §7.2)
# --------------------------------------------------------------------------- #
#
# ``TopologyVersion`` in ``contracts.py`` is the fixed *wire* shape: an opaque
# ``version_id``, a ``version_number``, ``parent_version_id``, the ordered
# ``edge_ids`` and metadata.  This module owns the computation that produces it
# and the diff between two of them.  Nothing here stores rows -- ``storage`` owns
# the table -- so the same functions serve a live pass, a stored read and a test.

#: Fields of a ``TopologyRelation`` that participate in a content diff.
#: ``relation_id`` is excluded because it is the identity being compared; the
#: clock fields are excluded because a new assertion written later about the same
#: endpoints is not a change to what was asserted.
_RELATION_DIFF_FIELDS = (
    "kind",
    "subject_id",
    "object_id",
    "status",
    "lifecycle",
    "confidence",
    "evidence",
    "requires_confirmation",
    "declared_by",
)

#: The three states of one edge in a diff.  A plain string, not a new enum: the
#: vocabulary is fixed by the API spec's ``TopologyDiff`` and a new member would
# have to be minted there first.
DIFF_ADDED = "added"
DIFF_REMOVED = "removed"
DIFF_CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class EdgeView:
    """One edge of a version, reduced to exactly what a diff compares.

    Deliberately not ``TopologyRelation``: a diff must not depend on the whole
    relation object (its clocks, its free-form ``evidence`` prose), only on the
    comparable content.  ``id`` is carried separately from ``content`` so two
    relations with the same endpoints but different ids cannot accidentally
    compare equal.
    """

    id: str
    kind: str
    subject_id: str
    object_id: str
    status: str
    lifecycle: str
    confidence: float | None
    evidence: Mapping[str, Any]
    requires_confirmation: bool
    declared_by: str

    @property
    def key(self) -> tuple[str, str, str]:
        """The endpoints and kind that identify this edge within a version."""
        return (self.kind, self.subject_id, self.object_id)

    @classmethod
    def from_relation(cls, relation: TopologyRelation) -> EdgeView:
        return cls(
            id=str(relation.relation_id),
            kind=_enum_text(relation.kind),
            subject_id=str(relation.subject_id),
            object_id=str(relation.object_id),
            status=_enum_text(relation.status),
            lifecycle=_enum_text(getattr(relation, "lifecycle", EdgeLifecycle.CANDIDATE)),
            confidence=None if relation.confidence is None else float(relation.confidence),
            evidence=dict(relation.evidence or {}),
            requires_confirmation=bool(relation.requires_confirmation),
            declared_by=str(relation.declared_by),
        )

    def content(self) -> tuple[Any, ...]:
        """The comparable content, in a stable field order."""
        return (
            self.kind,
            self.subject_id,
            self.object_id,
            self.status,
            self.lifecycle,
            self.confidence,
            _stable_json(self.evidence),
            self.requires_confirmation,
            self.declared_by,
        )


@dataclass(frozen=True, slots=True)
class EdgeChange:
    """One edge present in both versions whose comparable content moved."""

    edge_id: str
    kind: str
    subject_id: str
    object_id: str
    changed_fields: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "object_id": self.object_id,
            "changed_fields": list(self.changed_fields),
        }


@dataclass(frozen=True, slots=True)
class TopologyVersionRecord:
    """A computed topology version: identity, time, status, parent and diff.

    Satisfies the ``TopologyVersion`` contract of ``09 §7.2`` field for field:
    ``version_id``, ``version_number``, ``parent_version_id``, ``status``,
    ``created_at``, ``created_by``, ``change_event_id``, ``rollback_of``,
    ``summary`` and ``edge_ids``.  Two members are additive, because the task
    requires them and the contract has no such field:

    * ``edges`` -- the complete edge set, so a previous version stays readable
      from its own row without a join;
    * ``diff`` -- the comparison against ``parent_version_id``, so a consumer
      never has to re-derive it.

    ``edge_count`` mirrors the contract's redundant count.
    """

    version_id: str
    version_number: int
    parent_version_id: str | None
    status: str
    created_at: datetime
    created_by: str
    summary: str
    #: The complete edge set of this version, ordered by ``edge_id``.
    edges: tuple[EdgeView, ...] = ()
    #: The diff against :attr:`parent_version_id`; ``None`` = no parent.
    diff: TopologyDiff | None = None
    change_event_id: str | None = None
    rollback_of: str | None = None

    @property
    def edge_ids(self) -> tuple[str, ...]:
        return tuple(edge.id for edge in self.edges)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    def edge_set(self) -> dict[str, EdgeView]:
        """The edge set indexed by ``edge_id``."""
        return {edge.id: edge for edge in self.edges}


@dataclass(frozen=True, slots=True)
class TopologyDiff:
    """The added / removed / changed edges between two versions.

    The wire shape of ``TopologyDiff`` in ``09 §8.1`` minus the two endpoint
    ``TopologyVersion`` objects, which the route supplies from its own lookups.
    """

    added: tuple[EdgeView, ...] = ()
    removed: tuple[EdgeView, ...] = ()
    changed: tuple[EdgeChange, ...] = ()

    @property
    def added_count(self) -> int:
        return len(self.added)

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    @property
    def changed_count(self) -> int:
        return len(self.changed)

    @property
    def changed_fields(self) -> tuple[str, ...]:
        """Sorted union of every field name that differed across ``changed``."""
        names: set[str] = set()
        for change in self.changed:
            names.update(change.changed_fields)
        return tuple(sorted(names))

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "added_edges": [_view_dict(edge) for edge in self.added],
            "removed_edges": [_view_dict(edge) for edge in self.removed],
            "changed_edges": [change.as_dict() for change in self.changed],
            "changed_fields": list(self.changed_fields),
            "added_count": self.added_count,
            "removed_count": self.removed_count,
            "changed_count": self.changed_count,
        }


def version_id_for(version_number: int) -> str:
    """Opaque ``tv-<zero-padded sequence>``; never reused after a rollback."""
    return f"tv-{int(version_number):08d}"


def version_token(value: int | str | None) -> str | None:
    """Canonical comparison token for a topology version identifier.

    The engine spells one version three ways: inference records carry an ``int``
    (``contracts.InferenceRecord.topology_version``), version records and change
    events carry an opaque ``tv-<padded>`` id, and mapping cells carry either as
    a string.  Normalising them to one token lets reconciliation decide which
    records a change invalidated **without rewriting any stored value**.  ``None``
    stays ``None`` so "not stated" is never confused with a real version.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return version_id_for(value)
    text = str(value).strip()
    if text.isdigit():
        return version_id_for(int(text))
    if text.startswith("tv-") and text[3:].isdigit():
        return version_id_for(int(text[3:]))
    return text


def version_number(value: int | str | None) -> int | None:
    """The integer behind a version identifier, or ``None`` if it has none.

    Inverse of :func:`version_token` for the storage read path, which queries the
    existing integer ``inference_records.topology_version`` column.  No schema
    change: an unrecognised id simply matches no row.
    """
    token = version_token(value)
    if token is None:
        return None
    if token.startswith("tv-") and token[3:].isdigit():
        return int(token[3:])
    if token.isdigit():
        return int(token)
    return None


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _stable_json(value: Any) -> str:
    """Deterministic JSON for a free-form mapping, so equal content hashes equal."""
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _edge_key(relation: TopologyRelation) -> tuple[str, str, str]:
    return (
        _enum_text(relation.kind),
        str(relation.subject_id),
        str(relation.object_id),
    )


def diff_topologies(
    old: DeclaredTree,
    new: DeclaredTree,
) -> TopologyDiff:
    """Diff the typed relation set of two topologies.

    Deterministic: edges are compared by ``(kind, subject, object)``, each side
    is indexed once, and every output is sorted by that key.  Two runs over the
    same inputs produce byte-identical output.
    """
    old_edges = {_edge_key(rel): EdgeView.from_relation(rel) for rel in old.relations}
    new_edges = {_edge_key(rel): EdgeView.from_relation(rel) for rel in new.relations}

    added: list[EdgeView] = []
    removed: list[EdgeView] = []
    changed: list[EdgeChange] = []

    for key in sorted(set(old_edges) | set(new_edges)):
        before = old_edges.get(key)
        after = new_edges.get(key)
        if before is None and after is not None:
            added.append(after)
        elif after is None and before is not None:
            removed.append(before)
        elif before is not None and after is not None and before.content() != after.content():
            fields = tuple(
                name
                for name, left, right in zip(
                    _RELATION_DIFF_FIELDS, before.content(), after.content(), strict=True
                )
                if left != right
            )
            changed.append(
                EdgeChange(
                    edge_id=after.id,
                    kind=key[0],
                    subject_id=key[1],
                    object_id=key[2],
                    changed_fields=fields,
                )
            )

    return TopologyDiff(
        added=tuple(sorted(added, key=lambda e: e.key)),
        removed=tuple(sorted(removed, key=lambda e: e.key)),
        changed=tuple(sorted(changed, key=lambda e: (e.kind, e.subject_id, e.object_id))),
    )


def _view_dict(view: EdgeView) -> dict[str, Any]:
    return {
        "edge_id": view.id,
        "kind": view.kind,
        "subject_id": view.subject_id,
        "object_id": view.object_id,
        "status": view.status,
        "lifecycle": view.lifecycle,
        "confidence": view.confidence,
        "evidence": dict(view.evidence),
        "requires_confirmation": view.requires_confirmation,
        "declared_by": view.declared_by,
    }


def make_version(
    *,
    version_number: int,
    topology: DeclaredTree,
    status: EdgeState | None = None,
    parent: TopologyVersionRecord | None = None,
    parent_topology: DeclaredTree | None = None,
    created_at: datetime | None = None,
    created_by: str = "",
    summary: str = "",
    change_event_id: str | None = None,
    rollback_of: str | None = None,
) -> TopologyVersionRecord:
    """Build one version record from a topology and its predecessor.

    The edge set is taken from ``topology.relations`` ordered by ``relation_id``
    (the contract's ordering).  The diff is computed against ``parent_topology``
    when supplied; otherwise it is derived from the parent record's edge set via
    :func:`diff_edge_sets`.
    """
    created = created_at or datetime.now(UTC)
    edges = tuple(
        EdgeView.from_relation(rel)
        for rel in sorted(topology.relations, key=lambda rel: str(rel.relation_id))
    )
    diff: TopologyDiff | None = None
    if parent is not None:
        if parent_topology is not None:
            diff = diff_topologies(parent_topology, topology)
        else:
            diff = diff_edge_sets(parent.edge_set(), {edge.id: edge for edge in edges})
    return TopologyVersionRecord(
        version_id=version_id_for(int(version_number)),
        version_number=int(version_number),
        parent_version_id=None if parent is None else parent.version_id,
        status=_enum_text(status if status is not None else topology.status),
        created_at=created,
        created_by=created_by,
        summary=summary,
        change_event_id=change_event_id,
        rollback_of=rollback_of,
        edges=edges,
        diff=diff,
    )


def diff_edge_sets(
    old: Mapping[str, EdgeView],
    new: Mapping[str, EdgeView],
) -> TopologyDiff:
    """Diff two indexed edge sets by ``edge_id``.

    Used when only the stored edge lists are available (a historical version
    read back from the payload table), where the endpoints are not re-derivable
    from a ``DeclaredTree`` object.  ``old``/``new`` map ``edge_id -> EdgeView``.
    """
    added: list[EdgeView] = []
    removed: list[EdgeView] = []
    changed: list[EdgeChange] = []
    for edge_id in sorted(set(old) | set(new)):
        before = old.get(edge_id)
        after = new.get(edge_id)
        if before is None and after is not None:
            added.append(after)
        elif after is None and before is not None:
            removed.append(before)
        elif before is not None and after is not None and before.content() != after.content():
            changed.append(
                EdgeChange(
                    edge_id=edge_id,
                    kind=after.kind,
                    subject_id=after.subject_id,
                    object_id=after.object_id,
                    changed_fields=tuple(
                        name
                        for name, left, right in zip(
                            _RELATION_DIFF_FIELDS,
                            before.content(),
                            after.content(),
                            strict=True,
                        )
                        if left != right
                    ),
                )
            )
    return TopologyDiff(
        added=tuple(sorted(added, key=lambda e: e.key)),
        removed=tuple(sorted(removed, key=lambda e: e.key)),
        changed=tuple(sorted(changed, key=lambda e: (e.kind, e.subject_id, e.object_id))),
    )


def digest_edge_set(relations: Sequence[TopologyRelation]) -> str:
    """Content digest of an ordered edge set, for cheap equality checks.

    Uses the same comparable content as :func:`diff_topologies`, so two versions
    with different ``relation_id`` spellings of the same assertions hash equal --
    which is the point: a re-stamped id is not a topology change.
    """
    payload = [
        [
            _enum_text(rel.kind),
            str(rel.subject_id),
            str(rel.object_id),
            _enum_text(rel.status),
            _enum_text(getattr(rel, "lifecycle", EdgeLifecycle.CANDIDATE)),
            None if rel.confidence is None else round(float(rel.confidence), 6),
            _stable_json(dict(rel.evidence or {})),
            bool(rel.requires_confirmation),
            str(rel.declared_by),
        ]
        for rel in sorted(relations, key=lambda rel: (str(rel.relation_id)))
    ]
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def edge_views_from_payload(payload: Mapping[str, Any]) -> dict[str, EdgeView]:
    """Rebuild the indexed edge set of a stored version payload.

    Accepts either the relation-shaped rows a ``DeclaredTree`` payload carries or
    the ``EdgeView``-shaped rows the version store writes, so both forms read
    through one path.
    """
    out: dict[str, EdgeView] = {}
    for row in payload.get("relations") or []:
        relation_id = str(row.get("relation_id") or row.get("edge_id") or "")
        if not relation_id:
            continue
        out[relation_id] = EdgeView(
            id=relation_id,
            kind=str(row.get("kind") or ""),
            subject_id=str(row.get("subject_id") or ""),
            object_id=str(row.get("object_id") or ""),
            status=str(row.get("status") or "candidate"),
            lifecycle=str(row.get("lifecycle") or "candidate"),
            confidence=(
                None
                if row.get("confidence") is None
                else float(row["confidence"])
            ),
            evidence=dict(row.get("evidence") or {}),
            requires_confirmation=bool(row.get("requires_confirmation", True)),
            declared_by=str(row.get("declared_by") or "user"),
        )
    return out


#: Every name this module publicly exports.  ``__all__`` is the module's public
#: surface, so a name that was exported before this one existed must stay in it:
#: a ``from ... import *`` or a static check that trusts ``__all__`` must not
#: silently lose ``Discipline``, ``TopologyGraph`` or ``build_graph``.  The
#: imported re-exports are listed too, because they are public names of this
#: module exactly as they were before ``__all__`` was introduced.
__all__ = [
    # pre-existing public surface (unchanged by the versioned-edge-set work)
    "ChannelDecl",
    "CTType",
    "DeviceDecl",
    "Discipline",
    "FactorRegistry",
    "Phase",
    "DeclaredTree",
    "TopologyGraph",
    "EdgeState",
    "annotations",
    "build_graph",
    "dataclass",
    "datetime",
    "field",
    "scale_reading",
    # the versioned edge set (spec 04 §11; API contract 09 §7.2/§8.1)
    "DIFF_ADDED",
    "DIFF_CHANGED",
    "DIFF_REMOVED",
    "EdgeChange",
    "EdgeView",
    "TopologyDiff",
    "TopologyVersionRecord",
    "detect_configuration_changes",  # noqa: F822 - provided via __getattr__
    "diff_edge_sets",
    "diff_topologies",
    "digest_edge_set",
    "edge_views_from_payload",
    "make_version",
    "version_id_for",
    "version_number",
    "version_token",
]
