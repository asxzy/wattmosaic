"""Read-time hierarchy view: panels → circuits → devices (contract v1 §1).

Why this module exists
----------------------
The topology contract stores *measurements and wiring facts*: a channel, its
declared parent, its phase, its device.  It does not store a rendering.  For
years the panel had to guess the grouping from entity-id prefixes, which is how
it silently produced a wrong tree (a branch appearing under a panel it is not
fed by, a device repeated twice, a duplicate feed rendered as a fake nesting
level).

This module computes one authoritative tree from declared data only:

* ``CTChannel.panel_id`` — which board a channel sits in (additive contract
  field, written by discovery from the monitor it discovered);
* ``CTChannel.parent_ct_id`` — a declared wiring fact, never invented here;
* ``CTChannel.alias_of_ct_id`` — "this CT measures the same conductor as that
  one", i.e. a duplicate, which is an **alias, not a child**;
* ``DeviceTopology.dedicated_ct_ids`` / ``branch_ct_ids`` — attribution.

Nothing here writes to the topology.  ``parent_ct_id`` is only read.

Panel derivation (declared, then two documented legacy fallbacks)
-----------------------------------------------------------------
1. ``panel_id`` / ``panel_name`` when present (the normal case).
2. Legacy topologies stored before those fields existed: the discovery layer
   already wrote the board name into ``metadata.evidence`` as ``"panel=<name>"``.
   That string is trusted *once*, here, and turned into the same slug discovery
   uses for ``ct_id``.  It is the board the engine itself discovered, not a guess
   from a friendly name.
3. A channel that declares neither inherits the panel of the nearest declared
   ancestor through ``parent_ct_id`` (a declared parent is authoritative), and
   otherwise joins one implicit panel named **Installation** when, and only
   when, the topology declares no panels at all.  If at least one panel *is*
   declared, such a channel is surfaced under an ``Unattributed`` group with
   ``stats.unplaced_circuits`` — a fallback never silently invents a board.

Duplicate feeds
---------------
``refoss_main_panel_l1/l2`` is the same conductor as ``main_load_manager_l1/l2``
(two monitors on one service entrance).  A duplicate is an alias: merging it
must not create a nesting level.  Panels whose *feeds* alias each other are
merged into one node whose ``feed_ct_ids`` lists both (contract §1.4) and whose
badges say ``"2 monitors"``.  The canonical panel — the one owning the alias
targets, i.e. the CTs the rest of the engine sums — supplies the node id and
label; the merged name is recorded in ``notes``.

Nothing in this module touches the database, the clock beyond ``utcnow()`` for
``generated_at``, numpy or Home Assistant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from wattmosaic_engine.contracts import CTChannel, DeviceTopology, Topology, utcnow

#: Schema version of the tree payload (contract §1.1).
TREE_VERSION = 2

#: Label of the implicit panel used when a topology declares no panel at all.
IMPLICIT_PANEL_ID = "implicit"
IMPLICIT_PANEL_NAME = "Installation"

#: Status tone vocabulary shared by every node badge.
_STATUS_TONE = {"candidate": "warn", "confirmed": "ok", "rejected": "bad"}

#: ``Source`` values -> the frozen measurement vocabulary (contract §5.2).
#:
#: ``measured``/``inferred``/``prior`` are the three ways the engine can honestly
#: *hold* a number, and ``none`` is the absence of one.  ``model_estimated`` maps
#: to ``prior`` because the engine already grades the two identically:
#: ``disaggregation/tiers.py`` returns ``TIER_1_MODELLED`` for both
#: ``MODEL_ESTIMATED`` and ``CONFIGURED_PRIOR``, i.e. "a stored model or an
#: assertion supplied this value; nothing live measured it".  ``prior`` is that
#: tier's word in the UI vocabulary.  Any other source -- including ``unknown``
#: -- is ``none``.
_MEASUREMENT_BY_SOURCE = {
    "direct_measured": "measured",
    "inferred": "inferred",
    "configured_prior": "prior",
    "model_estimated": "prior",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str | None:
    """Enum/None-safe string (contracts store enums as plain ``str`` already)."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _slug(name: str) -> str:
    """Same slug rule as ``ingestion.discovery._panel_prefix`` (kept in sync)."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return cleaned or "panel"


def _legacy_panel_name(channel: CTChannel) -> str | None:
    """Board name discovery recorded in ``metadata.evidence`` before panel_id.

    Only used for topologies stored before the additive ``panel_id`` field
    existed; new topologies always carry the field.
    """
    metadata = channel.metadata if isinstance(channel.metadata, Mapping) else {}
    for item in metadata.get("evidence") or ():
        text = str(item)
        if text.startswith("panel="):
            name = text[len("panel=") :].strip()
            if name:
                return name
    return None


def _node_id(kind: str, identifier: str) -> str:
    return f"{kind}:{identifier}"


def _group_has_content(children: Iterable[Any]) -> bool:
    """Whether a ``CIRCUITS``/``DEVICES``/``SUBPANELS`` node should exist (R5).

    A group with no children must be omitted entirely.  Factored out so a test
    can monkeypatch it and prove the omission is what removes the empty node.
    """
    return any(True for _ in children)


def _break_parent_cycles(parent_of: dict[str, str | None], *, kind: str) -> list[str]:
    """Make a ``child -> parent`` map acyclic, in place, and explain each break.

    A malformed topology (a parent chain that loops, e.g. A's parent is B and
    B's parent is A) must never hang the walker or recurse forever.  Every node
    is walked at most once per chain, and the offending edge is severed so the
    loop becomes an ordinary root.  Returns human-readable notes.

    Factored out so a test can disable it and prove the guard is load-bearing.
    """
    notes: list[str] = []
    for start in sorted(parent_of):
        seen = [start]
        current = parent_of.get(start)
        while current is not None:
            if current in seen:
                broken = seen[seen.index(current)]
                parent_of[broken] = None
                notes.append(
                    f"{kind} nesting loop detected ("
                    + " → ".join(seen + [current])
                    + f"); edge broken at {broken!r}"
                )
                break
            if current not in parent_of:
                break
            seen.append(current)
            current = parent_of.get(current)
    return notes


def _result_field(result: Any, name: str) -> Any:
    if result is None:
        return None
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


def _result_power(result: Any) -> float | None:
    value = _result_field(result, "power_w")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _result_confidence(result: Any) -> float | None:
    value = _result_field(result, "confidence")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _measurement_of(result: Any) -> str:
    """The engine's measurement claim for one device result (contract §5.2).

    ``none`` is returned for a missing result and for ``source == "unknown"``.
    That is what makes a placeholder zero unrepresentable: an ``unknown`` result
    carries ``power_w = 0.0`` for residual accounting only (see
    ``DisaggregationEngine._unknown_result``, whose provenance says
    ``power_w_is_placeholder``), so reporting that number as this fixture's
    reading would be a fabricated measurement of zero.

    ``sample_count`` is deliberately **not** a measurement signal here.  On the
    stored contract it is the *model's* learning-sample count
    (``sample_count=0 if model is None else model.sample_count`` in
    ``disaggregation/engine.py``), and the production database has 192k
    ``direct_measured`` rows with ``sample_count=0``; treating a zero there as
    "unmeasured" would mark every real measurement unknown.  The source is the
    authority on how a value was obtained, so the source alone decides.

    A source the vocabulary cannot describe is ``none``: the engine must not
    claim a measurement state it does not have a word for.
    """
    source = _text(_result_field(result, "source"))
    if source is None:
        return "none"
    return _MEASUREMENT_BY_SOURCE.get(source, "none")


class _DisjointSet:
    """Minimal union-find; ``union(child, parent)`` keeps ``parent`` canonical."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        root = item
        seen: set[str] = set()
        while self._parent.get(root, root) != root:
            if root in seen:  # defensive: a malformed union graph must not hang
                break
            seen.add(root)
            root = self._parent[root]
        while self._parent.get(item, item) != root:
            nxt = self._parent[item]
            self._parent[item] = root
            item = nxt
        return root

    def union(self, child: str, parent: str) -> None:
        """Make ``parent``'s root the canonical representative of ``child``."""
        self.add(child)
        self.add(parent)
        child_root = self.find(child)
        parent_root = self.find(parent)
        if child_root != parent_root:
            self._parent[child_root] = parent_root


# --------------------------------------------------------------------------- #
# Intermediate model
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Panel:
    panel_id: str
    name: str
    feeds: set[str] = field(default_factory=set)
    circuit_ids: set[str] = field(default_factory=set)
    monitor_names: set[str] = field(default_factory=set)
    #: Panel id of the board feeding this one (``None`` = service entry / root).
    parent_panel_id: str | None = None


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


class _TreeBuilder:
    def __init__(
        self,
        topology: Topology,
        latest_results: Mapping[str, Any] | None,
        nesting: Any,
    ) -> None:
        self.topology = topology
        self.latest_results: Mapping[str, Any] = latest_results or {}
        self.notes: list[str] = []
        self.channels: dict[str, CTChannel] = {}
        for channel in topology.channels:
            if channel.ct_id in self.channels:
                self.notes.append(f"duplicate channel id {channel.ct_id!r}; the last row wins")
            self.channels[channel.ct_id] = channel
        self.devices: dict[str, DeviceTopology] = {}
        for device in topology.devices:
            if device.device_id in self.devices:
                self.notes.append(f"duplicate device id {device.device_id!r}; the last row wins")
            self.devices[device.device_id] = device

        self.alias_of: dict[str, str] = {}
        for ct_id, channel in self.channels.items():
            if channel.alias_of_ct_id:
                self.alias_of[ct_id] = channel.alias_of_ct_id
        self.nesting_candidates: list[dict[str, Any]] = []
        self.advisory_parents: dict[str, list[str]] = {}
        self.nesting_supplied = nesting is not None
        self._read_nesting(nesting)

        #: channel id -> declared panel (explicit, legacy evidence or inherited).
        self.channel_panel: dict[str, str | None] = {}
        self.panels: dict[str, _Panel] = {}
        self._panel_ds = _DisjointSet()
        self.feed_ids: set[str] = set()
        #: Panels that are fed from outside the installation.  At most one, and
        #: only when the data can actually distinguish it (see
        #: ``_compute_service_entry_ids``).
        self._service_entry_ids: set[str] = set()
        self.unplaced_circuit_ids: list[str] = []
        self.unplaced_device_ids: list[str] = []
        self.circuit_panel: dict[str, str] = {}
        self.circuit_parent: dict[str, str | None] = {}
        self.circuit_children: dict[str, list[str]] = {}
        self.attached_devices: dict[str, list[str]] = {}
        self.panel_level_devices: dict[str, list[str]] = {}
        self.duplicate_feeds = 0

    # -- nesting input ------------------------------------------------------ #
    def _read_nesting(self, nesting: Any) -> None:
        """Accept the ``/topology/nesting`` payload (or a bare candidate list)."""
        if nesting is None:
            return
        candidates: Iterable[Any] = ()
        if isinstance(nesting, Mapping):
            candidates = nesting.get("candidates") or ()
            for child, parent in (nesting.get("candidate_aliases") or {}).items():
                self.alias_of.setdefault(str(child), str(parent))
        elif isinstance(nesting, Iterable) and not isinstance(nesting, (str, bytes)):
            candidates = nesting
        for candidate in candidates:
            kind = _text(_result_field(candidate, "kind"))
            child = _text(_result_field(candidate, "child_ct_id"))
            parent = _text(_result_field(candidate, "parent_ct_id"))
            if not child or not parent:
                continue
            if kind == "duplicate":
                self.alias_of.setdefault(child, parent)
            elif kind == "nested":
                self.advisory_parents.setdefault(child, []).append(parent)
            else:
                continue
            self.nesting_candidates.append(
                {"child_ct_id": child, "parent_ct_id": parent, "kind": kind}
            )

    # -- panel derivation --------------------------------------------------- #
    def _declared_panel(self, channel: CTChannel) -> tuple[str, str, str] | None:
        """``(panel_id, panel_name, source)`` for one channel, or ``None``.

        ``source`` is ``"declared"`` for the additive ``panel_id`` field and
        ``"legacy_evidence"`` for the pre-field fallback, so the tree can say
        which topologies still need re-discovery to gain the real field.
        """
        if channel.panel_id:
            return channel.panel_id, channel.panel_name or channel.panel_id, "declared"
        legacy = _legacy_panel_name(channel)
        if legacy:
            return _slug(legacy), legacy, "legacy_evidence"
        return None

    def _resolve_panel_declarations(self) -> None:
        legacy_channels = 0
        for ct_id, channel in self.channels.items():
            declared = self._declared_panel(channel)
            if declared is not None:
                self.channel_panel[ct_id] = declared[0]
                panel = self.panels.setdefault(declared[0], _Panel(declared[0], declared[1]))
                panel.monitor_names.add(declared[1])
                if channel.ct_type == "main":
                    panel.feeds.add(ct_id)
                else:
                    panel.circuit_ids.add(ct_id)
                self._panel_ds.add(declared[0])
                if declared[2] == "legacy_evidence":
                    legacy_channels += 1
            else:
                self.channel_panel[ct_id] = None
        if legacy_channels:
            # The persisted channel table has no panel column yet, so a topology
            # reloaded from the database reaches us without ``panel_id``.  The
            # grouping discovery wrote into evidence is the same board, so the
            # tree stays correct — but the reader should know the stored field is
            # not yet authoritative for this topology.
            self.notes.append(
                f"{legacy_channels} channel(s) carried no panel_id and were grouped by "
                "the panel evidence discovery recorded; re-run discovery to store the field"
            )

        # Undeclared channels inherit the panel of their nearest declared
        # ancestor.  Declared parents are wiring facts; inheriting them is the
        # only fallback that never contradicts the topology.
        for ct_id in self.channels:
            if self.channel_panel.get(ct_id) is not None:
                continue
            inherited = self._inherited_panel(ct_id)
            self.channel_panel[ct_id] = inherited

        self.feed_ids = {
            ct_id
            for ct_id, panel_id in self.channel_panel.items()
            if panel_id is not None and self.channels[ct_id].ct_type == "main"
        }

        if not self.panels:
            # Legacy fallback: no board was ever declared.  One implicit panel
            # keeps the tree usable without inventing several boards.
            implicit = self.panels.setdefault(
                IMPLICIT_PANEL_ID, _Panel(IMPLICIT_PANEL_ID, IMPLICIT_PANEL_NAME)
            )
            self._panel_ds.add(implicit.panel_id)
            for ct_id, channel in self.channels.items():
                if self.channel_panel.get(ct_id) is not None:
                    continue
                self.channel_panel[ct_id] = IMPLICIT_PANEL_ID
                if channel.ct_type == "main":
                    implicit.feeds.add(ct_id)
                    self.feed_ids.add(ct_id)
                else:
                    implicit.circuit_ids.add(ct_id)
            self.notes.append(
                "no panel declared: one implicit panel "
                f"{IMPLICIT_PANEL_NAME!r} holds every circuit"
            )
        else:
            unknown = [
                ct_id
                for ct_id, panel_id in self.channel_panel.items()
                if panel_id is None and ct_id not in self.feed_ids
            ]
            self.unplaced_circuit_ids = sorted(unknown)
            if unknown:
                self.notes.append(
                    f"{len(unknown)} circuit(s) declare no panel and no declared ancestor; "
                    "surfaced under Unattributed"
                )

        for ct_id, panel_id in self.channel_panel.items():
            if panel_id is None:
                continue
            panel = self.panels.get(panel_id)
            if panel is None:
                continue
            if ct_id in panel.feeds or ct_id in panel.circuit_ids:
                continue
            # A channel attached to a panel it did not declare (inheritance).
            if self.channels[ct_id].ct_type == "main":
                panel.feeds.add(ct_id)
                self.feed_ids.add(ct_id)
            else:
                panel.circuit_ids.add(ct_id)

    def _inherited_panel(self, ct_id: str) -> str | None:
        seen: set[str] = set()
        current = self.channels[ct_id].parent_ct_id
        while current and current not in seen:
            seen.add(current)
            panel_id = self.channel_panel.get(current)
            if panel_id is not None:
                return panel_id
            parent = self.channels.get(current)
            if parent is None:
                return None
            current = parent.parent_ct_id
        if current in seen:
            self.notes.append(
                f"parent chain of {ct_id!r} loops; panel inheritance stopped at {current!r}"
            )
        return None

    # -- duplicate / alias handling ----------------------------------------- #
    def _alias_chain(self, ct_id: str) -> str | None:
        """Follow ``alias_of`` to the canonical CT (cycle-guarded)."""
        seen: set[str] = set()
        current = self.alias_of.get(ct_id)
        while current and current not in seen:
            seen.add(current)
            nxt = self.alias_of.get(current)
            if nxt is None:
                return current
            current = nxt
        if current is not None:
            self.notes.append(f"alias chain starting at {ct_id!r} loops; ignored")
        return None

    def _duplicate_feed_pairs(self) -> list[tuple[str, str, str, str]]:
        """``(alias_ct, alias_panel, canonical_ct, canonical_panel)`` feed pairs.

        Only ``main`` CTs are considered: a *load* that happens to alias another
        load says nothing about whether two boards are one board.  Factored out
        so a test can revert the merge by returning no pairs.
        """
        owner_of_ct: dict[str, str] = {}
        for ct_id, panel_id in self.channel_panel.items():
            if panel_id is not None:
                owner_of_ct[ct_id] = panel_id

        pairs: list[tuple[str, str, str, str]] = []
        for ct_id in sorted(self.feed_ids):
            target = self._alias_chain(ct_id)
            if target is None or target == ct_id:
                continue
            target_owner = owner_of_ct.get(target)
            own_owner = owner_of_ct.get(ct_id)
            if target_owner is None or own_owner is None or target_owner == own_owner:
                continue
            pairs.append((ct_id, own_owner, target, target_owner))
        return pairs

    def _merge_duplicate_panels(self) -> None:
        """Merge panels whose *feeds* alias each other; aliases never nest."""
        aliased_feeds = self._duplicate_feed_pairs()

        for ct_id, own_owner, target, target_owner in aliased_feeds:
            # ``target_owner`` owns the canonical CT, so it stays canonical.
            self._panel_ds.union(own_owner, target_owner)
            self.duplicate_feeds += 1
            self.notes.append(
                f"duplicate feed {ct_id!r} is an alias of {target!r}: "
                f"panel {own_owner!r} merged into {target_owner!r}, not nested"
            )

        # Collapse merged members onto the canonical panel object.
        merged: dict[str, _Panel] = {}
        for panel in self.panels.values():
            root = self._panel_ds.find(panel.panel_id)
            canonical = self.panels.get(root, panel)
            bucket = merged.setdefault(
                root,
                _Panel(
                    panel_id=root,
                    name=canonical.name,
                    monitor_names=set(),
                ),
            )
            bucket.feeds.update(panel.feeds)
            bucket.circuit_ids.update(panel.circuit_ids)
            bucket.monitor_names.update(panel.monitor_names)
        for root, bucket in merged.items():
            bucket.panel_id = root
            bucket.name = self.panels[root].name if root in self.panels else bucket.name
        # Re-key channel ownership and feeds onto the canonical ids.
        for ct_id, panel_id in list(self.channel_panel.items()):
            if panel_id is None:
                continue
            self.channel_panel[ct_id] = self._panel_ds.find(panel_id)
        self.panels = merged
        self.feed_ids = {
            ct_id
            for ct_id, panel_id in self.channel_panel.items()
            if panel_id is not None and self.channels[ct_id].ct_type == "main"
        }

        for panel in sorted(self.panels.values(), key=lambda item: item.panel_id):
            if len(panel.monitor_names) > 1:
                self.notes.append(
                    "panel " + repr(panel.name) + " is measured by "
                    + f"{len(panel.monitor_names)} monitors: "
                    + ", ".join(sorted(panel.monitor_names))
                )

    # -- panel nesting ------------------------------------------------------ #
    def _resolve_panel_parents(self) -> None:
        for panel_id, panel in self.panels.items():
            candidates: set[str] = set()
            for feed_id in sorted(panel.feeds):
                parent_ct = self.channels[feed_id].parent_ct_id
                if not parent_ct:
                    continue
                owner = self.channel_panel.get(parent_ct)
                if owner is None or owner == panel_id:
                    continue
                candidates.add(owner)
            if len(candidates) > 1:
                self.notes.append(
                    f"panel {panel.name!r} has feeds declaring different parents "
                    f"({', '.join(sorted(candidates))}); using {sorted(candidates)[0]!r}"
                )
            panel.parent_panel_id = sorted(candidates)[0] if candidates else None

        parent_of = {pid: panel.parent_panel_id for pid, panel in self.panels.items()}
        self.notes.extend(_break_parent_cycles(parent_of, kind="panel"))
        for panel_id, parent in parent_of.items():
            self.panels[panel_id].parent_panel_id = parent

        self._service_entry_ids = self._compute_service_entry_ids()

    def _compute_service_entry_ids(self) -> set[str]:
        """The board the house hangs off, or nothing when the data does not say.

        A panel is *fed from outside the installation* when none of its feeds
        declares a parent CT -- there is nothing upstream of it.  That test is
        necessary but not sufficient for "service entry", because on a real
        install every ``parent_ct_id`` may be null (discovery never infers
        containment), and then every root passes it.  Claiming three service
        entries is a false statement about the wiring, and it also sorts the
        real main board *last* (roots put service entries first).

        So the claim is made only when exactly one root qualifies.  When several
        do, the data cannot distinguish them and no panel is labelled; the
        subtitle falls back to "distribution board", which claims nothing.  This
        is deliberately conservative: a missing label is recoverable by the user
        reading the tree, a wrong one is not.
        """
        roots = [
            panel_id
            for panel_id, panel in self.panels.items()
            if panel.parent_panel_id is None or panel.parent_panel_id not in self.panels
        ]
        unfed = [
            panel_id
            for panel_id in roots
            if self.panels[panel_id].feeds
            and all(not self.channels[feed].parent_ct_id for feed in self.panels[panel_id].feeds)
        ]
        if len(unfed) == 1:
            return {unfed[0]}
        if len(unfed) > 1:
            self.notes.append(
                f"{len(unfed)} panels are fed from outside the installation "
                f"({', '.join(sorted(unfed))}); none is labelled the service entry"
            )
        return set()

    # -- circuit nesting ---------------------------------------------------- #
    def _resolve_circuits(self) -> None:
        for ct_id in sorted(self.channels):
            if ct_id in self.feed_ids:
                continue  # a panel feed lives on the panel node, never as a circuit
            self.circuit_panel.setdefault(ct_id, self.channel_panel.get(ct_id) or "")

        for ct_id in sorted(self.circuit_panel):
            channel = self.channels[ct_id]
            parent = channel.parent_ct_id
            resolved: str | None = None
            if parent and parent != ct_id and parent in self.circuit_panel:
                if self.circuit_panel[parent] == self.circuit_panel[ct_id]:
                    resolved = parent
                else:
                    self.notes.append(
                        f"circuit {ct_id!r} declares a parent in another panel "
                        f"({parent!r}); shown at panel level"
                    )
            elif parent and parent not in self.channels:
                self.notes.append(
                    f"circuit {ct_id!r} declares missing parent {parent!r}; shown at panel level"
                )
            self.circuit_parent[ct_id] = resolved

        self.notes.extend(_break_parent_cycles(self.circuit_parent, kind="circuit"))

        for ct_id, parent in self.circuit_parent.items():
            if parent is not None:
                self.circuit_children.setdefault(parent, []).append(ct_id)
        for children in self.circuit_children.values():
            children.sort()

    # -- device attribution ------------------------------------------------- #
    def _device_ct_ids(self, device: DeviceTopology) -> list[str]:
        referenced = list(device.dedicated_ct_ids) + list(device.branch_ct_ids)
        referenced.extend(
            ct_id for ct_id, channel in self.channels.items() if channel.device_id == device.device_id
        )
        return sorted({ct_id for ct_id in referenced if ct_id in self.channels})

    def _place_device(
        self, device_id: str, panel_id: str, ct_ids: list[str]
    ) -> tuple[str, str]:
        """Where a device goes: ``("circuit", ct_id)`` or ``("panel", panel_id)``.

        Exactly one placement per device is what guarantees contract R6 (every
        device appears exactly once).  A device attributes to a *single* circuit
        only when it references exactly one non-feed CT of this panel; a
        double-pole device spans two CTs, and no single circuit owns it, so it
        belongs in the panel's ``DEVICES`` group (contract R3).

        Factored into one seam so a test can substitute the naive rule that
        places a device under a circuit *and* at panel level, proving the
        exactly-once test actually detects a regression.
        """
        if len(ct_ids) == 1 and ct_ids[0] not in self.feed_ids:
            circuit_id = ct_ids[0]
            if self.circuit_panel.get(circuit_id) == panel_id:
                return "circuit", circuit_id
        return "panel", panel_id

    def _assign_device(
        self, device_id: str, *, where: str, target: str, panel_id: str
    ) -> None:
        """Record a device in exactly one place.

        This is the single write point for placement: a device is either under
        one circuit or in its panel's ``DEVICES`` group, never both.  Keeping it
        in one helper is what makes the exactly-once guarantee auditable, and it
        gives a negative test a seam to append a second placement and prove the
        tree really does duplicate.
        """
        if where == "circuit":
            self.attached_devices.setdefault(target, []).append(device_id)
        else:
            self.panel_level_devices.setdefault(panel_id, []).append(device_id)

    def _resolve_devices(self) -> None:
        for device_id in sorted(self.devices):
            device = self.devices[device_id]
            ct_ids = self._device_ct_ids(device)
            if not ct_ids:
                self.unplaced_device_ids.append(device_id)
                continue
            owners = sorted(
                {
                    panel_id
                    for ct_id in ct_ids
                    if (panel_id := self.channel_panel.get(ct_id)) is not None
                }
            )
            if not owners:
                self.unplaced_device_ids.append(device_id)
                continue
            if len(owners) > 1:
                self.notes.append(
                    f"device {device_id!r} references CTs in several panels "
                    f"({', '.join(owners)}); placed under {owners[0]!r}"
                )
            panel_id = owners[0]
            where, target = self._place_device(device_id, panel_id, ct_ids)
            self._assign_device(device_id, where=where, target=target, panel_id=panel_id)

        for device_ids in self.attached_devices.values():
            device_ids.sort(key=self._device_sort_key)
        for device_ids in self.panel_level_devices.values():
            device_ids.sort(key=self._device_sort_key)

    def _device_sort_key(self, device_id: str) -> tuple[str, str]:
        device = self.devices[device_id]
        return ((device.name or device_id).casefold(), device_id)

    # -- rendering ---------------------------------------------------------- #
    def _badges(self, status: str | None, extra: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
        badges: list[dict[str, str]] = []
        tone = _STATUS_TONE.get(status or "")
        if status and tone is not None:
            badges.append({"text": status, "tone": tone})
        badges.extend(extra or ())
        return badges

    def _device_node(self, device_id: str, *, depth: int, via_ct_id: str | None) -> dict[str, Any]:
        device = self.devices[device_id]
        result = self.latest_results.get(device_id)
        status = _text(device.topology_status)
        measurement = _measurement_of(result)
        # ``none`` means the engine holds no value for this fixture.  Its stored
        # ``power_w`` (0.0 on an ``unknown`` result) exists only for residual
        # accounting, so it is not surfaced as a reading: a zero would be a claim.
        power = _result_power(result) if measurement != "none" else None
        node: dict[str, Any] = {
            "id": _node_id("device", device_id),
            "kind": "device",
            "label": device.name or device_id,
            "subtitle": _text(device.phase) or "unknown phase",
            "badges": self._badges(status),
            "depth_hint": depth,
            "children": [],
            "device_id": device_id,
            "phase": _text(device.phase),
            "topology_status": status,
            "source": _text(_result_field(result, "source")),
            "measurement": measurement,
            "latest_power_w": power,
            "confidence": _result_confidence(result),
            "via_ct_id": via_ct_id,
        }
        if device.double_pole:
            node["double_pole"] = True
        return node

    def _circuit_node(self, ct_id: str, *, depth: int, path: tuple[str, ...]) -> dict[str, Any]:
        channel = self.channels[ct_id]
        status = _text(channel.topology_status)
        extra: list[dict[str, str]] = []
        if channel.alias_of_ct_id:
            extra.append({"text": "alias", "tone": "info"})
        if channel.paired_ct_id:
            extra.append({"text": "paired", "tone": "info"})
        if _text(channel.polarity) == "inverted":
            extra.append({"text": "reversed", "tone": "warn"})
        if not channel.enabled:
            extra.append({"text": "disabled", "tone": "muted"})

        children: list[dict[str, Any]] = []
        for device_id in self.attached_devices.get(ct_id, ()):
            children.append(
                self._device_node(device_id, depth=depth + 1, via_ct_id=ct_id)
            )
        for child_id in self.circuit_children.get(ct_id, ()):
            if child_id in path:  # belt and braces; the resolver already broke cycles
                continue
            children.append(
                self._circuit_node(child_id, depth=depth + 1, path=(*path, child_id))
            )

        phase = _text(channel.phase) or "unknown"
        ct_type = _text(channel.ct_type) or "unknown"
        return {
            "id": _node_id("circuit", ct_id),
            "kind": "circuit",
            "label": channel.name or ct_id,
            "subtitle": f"{ct_type} · {phase}",
            "badges": self._badges(status, extra),
            "depth_hint": depth,
            "children": children,
            "ct_id": ct_id,
            "ct_type": ct_type,
            "phase": _text(channel.phase),
            "entity_id": channel.entity_id,
            "enabled": bool(channel.enabled),
            "parent_ct_id": channel.parent_ct_id,
            "paired_ct_id": channel.paired_ct_id,
            "alias_of_ct_id": channel.alias_of_ct_id,
            "topology_status": status,
            "polarity": _text(channel.polarity),
            "measurement_multiplier": float(channel.measurement_multiplier),
            "latest_power_w": None,
        }

    def _group_node(
        self,
        *,
        panel_id: str,
        role: str,
        label: str,
        depth: int,
        children: list[dict[str, Any]],
        ident: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": _node_id("group", ident or f"{panel_id}:{role}"),
            "kind": "group",
            "label": label,
            "subtitle": f"{len(children)} node(s)",
            "badges": [],
            "depth_hint": depth,
            "children": children,
            "role": role,
        }

    def _panel_node(self, panel_id: str, *, depth: int, path: tuple[str, ...]) -> dict[str, Any]:
        panel = self.panels[panel_id]
        circuit_ids = sorted(
            ct_id
            for ct_id in self.channels
            if self.circuit_panel.get(ct_id) == panel_id and self.circuit_parent.get(ct_id) is None
        )
        device_ids = sorted(
            self.panel_level_devices.get(panel_id, ()), key=self._device_sort_key
        )
        child_panels = sorted(
            (
                other.panel_id
                for other in self.panels.values()
                if other.parent_panel_id == panel_id
            ),
            key=lambda pid: (self.panels[pid].name.casefold(), pid),
        )
        feed_ct_ids = sorted(panel.feeds)
        is_service_entry = self._is_service_entry(panel_id)

        groups: list[dict[str, Any]] = []
        group_depth = depth + 1
        circuits = [
            self._circuit_node(ct_id, depth=group_depth + 1, path=(ct_id,))
            for ct_id in circuit_ids
        ]
        if _group_has_content(circuits):
            groups.append(
                self._group_node(
                    panel_id=panel_id,
                    role="circuits",
                    label="CIRCUITS",
                    depth=group_depth,
                    children=circuits,
                )
            )
        devices = [
            self._device_node(device_id, depth=group_depth + 1, via_ct_id=None)
            for device_id in device_ids
        ]
        if _group_has_content(devices):
            groups.append(
                self._group_node(
                    panel_id=panel_id,
                    role="devices",
                    label="DEVICES",
                    depth=group_depth,
                    children=devices,
                )
            )
        subpanel_nodes: list[dict[str, Any]] = []
        for child_id in child_panels:
            if child_id in path:  # defensive; resolver breaks cycles
                continue
            subpanel_nodes.append(
                self._panel_node(child_id, depth=group_depth + 1, path=(*path, child_id))
            )
        if _group_has_content(subpanel_nodes):
            groups.append(
                self._group_node(
                    panel_id=panel_id,
                    role="subpanels",
                    label="SUBPANELS",
                    depth=group_depth,
                    children=subpanel_nodes,
                )
            )

        extra: list[dict[str, str]] = []
        monitors = sorted(panel.monitor_names)
        if len(monitors) > 1:
            extra.append({"text": f"{len(monitors)} monitors", "tone": "info"})
        role_text = "service entry" if is_service_entry else "distribution board"
        return {
            "id": _node_id("panel", panel_id),
            "kind": "panel",
            "label": panel.name,
            "subtitle": f"{role_text} · {len(circuit_ids)} circuits",
            "badges": self._badges(
                "candidate" if _text(self.topology.status) == "candidate" else None, extra
            ),
            "depth_hint": depth,
            "children": groups,
            "panel_id": panel_id,
            "monitor": panel.name,
            "monitors": monitors,
            "feed_ct_ids": feed_ct_ids,
            "is_service_entry": is_service_entry,
        }

    # -- summaries ---------------------------------------------------------- #
    def _panel_summary(self, node: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": node["id"],
            "panel_id": node["panel_id"],
            "label": node["label"],
            "monitor": node["monitor"],
            "feed_ct_ids": list(node["feed_ct_ids"]),
            "is_service_entry": node["is_service_entry"],
            "depth": node["depth_hint"],
        }

    def _circuit_summary(self, node: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": node["id"],
            "ct_id": node["ct_id"],
            "label": node["label"],
            "ct_type": node["ct_type"],
            "phase": node["phase"],
            "parent_ct_id": node["parent_ct_id"],
            "topology_status": node["topology_status"],
            "depth": node["depth_hint"],
        }

    def _device_summary(self, node: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": node["id"],
            "device_id": node["device_id"],
            "label": node["label"],
            "phase": node["phase"],
            "topology_status": node["topology_status"],
            "via_ct_id": node["via_ct_id"],
            "measurement": node["measurement"],
            "latest_power_w": node["latest_power_w"],
            "confidence": node["confidence"],
            "source": node["source"],
            "depth": node["depth_hint"],
        }

    @staticmethod
    def _walk(nodes: Iterable[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
        stack = list(reversed(list(nodes)))
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.get("children") or ()))

    # -- public entry ------------------------------------------------------- #
    def build(self) -> dict[str, Any]:
        self._resolve_panel_declarations()
        self._merge_duplicate_panels()
        self._resolve_panel_parents()
        self._resolve_circuits()
        self._resolve_devices()

        # Contract R7 orders panels by name.  Roots additionally put the service
        # entry first, because that is the board the whole house hangs off and
        # the UI should not have to hunt for it.
        roots = sorted(
            (
                panel_id
                for panel_id, panel in self.panels.items()
                if panel.parent_panel_id is None or panel.parent_panel_id not in self.panels
            ),
            key=lambda pid: (
                not self._is_service_entry(pid),
                self.panels[pid].name.casefold(),
                pid,
            ),
        )
        if not roots:
            # Every panel sits in a cycle; break one so rendering always happens.
            fallback = sorted(self.panels)[0]
            self.panels[fallback].parent_panel_id = None
            self.notes.append(f"no panel root found; {fallback!r} promoted to root")
            roots = [fallback]

        root_nodes = [self._panel_node(pid, depth=0, path=(pid,)) for pid in roots]

        # Unattributed: nothing may be dropped.  Devices with no resolvable CT,
        # and circuits whose panel is unknown, land in one final group under the
        # primary root panel (contract §1.5).
        root_node = root_nodes[0]
        unattributed: list[dict[str, Any]] = []
        unplaced_circuits: list[str] = []
        for ct_id in self.unplaced_circuit_ids:
            if ct_id in self.feed_ids:
                continue
            if self.circuit_parent.get(ct_id) is not None:
                # Nested under another unplaced circuit; listing it here too
                # would render the same circuit twice.
                continue
            unplaced_circuits.append(ct_id)
        if unplaced_circuits:
            children = [
                self._circuit_node(ct_id, depth=root_node["depth_hint"] + 2, path=(ct_id,))
                for ct_id in unplaced_circuits
            ]
            unattributed.append(
                self._group_node(
                    panel_id=root_node["panel_id"],
                    role="circuits",
                    label="Unattributed",
                    depth=root_node["depth_hint"] + 1,
                    children=children,
                    ident=f"{root_node['panel_id']}:unattributed:circuits",
                )
            )
        if self.unplaced_device_ids:
            children = [
                self._device_node(
                    device_id, depth=root_node["depth_hint"] + 2, via_ct_id=None
                )
                for device_id in sorted(self.unplaced_device_ids, key=self._device_sort_key)
            ]
            unattributed.append(
                self._group_node(
                    panel_id=root_node["panel_id"],
                    role="devices",
                    label="Unattributed",
                    depth=root_node["depth_hint"] + 1,
                    children=children,
                    ident=f"{root_node['panel_id']}:unattributed:devices",
                )
            )
        root_node["children"] = [*root_node["children"], *unattributed]

        all_nodes = list(self._walk(root_nodes))
        circuit_nodes = [node for node in all_nodes if node["kind"] == "circuit"]
        device_nodes = [node for node in all_nodes if node["kind"] == "device"]
        panel_nodes = [node for node in all_nodes if node["kind"] == "panel"]

        # Every circuit and device must appear exactly once (contract R6).
        seen_circuits: set[str] = set()
        for node in circuit_nodes:
            if node["ct_id"] in seen_circuits:
                self.notes.append(f"circuit {node['ct_id']!r} appeared twice; deduplicated")
            seen_circuits.add(node["ct_id"])
        seen_devices: set[str] = set()
        for node in device_nodes:
            if node["device_id"] in seen_devices:
                self.notes.append(f"device {node['device_id']!r} appeared twice; deduplicated")
            seen_devices.add(node["device_id"])

        max_depth = max((int(node["depth_hint"]) for node in all_nodes), default=0)
        unplaced_circuit_count = len(
            [ct_id for ct_id in self.unplaced_circuit_ids if ct_id not in self.feed_ids]
        )

        stats = {
            "panels": len(panel_nodes),
            "circuits": len(circuit_nodes),
            "devices": len(device_nodes),
            "unplaced_devices": len(self.unplaced_device_ids),
            "unplaced_circuits": unplaced_circuit_count,
            "max_depth": max_depth,
            "duplicate_feeds": self.duplicate_feeds,
            # Additive context so a client can show "32 of 38 channels are
            # circuits": the other 6 are panel feeds, listed on the panel nodes.
            "channels": len(self.channels),
            "feed_circuits": len(self.feed_ids),
        }

        self.notes.extend(
            [
                "Read-time view: parent_ct_id is never modified by this endpoint.",
                "A panel's feed is the set of 'main' CTs declaring that panel.",
                "A duplicate feed is an alias, not a child: panels measured by two "
                "monitors are merged into one node.",
                "Devices spanning two CTs (double-pole) are listed in the panel's "
                "DEVICES group; no single circuit owns them.",
            ]
        )

        payload: dict[str, Any] = {
            "version": TREE_VERSION,
            "status": _text(self.topology.status),
            "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
            "topology_version": int(self.topology.version),
            "stats": stats,
            "roots": root_nodes,
            "flat": {
                "panels": [self._panel_summary(node) for node in panel_nodes],
                "circuits": [self._circuit_summary(node) for node in circuit_nodes],
                "devices": [self._device_summary(node) for node in device_nodes],
            },
            "notes": self.notes,
        }
        if self.nesting_supplied:
            payload["suggestions"] = {
                "candidate_parents": {
                    child: sorted(parents) for child, parents in sorted(self.advisory_parents.items())
                },
                "duplicate_aliases": dict(sorted(self.alias_of.items())),
                "applied": True,
                "note": (
                    "advisory only: a candidate parent is never a tree edge, and a "
                    "duplicate is an alias, so leaf counts are unchanged"
                ),
            }
        return payload

    def _is_service_entry(self, panel_id: str) -> bool:
        return panel_id in self._service_entry_ids


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def build_panel_tree(
    topology: Topology,
    *,
    latest_results: Mapping[str, Any] | None = None,
    nesting: Any = None,
) -> dict[str, Any]:
    """Turn a :class:`Topology` into the panel tree of contract v1 §1.

    ``latest_results`` maps ``device_id`` to an ``InferenceResult`` (or a plain
    dict with ``power_w``/``confidence``/``source``); it only ever fills the
    nullable measurement fields.

    ``nesting`` is the ``GET /api/v1/topology/nesting`` payload, or a bare list
    of candidates.  Duplicate candidates merge panels whose feeds alias each
    other; *nested* candidates are returned under ``suggestions`` and never
    become tree edges, so circuit/device counts cannot change because of them.

    Pure and deterministic: same input, same output (except ``generated_at``).
    """
    return _TreeBuilder(topology, latest_results, nesting).build()


__all__ = ["IMPLICIT_PANEL_ID", "IMPLICIT_PANEL_NAME", "TREE_VERSION", "build_panel_tree"]
