"""Transition collection and anchored step detection (X02 split).

Moved verbatim from :mod:`~wattmosaic_engine.disaggregation.engine` as
module-level functions that take engine state explicitly.  ``_Transition`` and
the pure helpers it needs live here so this module never imports the
orchestrator.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from wattmosaic_engine.contracts import Topology
from wattmosaic_engine.disaggregation._shared import _is_off, _is_on
from wattmosaic_engine.disaggregation.events import StepChange, detect_changes
from wattmosaic_engine.electrical.topology import TopologyGraph
from wattmosaic_engine.electrical.window import CoercedWindow


@dataclass(frozen=True, slots=True)
class _Transition:
    device_id: str
    previous: str | None
    new: str | None
    at: datetime | None
    direction: str | None
    #: True only when ``at`` is the instant HA reported the state change.
    #: Transitions inferred from the previous cycle's snapshot also carry a
    #: timestamp (``window.at``), but that is when we *looked*, not when the
    #: device switched, so it must never be used to anchor CT step detection.
    anchored: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "previous_state": self.previous,
            "new_state": self.new,
            "occurred_at": None if self.at is None else self.at.isoformat(),
            "direction": self.direction,
            "anchored": self.anchored,
        }


def _state_text(device_states: Mapping[str, Any], device_id: str) -> str | None:
    raw = device_states.get(device_id)
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    state = getattr(raw, "state", None)
    return None if state is None else str(state)


def _normalize_overrides(raw: Any) -> dict[str, str]:
    """``{"Heating": "on", "Cooling": "off"}`` -> lowercase, validated."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for label, meaning in raw.items():
        key = str(label).strip().lower()
        value = str(meaning).strip().lower()
        if key and value in {"on", "off"}:
            out[key] = value
    return out


def _device_state_overrides(topology: Topology) -> dict[str, dict[str, str]]:
    """``device_id -> {label: on|off}`` declared in the device's metadata.

    The global ``ON_STATES``/``OFF_STATES`` sets are a good default, but a single
    global meaning cannot describe every integration.  A spa in a typical
    installation reports ``Cooling`` while drawing 22 W and ``Heating`` while
    drawing 4.5 kW; the global set treats ``cooling`` as ON, which would make the
    engine believe a 22 W standby state is a 4.5 kW heater.  Overrides are
    *declarative*, per device, and derived from measured power levels — never
    guessed from the label text alone.
    """
    from wattmosaic_engine.device_links import STATE_OVERRIDES_KEY

    out: dict[str, dict[str, str]] = {}
    for device in topology.devices:
        metadata = device.metadata if isinstance(device.metadata, Mapping) else {}
        overrides = _normalize_overrides(metadata.get(STATE_OVERRIDES_KEY))
        if overrides:
            out[device.device_id] = overrides
    return out


def _transition_direction(
    previous: str | None,
    new: str | None,
    overrides: Mapping[str, str] | None = None,
) -> str | None:
    if previous is None and new is None:
        return None
    if previous is None:
        return "activation" if _is_on(new, overrides) else None
    if new is None:
        return "deactivation" if _is_on(previous, overrides) else None
    if _is_on(new, overrides) and _is_off(previous, overrides):
        return "activation"
    if _is_off(new, overrides) and _is_on(previous, overrides):
        return "deactivation"
    if _is_on(new, overrides) and not _is_on(previous, overrides):
        return "activation"
    if _is_off(new, overrides) and not _is_off(previous, overrides):
        return "deactivation"
    return None


def _collect_transitions(
    window: CoercedWindow,
    device_states: Mapping[str, Any],
    overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, _Transition]:
    overrides = overrides or {}
    out: dict[str, _Transition] = {}
    for item in window.state_transitions:
        if isinstance(item, Mapping):
            did = item.get("device_id")
            prev = item.get("previous_state", item.get("old_state"))
            new = item.get("new_state", item.get("state"))
            at = item.get("occurred_at", item.get("timestamp"))
        else:
            did = getattr(item, "device_id", None)
            prev = getattr(item, "previous_state", None)
            new = getattr(item, "new_state", None)
            at = getattr(item, "occurred_at", getattr(item, "timestamp", None))
        if not did:
            continue
        if not isinstance(at, datetime):
            at = None
        out[str(did)] = _Transition(
            device_id=str(did),
            previous=None if prev is None else str(prev),
            new=None if new is None else str(new),
            at=at,
            direction=_transition_direction(
                None if prev is None else str(prev),
                None if new is None else str(new),
                overrides.get(str(did)),
            ),
            anchored=at is not None,
        )
    # Derive transitions from previous_states vs current device_states.
    for did in device_states:
        current = _state_text(device_states, did)
        previous = window.previous_states.get(did)
        if did in out or previous is None:
            continue
        if current is None or current == previous:
            continue
        out[did] = _Transition(
            device_id=did,
            previous=previous,
            new=current,
            at=window.at,
            direction=_transition_direction(previous, current, overrides.get(did)),
            anchored=False,
        )
    return out


def _device_step(
    graph: TopologyGraph,
    device_id: str,
    steps: Mapping[str, StepChange],
) -> StepChange | None:
    """Best detected step on a CT that observes ``device_id``."""
    candidates = [
        steps[cid]
        for cid in graph.device_channels(device_id)
        if cid in steps and steps[cid].detected
    ]
    if not candidates:
        return None

    def rank(step: StepChange) -> tuple[int, float]:
        t = graph.channels.get(step.channel_id)
        kind = str(t.ct_type) if t else "unknown"
        specificity = 1 if kind == "branch" else 2
        return (specificity, -abs(step.delta_w))

    return sorted(candidates, key=rank)[0]


def _attribution_step(
    graph: TopologyGraph,
    device_id: str,
    steps: Mapping[str, StepChange],
) -> StepChange | None:
    """Best detected step on a row that *attributes* this device.

    Deliberately narrower than :func:`_device_step`.  A device is also observed
    by its enclosing mains, but a step on a main may have been produced by any
    other circuit in the panel, so it cannot be attributed to this device.  Only
    the device's own attribution rows may speak for it (ADR-0026).
    """
    candidates = [
        steps[cid]
        for cid in graph.attribution_ct_ids(device_id)
        if cid in steps and steps[cid].detected
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda step: abs(step.delta_w))


def _row_siblings(graph: TopologyGraph, device_id: str) -> tuple[str, ...]:
    """Other devices that share at least one of this device's attribution rows."""
    rows = set(graph.attribution_ct_ids(device_id))
    if not rows:
        return ()
    topology = graph.topology
    out = [
        other.device_id
        for other in topology.devices
        if other.device_id != device_id
        and rows.intersection(other.branch_ct_ids)
    ]
    return tuple(out)


def _siblings_held(
    graph: TopologyGraph,
    device_id: str,
    transitions: Mapping[str, _Transition],
) -> bool:
    """True when no other device on this device's rows changed state.

    This is the condition the delta rule needs, and it is deliberately weaker
    than ADR-0023's "every sibling is positively OFF".  A sibling may well be
    running; all that matters is that it did *not* change in this window, because
    then the measured step on the row belongs to the device that did change.
    A sibling whose state is unknown is not evidence of instability, but a
    sibling that visibly changed is evidence against attribution.
    """
    for sibling in _row_siblings(graph, device_id):
        transition = transitions.get(sibling)
        if transition is not None and transition.direction is not None:
            return False
    return True


def _anchor_steps(
    steps: Mapping[str, StepChange],
    transitions: Mapping[str, _Transition],
    graph: TopologyGraph,
    window: CoercedWindow,
    *,
    after_s: float,
    cadence_s: float,
    k_mad: float,
    min_delta_w: float,
    noise_floor_w: float,
    noise_floors: Mapping[str, float] | None = None,
    diagnostics: dict[str, Any],
) -> dict[str, StepChange]:
    """Confine CT steps to the instant HA says the device switched.

    ``steps`` is the unanchored scan.  It answers "did this channel move
    somewhere in the buffered history", which is not the same question as
    "did it move when this device switched".  On a real capture the two
    differ by up to a minute: the CT cadence is 15.7 s and the engine runs
    every 5 s, so a lamp switched on at t can be matched to a sister
    circuit's step at t−47 s.

    Every transition HA timestamped itself (``anchored``) gets a rescan of
    exactly the channels that observe its device, restricted to
    ``[at − after_s − cadence, at + after_s + cadence]``: one observation
    window either side, which is the physical uncertainty of *when* a CT
    published a step.  A step on one of those channels that lands outside
    the window is discarded, because that channel's movement is not evidence
    about this transition.

    A transition derived from the previous cycle's snapshot carries no
    instant -- only the time we looked -- so it anchors nothing and the
    unanchored result stands for it.
    """
    if not steps:
        return dict(steps)
    anchored = {
        did: t for did, t in transitions.items() if t.anchored and t.at is not None
    }
    if not anchored:
        return dict(steps)
    out = dict(steps)
    multipliers = {cid: graph.multiplier_for(cid) for cid in window.readings}
    # The anchored rescan must use the same per-channel floor as the
    # unanchored scan.  Falling back to the scalar here would let a noisy
    # channel pass the unanchored threshold and then fail (or vice versa) in
    # the rescan, so a step's detected-ness would depend on which path
    # measured it.
    floors = dict(noise_floors or {})
    #: For each channel that observes an anchored transition, the instants it
    #: is allowed to have stepped at.  A channel shared by several devices
    #: (a branch or a main) is valid if *any* of its devices can explain it.
    windows: dict[str, list[tuple[datetime, datetime]]] = {}
    dropped: list[str] = []
    skipped: list[str] = []
    for did, transition in anchored.items():
        at = transition.at
        if at is None:  # pragma: no cover - filtered above
            continue
        lo = at - timedelta(seconds=after_s + cadence_s)
        hi = at + timedelta(seconds=after_s + cadence_s)
        channels = {
            cid: window.readings[cid]
            for cid in graph.device_channels(did)
            if cid in window.readings
        }
        if not channels:
            continue
        try:
            rescued = detect_changes(
                channels,
                window.at,
                before_s=after_s,
                after_s=after_s,
                k_mad=k_mad,
                min_delta_w=min_delta_w,
                noise_floor_w={
                    cid: floors.get(cid, noise_floor_w) for cid in channels
                },
                multipliers={
                    cid: multipliers[cid] for cid in channels if cid in multipliers
                },
                anchor=at,
                anchor_before_s=after_s,
                anchor_after_s=after_s,
                anchor_grace_s=cadence_s,
                cadence_s=cadence_s,
            )
        except ValueError:
            # Naive/aware mismatch between the transition instant and the
            # window.  The data cannot be anchored, so the unanchored scan
            # stands rather than raising out of inference.
            skipped.append(did)
            continue
        for cid in channels:
            windows.setdefault(cid, []).append((lo, hi))
            rescued_step = rescued.get(cid)
            if rescued_step is not None and rescued_step.detected:
                out[cid] = rescued_step
            elif cid not in rescued:
                # No candidate split at all near the transition: leave the
                # unanchored step in place for the window test below to
                # judge.
                continue
            else:
                # A split exists but is below threshold.  That is evidence
                # the channel did *not* move at the transition, so an
                # unanchored step elsewhere cannot be cited for it.
                out.pop(cid, None)
                dropped.append(cid)
    for cid, spans in windows.items():
        step = out.get(cid)
        if step is None or not step.detected:
            continue
        when = step.change_time
        if any(lo <= when <= hi for lo, hi in spans):
            continue
        # The channel moved, but not when any device that observes it
        # switched: the movement belongs to an unrelated load and citing it
        # would credit one device with another circuit's power.
        dropped.append(cid)
        out.pop(cid, None)
    if dropped:
        diagnostics["anchored_steps_dropped"] = sorted(dropped)
        diagnostics["warnings"].append("ct_step_outside_transition_anchor")
    if skipped:
        diagnostics["anchor_skipped_devices"] = sorted(skipped)
    return out
