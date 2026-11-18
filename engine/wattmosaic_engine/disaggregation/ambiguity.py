"""Concurrent-event ambiguity detection and group bounds (X02 split).

Moved verbatim from :mod:`~wattmosaic_engine.disaggregation.engine` as
module-level functions that take engine state explicitly.  D5/D6 delete this
module later; until then its behaviour is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from wattmosaic_engine.disaggregation._shared import _is_off
from wattmosaic_engine.disaggregation.configuration import EffectiveConfig
from wattmosaic_engine.disaggregation.transitions import _Transition
from wattmosaic_engine.electrical.matrix import MeasurementMatrix
from wattmosaic_engine.electrical.models import ModelView
from wattmosaic_engine.electrical.solver import Identifiability
from wattmosaic_engine.electrical.topology import TopologyGraph
from wattmosaic_engine.electrical.window import CoercedWindow


def _detect_ambiguity(
    transitions: Mapping[str, _Transition],
    graph: TopologyGraph,
    ident: Identifiability,
    diagnostics: dict[str, Any],
) -> set[str]:
    changed = [d for d, t in transitions.items() if t.direction is not None]
    if len(changed) < 2:
        # Ambiguity needs at least two claimants.  A *single* changed device
        # that the measurements cannot separate is not ambiguous: there is
        # nothing it could be confused with.  (An unexplained residual with
        # no transition at all is flagged in ``_reconcile`` instead.)
        return set()
    unidentifiable = set(ident.unidentifiable)
    ambiguous: set[str] = set()
    groups: list[list[str]] = []
    # Ambiguous when at least two devices that changed state cannot be
    # separated by the measurements (null-space sharing).
    for group in ident.ambiguous_groups:
        members = [d for d in group if d in changed]
        if len(members) >= 2:
            groups.append(members)
            ambiguous.update(members)
    # Devices that changed but are unidentifiable for another reason.
    unresolved = [d for d in changed if d in unidentifiable and d not in ambiguous]
    if unresolved:
        groups.append(unresolved)
        ambiguous.update(unresolved)
    if ambiguous:
        diagnostics["ambiguity"] = {
            "ambiguous": True,
            "explanation": "ambiguous_concurrent_events",
            "groups": groups,
            "devices": sorted(ambiguous),
            "changed_devices": sorted(changed),
            "reason": (
                "concurrent state changes cannot be separated by the available "
                "CT rows (shared null space); power is left unattributed"
            ),
        }
        diagnostics["warnings"].append("ambiguous_concurrent_events")
    _ = graph
    return ambiguous


def _group_bounds(
    members: Sequence[str],
    *,
    matrix: MeasurementMatrix,
    window: CoercedWindow,
    models: Mapping[str, ModelView | None],
    states: Mapping[str, str | None],
    configs: Mapping[str, EffectiveConfig],
    active_rows: Sequence[str],
) -> tuple[float | None, float | None]:
    """Honest floor and cap of an inseparable group's *sum*.

    An ambiguous group is only observable through the total on its shared
    attribution row, never through a per-device value.  ``min_w`` is the
    largest wattage the members' own stated on-power says the group must
    draw (clamped >= 0; zero when no on-power is stated).  ``max_w`` is that
    shared row's measured capacity clipped by the members' stated upper
    bounds.  No per-device watt is computed here: the caller attaches the
    range to every member, whose ``power_w`` stays a 0.0 placeholder.
    ``(None, None)`` means no evidence established a meaningful range.
    """
    active = set(active_rows)
    floors: list[float] = []
    stated_caps: list[float] = []
    for did in members:
        view = models.get(did)
        cfg = configs.get(did)
        floor = 0.0
        if view is not None and view.params.on_power_w is not None:
            floor = max(floor, max(float(view.params.on_power_w), 0.0))
        if cfg is not None and cfg.expected_on_w is not None:
            floor = max(floor, max(float(cfg.expected_on_w), 0.0))
        # A device positively OFF is pinned at zero by an explicit state
        # constraint, so it cannot raise the group's floor.
        if _is_off(states.get(did)):
            floor = 0.0
        floors.append(floor)
        cap: float | None = None
        if view is not None and view.upper_bound_w is not None:
            cap = max(float(view.upper_bound_w), 0.0)
        if cfg is not None and cfg.config_ceiling_w is not None:
            ceiling = max(float(cfg.config_ceiling_w), 0.0)
            cap = ceiling if cap is None else min(cap, ceiling)
        if cap is not None:
            stated_caps.append(cap)

    # Capacity of the tightest active attribution row that sees every member.
    capacities: list[float] = []
    for cid in matrix.row_channel_ids:
        if cid not in active:
            continue
        row = matrix.row_index(cid)
        if row is None:
            continue
        cols = [matrix.col_index(did) for did in members]
        if any(col is None or abs(float(matrix.A[row, col])) <= 0.0 for col in cols):
            continue
        reading = window.readings.get(cid)
        if reading is None:
            continue
        value = reading.signed_w()
        if value is None:
            continue
        capacities.append(max(0.0, abs(float(value))))

    upper: list[float] = list(stated_caps)
    row_capacity = min(capacities) if capacities else None
    if row_capacity is not None:
        upper.append(row_capacity)
    max_w = min(upper) if upper else None
    min_w = max(floors) if any(f > 0.0 for f in floors) else None
    if min_w is None and max_w is None:
        return None, None
    if min_w is None:
        min_w = 0.0
    if max_w is None:
        max_w = min_w
    # A stated floor can never exceed the cap; contradictory user evidence
    # must resolve to the interval, not to an impossible range.
    max_w = max(max_w, min_w)
    return round(min_w, 6), round(max_w, 6)
