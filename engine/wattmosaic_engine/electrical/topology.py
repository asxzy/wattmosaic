"""Electrical topology graph: containment, validation, pairing, phases.

Terminology
-----------
* **Main CT** – an incoming line CT (utility service or a panel feed).
* **Branch CT** – measures a circuit that contains other loads/CTs.
* **Dedicated CT** – measures exactly one logical device and nothing else.
* **Containment** – ``child.parent_ct_id == parent``.  A parent's measurement
  physically includes every descendant.  That is *not* double counting as long
  as totals are never produced by adding a parent row and a child row together.

Double-counting rule implemented here
-------------------------------------
A device is *attributed* using exactly one **most specific** CT: its dedicated
CT when verified, otherwise its deepest enclosing branch CT.  Ancestor branch
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

from dataclasses import dataclass, field

from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    Topology,
    TopologyStatus,
)


@dataclass(frozen=True, slots=True)
class Discipline:
    """Resolved electrical placement of one device."""

    device_id: str
    phase: Phase
    double_pole: bool
    dedicated_ct_id: str | None
    most_specific_ct_id: str | None
    enclosing_branch_ct_ids: tuple[str, ...]
    enclosing_main_ct_ids: tuple[str, ...]
    topology_status: TopologyStatus

    @property
    def has_dedicated_ct(self) -> bool:
        return self.dedicated_ct_id is not None

    @property
    def is_confirmed(self) -> bool:
        return self.topology_status == TopologyStatus.CONFIRMED


@dataclass(slots=True)
class TopologyGraph:
    """Validated, indexed view over a :class:`Topology` contract."""

    topology: Topology
    phase_split_l1: float = 0.5
    problems: list[str] = field(default_factory=list, init=False)
    channel_ids: list[str] = field(default_factory=list, init=False)
    device_ids: list[str] = field(default_factory=list, init=False)
    children: dict[str, list[str]] = field(default_factory=dict, init=False)
    parent_of: dict[str, str | None] = field(default_factory=dict, init=False)
    paired_with: dict[str, str] = field(default_factory=dict, init=False)
    double_pole_pairs: list[tuple[str, str]] = field(default_factory=list, init=False)
    _ancestors: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _descendants: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _channels: dict[str, CTChannel] = field(default_factory=dict, init=False)
    _devices: dict[str, DeviceTopology] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not 0.0 < self.phase_split_l1 < 1.0:
            raise ValueError("phase_split_l1 must be strictly between 0 and 1")
        self._index()

    # ------------------------------------------------------------------ #
    # Construction / validation
    # ------------------------------------------------------------------ #
    def _index(self) -> None:
        self.problems = []
        cmap: dict[str, CTChannel] = {}
        for ch in self.topology.channels:
            if ch.ct_id in cmap:
                self.problems.append(f"duplicate CT id: {ch.ct_id}")
                continue
            cmap[ch.ct_id] = ch
        dmap: dict[str, DeviceTopology] = {}
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
            if ch.ct_type == CTType.DEDICATED_DEVICE:
                if not ch.device_id:
                    self.problems.append(
                        f"dedicated CT {cid} has no device_id; it cannot be attributed"
                    )
                elif ch.device_id not in self._devices:
                    self.problems.append(
                        f"dedicated CT {cid} references unknown device {ch.device_id}"
                    )
                else:
                    dev = self._devices[ch.device_id]
                    if dev.dedicated_ct_ids and cid not in dev.dedicated_ct_ids:
                        self.problems.append(
                            f"dedicated CT {cid} for device {ch.device_id} is missing from "
                            "DeviceTopology.dedicated_ct_ids"
                        )
            if ch.phase == Phase.UNKNOWN:
                self.problems.append(
                    f"CT {cid} has unknown phase; phase-dependent rows are treated as BOTH"
                )
            if ch.ct_type == CTType.MAIN and ch.parent_ct_id is not None:
                self.problems.append(
                    f"CT {cid} is type=main but has a parent CT {ch.parent_ct_id} "
                    "(nested mains cannot be summed)"
                )
            if ch.paired_ct_id and ch.phase == Phase.BOTH:
                self.problems.append(
                    f"CT {cid} is paired as one leg of a double-pole load but is marked BOTH"
                )
            if ch.paired_ct_id and ch.phase not in (Phase.L1, Phase.L2, Phase.UNKNOWN):
                self.problems.append(f"CT {cid} paired leg must be L1 or L2")

    def _validate_devices(self) -> None:
        for did, dev in self._devices.items():
            for cid in dev.dedicated_ct_ids:
                ch = self._channels.get(cid)
                if ch is None:
                    self.problems.append(f"device {did} references unknown dedicated CT {cid}")
                    continue
                if ch.ct_type != CTType.DEDICATED_DEVICE:
                    self.problems.append(
                        f"device {did} dedicated CT {cid} has type {ch.ct_type}, "
                        "not dedicated_device"
                    )
                if ch.device_id and ch.device_id != did:
                    self.problems.append(
                        f"dedicated CT {cid} is assigned to device {ch.device_id}, "
                        f"not {did}"
                    )
            for cid in dev.branch_ct_ids:
                ch = self._channels.get(cid)
                if ch is None:
                    self.problems.append(f"device {did} references unknown branch CT {cid}")
                    continue
                if ch.ct_type == CTType.MAIN:
                    self.problems.append(
                        f"device {did} lists main CT {cid} as a branch CT"
                    )
                if ch.ct_type == CTType.DEDICATED_DEVICE and cid not in dev.dedicated_ct_ids:
                    self.problems.append(
                        f"device {did} lists dedicated CT {cid} as a branch CT but "
                        "does not declare it dedicated"
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
    def channels(self) -> dict[str, CTChannel]:
        return self._channels

    @property
    def devices(self) -> dict[str, DeviceTopology]:
        return self._devices

    def channel(self, ct_id: str) -> CTChannel | None:
        return self._channels.get(ct_id)

    def device(self, device_id: str) -> DeviceTopology | None:
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

    def main_channels(self) -> list[CTChannel]:
        return [c for c in self._channels.values() if c.ct_type == CTType.MAIN and c.enabled]

    def branch_channels(self) -> list[CTChannel]:
        return [c for c in self._channels.values() if c.ct_type == CTType.BRANCH and c.enabled]

    def dedicated_channel_for(self, device_id: str) -> CTChannel | None:
        dev = self._devices.get(device_id)
        if dev is None:
            return None
        for cid in dev.dedicated_ct_ids:
            ch = self._channels.get(cid)
            if ch and ch.enabled and ch.ct_type == CTType.DEDICATED_DEVICE:
                return ch
        # Reverse lookup: a dedicated CT may point at the device.
        for ch in self._channels.values():
            if (
                ch.enabled
                and ch.ct_type == CTType.DEDICATED_DEVICE
                and ch.device_id == device_id
            ):
                return ch
        return None

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
            # Keep both legs of a verified pair.
            legs = [
                cid
                for cid in ordered
                if self._channels[cid].paired_ct_id in ordered
            ]
            if legs:
                return tuple(legs)
        return (ordered[0],)

    def most_specific_branch_ct(self, device_id: str) -> str | None:
        group = self.most_specific_branch_group(device_id)
        return group[0] if group else None

    def attribution_ct_ids(self, device_id: str) -> tuple[str, ...]:
        """CTs that carry this device's power *once* (its measurement rows)."""
        dedicated = self.dedicated_channel_for(device_id)
        if dedicated is not None:
            out = [dedicated.ct_id]
            partner = dedicated.paired_ct_id
            if partner and partner in self._channels and self.is_enabled(partner):
                out.append(partner)
            return tuple(out)
        return self.most_specific_branch_group(device_id)

    def resolve(self, device_id: str) -> Discipline:
        """Resolve the device's measurement placement (cached by caller)."""
        dev = self._devices.get(device_id)
        if dev is None:
            return Discipline(
                device_id=device_id,
                phase=Phase.UNKNOWN,
                double_pole=False,
                dedicated_ct_id=None,
                most_specific_ct_id=None,
                enclosing_branch_ct_ids=(),
                enclosing_main_ct_ids=(),
                topology_status=TopologyStatus.REJECTED,
            )
        dedicated = self.dedicated_channel_for(device_id)
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
            dedicated_ct_id=dedicated.ct_id if dedicated else None,
            most_specific_ct_id=most_specific,
            enclosing_branch_ct_ids=tuple(enclosing_branch),
            enclosing_main_ct_ids=tuple(enclosing_main),
            topology_status=TopologyStatus(dev.topology_status),
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
        dev = self._devices.get(device_id)
        if dev is None:
            return Phase.UNKNOWN
        phase = Phase(dev.phase)
        if phase == Phase.UNKNOWN and dev.double_pole:
            return Phase.BOTH
        return phase

    def phase_coefficient(self, device_id: str, row_phase: Phase | str) -> float:
        """Fraction of a device's total power that appears on ``row_phase``.

        * single-phase device on its own line -> ``1.0``
        * single-phase device on a ``BOTH`` (sum) row -> ``1.0``
        * double-pole device on a ``BOTH`` row -> ``1.0`` (total power)
        * double-pole device on a single line -> ``phase_split_l1`` for L1 and
          ``1 - phase_split_l1`` for L2 (default 0.5/0.5, configurable)

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
            if row == Phase.L1:
                return self.phase_split_l1
            if row == Phase.L2:
                return 1.0 - self.phase_split_l1
            return 0.0
        if dev_phase == Phase.UNKNOWN:
            # Unknown placement: model as a BOTH device so no energy is lost,
            # but flag it during validation.
            return 1.0 if row == Phase.BOTH else self.phase_split_l1
        if row == Phase.BOTH:
            return 1.0
        return 1.0 if row == dev_phase else 0.0

    def device_channels(self, device_id: str) -> tuple[str, ...]:
        """All CTs that physically observe the device (dedicated + enclosing)."""
        d = self.resolve(device_id)
        out: list[str] = []
        if d.dedicated_ct_id:
            out.append(d.dedicated_ct_id)
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
        """Devices with no dedicated CT and no enclosing branch/main CT."""
        out = []
        for did in self.device_ids:
            d = self.resolve(did)
            if not d.enclosing_branch_ct_ids and not d.enclosing_main_ct_ids:
                out.append(did)
        return out

    def nested_mains(self) -> list[tuple[str, str]]:
        """Pairs ``(inner, outer)`` where one main CT contains another."""
        mains = {c.ct_id for c in self.main_channels()}
        pairs: list[tuple[str, str]] = []
        for inner in mains:
            for anc in self.ancestors(inner):
                if anc in mains:
                    pairs.append((inner, anc))
        return pairs


def build_graph(topology: Topology, *, phase_split_l1: float = 0.5) -> TopologyGraph:
    return TopologyGraph(topology, phase_split_l1=phase_split_l1)
