"""Level 1-4 load disaggregation engine (spec §9).

Public contract (``docs/interface_contracts.md`` §3.4)::

    DisaggregationEngine().infer(
        window, *, device_states, models, topology
    ) -> InferenceBatch

Pipeline
--------
1. Validate/index the topology (``TopologyGraph``).
2. Resolve Level-1 dedicated CT measurements (``resolve_direct``).
3. Build ``y = A x`` (``build_measurement_matrix``), residualising nested CTs
   so no physical quantity is counted twice.
4. Subtract direct contributions from ``y`` and solve for the remaining
   devices with bounded NNLS.
5. SVD identifiability analysis decides what may be called ``inferred``.
6. Event detection (observed cadence, MAD threshold) provides evidence and
   concurrency/ambiguity detection.
7. Assign the measurement hierarchy per device, with per-device isolation.
8. Reconcile: ``unknown_residual = main - accounted`` and never redistribute.

Nothing in this module mutates the topology or calls Home Assistant.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from wattmosaic_engine.contracts import (
    InferenceResult,
    MonitoringLevel,
    PowerBehavior,
    QualityStatus,
    Reconciliation,
    Source,
    StageStatus,
    Topology,
)
from wattmosaic_engine.device_links import STATE_OVERRIDES_KEY
from wattmosaic_engine.disaggregation.confidence import (
    ConfidenceInputs,
    compute_confidence,
    event_isolation_score,
)
from wattmosaic_engine.disaggregation.configuration import (
    EffectiveConfig,
    configured_power,
    effective_configs,
)
from wattmosaic_engine.disaggregation.events import StepChange, StepTracker, detect_changes
from wattmosaic_engine.disaggregation.tiers import TierInputs, grade
from wattmosaic_engine.electrical.direct import DirectReading, resolve_direct
from wattmosaic_engine.electrical.main_power import MainPower, main_power_w
from wattmosaic_engine.electrical.matrix import MeasurementMatrix, build_measurement_matrix
from wattmosaic_engine.electrical.models import ModelView, resolve_model
from wattmosaic_engine.electrical.solver import (
    Identifiability,
    analyse_identifiability,
    augment_with_unknown_slack,
    solve_constrained_nnls,
)
from wattmosaic_engine.electrical.topology import TopologyGraph, build_graph
from wattmosaic_engine.electrical.window import CoercedWindow, coerce_window

#: A device explicitly reported OFF must not be credited with more than this
#: (or twice its modelled standby power) just because its circuit carries an
#: unexplained load.
OFF_ATTRIBUTION_TOLERANCE_W = 15.0

#: A device the state declares OFF, with no stated standby draw, is pinned at
#: zero.  The bounded solver requires ``lower < upper`` strictly, so the cap is
#: the smallest positive float that still behaves as zero at any realistic watt
#: magnitude rather than a literal ``0.0``.
_PINNED_ZERO_W = 1e-9

#: Relative tolerance when comparing a solved value with a modelled value.
MODEL_MATCH_REL_TOL = 0.40

ON_STATES = {
    "on",
    "running",
    "heating",
    "cooling",
    "active",
    "open",
    "playing",
    "washing",
    "drying",
    "cleaning",
}
#: States that positively indicate no *active* operation.  ``unavailable`` and
#: ``unknown`` are deliberately excluded: they mean "we do not know", not "off",
#: and treating them as OFF would silently pin a device to zero.
OFF_STATES = {"off", "idle", "standby", "closed", "paused", "none"}
UNKNOWN_STATES = {"unavailable", "unknown", ""}


@dataclass(slots=True)
class InferenceBatch:
    results: list[InferenceResult]
    reconciliation: Reconciliation
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def by_device(self) -> dict[str, InferenceResult]:
        return {r.device_id: r for r in self.results}

    def get(self, device_id: str) -> InferenceResult | None:
        return self.by_device().get(device_id)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


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
    out: dict[str, dict[str, str]] = {}
    for device in topology.devices:
        metadata = device.metadata if isinstance(device.metadata, Mapping) else {}
        overrides = _normalize_overrides(metadata.get(STATE_OVERRIDES_KEY))
        if overrides:
            out[device.device_id] = overrides
    return out


def _state_meaning(state: str | None, overrides: Mapping[str, str] | None = None) -> str | None:
    """``"on"``, ``"off"`` or ``None`` (unknown), honouring per-device overrides."""
    if state is None:
        return None
    key = state.strip().lower()
    if overrides:
        override = overrides.get(key)
        if override is not None:
            return override
    if key in ON_STATES:
        return "on"
    if key in OFF_STATES:
        return "off"
    return None


def _is_on(state: str | None, overrides: Mapping[str, str] | None = None) -> bool:
    return _state_meaning(state, overrides) == "on"


def _is_off(state: str | None, overrides: Mapping[str, str] | None = None) -> bool:
    return _state_meaning(state, overrides) == "off"


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


@dataclass(frozen=True, slots=True)
class _Transition:
    device_id: str
    previous: str | None
    new: str | None
    at: datetime | None
    direction: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "previous_state": self.previous,
            "new_state": self.new,
            "occurred_at": None if self.at is None else self.at.isoformat(),
            "direction": self.direction,
        }


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
        specificity = 0 if kind == "dedicated_device" else (1 if kind == "branch" else 2)
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
        and rows.intersection([*other.dedicated_ct_ids, *other.branch_ct_ids])
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


def _state_power(model: ModelView | None, state: str | None) -> float | None:
    if model is None:
        return None
    value = model.power_for_state(state)
    if value is not None:
        return value
    # The aggregate ``estimated_power_w`` is a fallback for a state the model does
    # not describe -- but never for a state that positively says the device is
    # off.  A model with no ``off_power_w``/``standby_power_w`` returns ``None``
    # for "off", and substituting the aggregate estimate there reported an OFF
    # device as drawing its full running power.  That is the same fabrication as
    # crediting a whole CT row to one lamp: a number that looks measured and is
    # not.  "No model value for this state" is the honest answer.
    normalized = (state or "").strip().lower()
    if normalized in {"off", "idle", "standby", "closed", "paused", "unavailable", "unknown"}:
        return None
    if model.estimated_power_w is not None:
        return float(model.estimated_power_w)
    return None


def _measured_level(
    metadata: Mapping[str, object] | None, device_id: str
) -> float | None:
    """Measured dimmer output for one device, or ``None`` when not reported.

    The facade collects each device's control value from its declared state
    entity and publishes it under ``metadata["device_levels"]``.  This reads it
    back per device and refuses anything that is not a finite number: a level is
    a fraction of full scale, and a malformed one must degrade to "not reported"
    rather than to some arbitrary wattage.

    Range handling is deliberately *not* repeated here.  ``interpolate_curve``
    already clamps to the measured points -- because extrapolating past what was
    observed would invent watts -- and that behaviour is tested there.  A second
    clamp here could drift from it, and neither copy would be provably
    load-bearing.
    """
    levels = (metadata or {}).get("device_levels")
    if not isinstance(levels, Mapping):
        return None
    raw = levels.get(device_id)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _stage_for(model: ModelView | None, power_w: float) -> tuple[str | None, StageStatus]:
    if model is None or not model.params.stages:
        return (model.stage if model else None), StageStatus.UNKNOWN
    best = None
    best_distance = float("inf")
    for stage in model.params.stages:
        distance = abs(power_w - stage.power_mean_w)
        if distance < best_distance:
            best_distance = distance
            best = stage
    if best is None:
        return None, StageStatus.UNKNOWN
    tolerance = max(2.0 * best.power_std_w, 0.10 * max(abs(best.power_mean_w), 1.0))
    if best_distance <= tolerance:
        return best.name, StageStatus.PROBABLE
    return best.name, StageStatus.UNKNOWN


def _quality_from_age(age_s: float, quality: QualityStatus) -> QualityStatus:
    if quality in (QualityStatus.MISSING, QualityStatus.BAD):
        return quality
    if age_s > 180.0:
        return QualityStatus.STALE
    if age_s > 30.0:
        return QualityStatus.DEGRADED
    return quality


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class DisaggregationEngine:
    """Stateless, deterministic disaggregation engine.

    Configuration is passed as constructor defaults and may be overridden per
    call.  No state is mutated between calls, which makes every inference
    reproducible from (window, states, models, topology).
    """

    def __init__(
        self,
        *,
        max_measurement_age_s: float = 120.0,
        phase_split_l1: float = 0.5,
        k_mad: float = 4.0,
        min_delta_w: float = 5.0,
        noise_floor_w: float = 1.0,
        off_tolerance_w: float = OFF_ATTRIBUTION_TOLERANCE_W,
        model_ridge: float = 0.0,
        tracker: StepTracker | None = None,
    ) -> None:
        self.max_measurement_age_s = float(max_measurement_age_s)
        self.phase_split_l1 = float(phase_split_l1)
        self.k_mad = float(k_mad)
        self.min_delta_w = float(min_delta_w)
        self.noise_floor_w = float(noise_floor_w)
        self.off_tolerance_w = float(off_tolerance_w)
        self.model_ridge = float(max(model_ridge, 0.0))
        #: Optional observation buffer.  Supplying one enables CT step detection
        #: even when the synchroniser exposes only a point-in-time snapshot.
        self.tracker = tracker

    # -- §3.4 ---------------------------------------------------------------
    def infer(
        self,
        window: object,
        *,
        device_states: Mapping[str, Any],
        models: Any,
        topology: Topology,
    ) -> InferenceBatch:
        coerced = coerce_window(window)
        if self.tracker is not None:
            # Fill in history/cadence from the tracker where the window has
            # none, then record this cycle for the next one.  ``CoercedWindow``
            # is frozen, so a new instance is built instead of mutating.
            readings = dict(coerced.readings)
            for cid, reading in readings.items():
                if not reading.history:
                    hist = self.tracker.history(cid)
                    if hist:
                        readings[cid] = dataclasses.replace(reading, history=hist)
            if coerced.cadence_s is None:
                coerced = dataclasses.replace(
                    coerced,
                    readings=readings,
                    cadence_s=self.tracker.cadence_s(),
                )
            else:
                coerced = dataclasses.replace(coerced, readings=readings)
        graph = build_graph(topology, phase_split_l1=self.phase_split_l1)
        device_states = dict(device_states or {})
        device_ids = list(graph.device_ids)
        # Per-device configuration governs what a device may claim (ADR-0011).  It
        # is read from the topology metadata projection, so inference, learning
        # and event intake can never disagree about a device's three axes.
        configs = effective_configs(topology, graph=graph)
        # State keys that are not in the topology are recorded, never invented
        # as devices: a state entity without a modelled placement cannot be
        # attributed and must not create a phantom column in A.
        unknown_state_devices = [d for d in device_states if d not in graph.devices]

        diagnostics: dict[str, Any] = {
            "contract": "1.2.0",
            "topology_version": topology.version,
            "topology_status": str(topology.status),
            "topology_problems": graph.validate(),
            "window_at": coerced.at.isoformat(),
            "cadence_s": None,
            "before_s": None,
            "after_s": None,
            "device_count": len(device_ids),
            "unknown_state_devices": unknown_state_devices,
            "excluded_rows": {},
            "warnings": [],
            "ambiguity": {"ambiguous": False, "groups": [], "devices": []},
            "events": [],
            "errors": {},
            "configured_devices": sorted(configs),
        }
        if graph.validate():
            diagnostics["warnings"].append("topology_validation_problems")

        # ---- event detection (observed cadence, not a 2 s assumption) ----
        before_s, after_s = coerced.before_after_seconds()
        diagnostics["cadence_s"] = coerced.cadence()
        diagnostics["before_s"] = before_s
        diagnostics["after_s"] = after_s
        # Previous-cycle values come from the caller's metadata when it keeps
        # one.  Using the *current* readings here would make every two-point
        # delta zero and silently disable event detection.
        raw_previous = coerced.metadata.get("previous_values_w") or {}
        if not raw_previous and self.tracker is not None:
            raw_previous = self.tracker.previous_values()
        previous_values: dict[str, float] = {}
        if isinstance(raw_previous, Mapping):
            for cid, value in raw_previous.items():
                try:
                    previous_values[str(cid)] = float(value)
                except (TypeError, ValueError):
                    continue
        steps = detect_changes(
            coerced.readings,
            coerced.at,
            before_s=before_s,
            after_s=after_s,
            k_mad=self.k_mad,
            min_delta_w=self.min_delta_w,
            noise_floor_w=self.noise_floor_w,
            previous_values=previous_values,
            multipliers={cid: graph.multiplier_for(cid) for cid in coerced.readings},
        )
        state_overrides = _device_state_overrides(topology)
        transitions = _collect_transitions(coerced, device_states, state_overrides)
        diagnostics["events"] = [
            {**t.as_dict(), "source": "ha_state_or_previous_state"}
            for t in transitions.values()
        ]
        diagnostics["ct_steps"] = {
            cid: step.as_dict() for cid, step in steps.items() if step.detected
        }
        if self.tracker is not None:
            # Record this cycle *after* detection so it becomes the history for
            # the next call (never the basis of the current one).
            self.tracker.observe(coerced.readings, coerced.at)

        all_states = {did: _state_text(device_states, did) for did in device_ids}
        models_by_device: dict[str, ModelView | None] = {}
        for did in device_ids:
            try:
                models_by_device[did] = resolve_model(
                    models, did, state=all_states.get(did), context=coerced.metadata
                )
            except Exception as exc:  # pragma: no cover - defensive isolation
                models_by_device[did] = None
                diagnostics["errors"][did] = f"model_resolution_failed:{type(exc).__name__}"

        # ---- Level 1: direct measurement ---------------------------------
        direct = resolve_direct(
            coerced, graph, max_age_s=self.max_measurement_age_s, at=coerced.at
        )
        diagnostics["direct_measurements"] = {d: r.as_dict() for d, r in direct.items()}

        # ---- measurement system ------------------------------------------
        matrix = build_measurement_matrix(
            graph, device_ids, noise_floor_w=self.noise_floor_w
        )
        system = self._solve_system(
            matrix,
            graph,
            coerced,
            direct,
            device_ids,
            models_by_device,
            all_states,
            diagnostics,
            state_overrides,
            configs,
        )
        x_fit = system["x"]
        at_upper = system.get("at_upper_bound", set())
        declared_ceiling_hit = system.get("declared_ceiling_hit", set())
        ident: Identifiability = system["identifiability"]
        diagnostics["solver"] = system["solve_result"].as_dict()
        diagnostics["identifiability"] = ident.as_dict()
        diagnostics["matrix"] = {
            "shape": matrix.shape,
            "row_channel_ids": list(matrix.row_channel_ids),
            "col_device_ids": list(matrix.col_device_ids),
            "unobserved_devices": list(matrix.unobserved_devices),
            "nested_children": {k: list(v) for k, v in matrix.nested_children.items()},
            "A": matrix.A.tolist(),
        }

        # ---- concurrent-event ambiguity -----------------------------------
        ambiguous_devices = self._detect_ambiguity(
            transitions, graph, ident, diagnostics
        )

        # ---- per-device attribution --------------------------------------
        results: list[InferenceResult] = []
        for did in device_ids:
            try:
                result = self._infer_device(
                    did,
                    graph=graph,
                    window=coerced,
                    direct=(
                        None
                        if (
                            (cfg := configs.get(did)) is not None
                            and not cfg.allows_direct_measurement
                        )
                        else direct.get(did)
                    ),
                    model=models_by_device.get(did),
                    state=all_states.get(did),
                    transition=transitions.get(did),
                    step=_device_step(graph, did, steps),
                    attribution_step=_attribution_step(graph, did, steps),
                    siblings_stable=_siblings_held(graph, did, transitions),
                    row_is_shared=bool(_row_siblings(graph, did)),
                    fit_value=x_fit.get(did),
                    identifiable=did not in ident.unidentifiable,
                    ambiguous=did in ambiguous_devices,
                    fit_at_ceiling=did in at_upper,
                    declared_ceiling_hit=did in declared_ceiling_hit,
                    state_overrides=state_overrides.get(did),
                    config=configs.get(did),
                )
            except Exception as exc:  # error isolation: one device cannot abort the batch
                diagnostics["errors"][did] = f"{type(exc).__name__}:{exc}"
                result = self._fallback_result(did, coerced.at, exc)
            results.append(result)

        reconciliation = self._reconcile(
            graph, coerced, results, matrix, direct, diagnostics
        )
        diagnostics["accounted_devices"] = [
            r.device_id for r in results if r.source != Source.UNKNOWN
        ]
        diagnostics["unknown_devices"] = [
            r.device_id for r in results if r.source == Source.UNKNOWN
        ]
        return InferenceBatch(
            results=results,
            reconciliation=reconciliation,
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------------ #
    # Solver plumbing
    # ------------------------------------------------------------------ #
    def _solve_system(
        self,
        matrix: MeasurementMatrix,
        graph: TopologyGraph,
        window: CoercedWindow,
        direct: Mapping[str, DirectReading],
        device_ids: list[str],
        models: Mapping[str, ModelView | None],
        states: Mapping[str, str | None],
        diagnostics: dict[str, Any],
        state_overrides: Mapping[str, Mapping[str, str]] | None = None,
        configs: Mapping[str, EffectiveConfig] | None = None,
    ) -> dict[str, Any]:
        state_overrides = state_overrides or {}
        configs = configs or {}
        prepared = matrix.prepare(window.readings)
        n = len(device_ids)
        idx = {d: j for j, d in enumerate(device_ids)}
        mask = prepared.mask.copy()
        excluded = dict(prepared.excluded_rows)

        # Row usability: never use a stale/missing row.  Repeated rows are
        # down-weighted because they are not fresh physical samples.
        weights = np.array(matrix.row_weights, dtype=float)
        for i, cid in enumerate(matrix.row_channel_ids):
            reading = window.readings.get(cid)
            if reading is None:
                mask[i] = False
                excluded.setdefault(cid, "missing_measurement")
                continue
            if not reading.is_usable(self.max_measurement_age_s):
                mask[i] = False
                if reading.age_s > self.max_measurement_age_s:
                    excluded.setdefault(cid, "stale_measurement")
                elif reading.quality in (QualityStatus.MISSING, QualityStatus.BAD):
                    excluded.setdefault(cid, f"quality_{reading.quality}")
                else:
                    excluded.setdefault(cid, "unusable_measurement")
                continue
            if reading.repeated:
                weights[i] *= 0.5

        # Direct powers, used to remove known contributions from y.
        x_direct = np.zeros(n)
        for did, reading in direct.items():
            if reading.usable and reading.power_w is not None and did in idx:
                x_direct[idx[did]] = float(reading.power_w)

        A = np.array(matrix.A, dtype=float)
        y_adj = prepared.y - A @ x_direct

        # Solve only for devices that are not directly measured and that have
        # at least one live coefficient.  Everything else is left at zero.
        direct_usable = {
            did for did, r in direct.items() if r.usable and r.power_w is not None
        }
        solve_cols = [
            j
            for j, did in enumerate(device_ids)
            if did not in direct_usable and np.any(np.abs(A[:, j]) > 0)
        ]
        if not solve_cols:
            x = x_direct.copy()
            solve_result = solve_constrained_nnls(
                np.zeros((0, 0)), np.zeros(0), device_ids=[], analyse=True
            )
            # Identifiability still matters for the non-direct devices.
            ident = analyse_identifiability(
                A[:, [j for j in range(n) if device_ids[j] not in direct_usable]],
                [d for d in device_ids if d not in direct_usable],
            )
            return {
                "x": {d: float(x[idx[d]]) for d in device_ids},
                "index": idx,
                "identifiability": ident,
                "solve_result": solve_result,
            }

        A_sub = A[:, solve_cols]
        y_sub = y_adj
        sub_ids = [device_ids[j] for j in solve_cols]

        # Main CT rows get an explicit non-negative "unknown load" slack.  The
        # main row then constrains the *sum* of known devices without forcing
        # the fit to invent device power for unmodelled household load.  Rows the
        # user declared shared get the same slack for the same reason (ADR-0011).
        shared_rows: list[str] = []
        for did, cfg in configs.items():
            if not cfg.shared_unmodelled:
                continue
            for cid in graph.attribution_ct_ids(did):
                if cid in matrix.row_channel_ids and cid not in shared_rows:
                    shared_rows.append(cid)
        A_aug, slack_names, slack_rows = augment_with_unknown_slack(
            A_sub, matrix.row_channel_ids, matrix.row_kind, shared_rows=shared_rows
        )
        diagnostics["shared_rows_held_as_upper_bounds"] = shared_rows
        n_sub = len(sub_ids)
        row_has_support = np.array(
            [np.any(np.abs(A_aug[i]) > 0) for i in range(A_aug.shape[0])]
        )
        for i, cid in enumerate(matrix.row_channel_ids):
            if not row_has_support[i]:
                mask[i] = False
                if cid not in excluded:
                    excluded[cid] = "direct_only_row"

        upper: list[float | None] = []
        prior: list[float] = []
        state_pinned: list[str] = []
        for j in solve_cols:
            did = device_ids[j]
            view = models.get(did)
            model_power = _state_power(view, states.get(did))
            prior.append(0.0 if model_power is None else max(float(model_power), 0.0))
            # An explicitly OFF device is a *state constraint*: it cannot draw
            # more than its standby power.  Pinning it is what makes a shared
            # branch identifiable (the other device then owns the remainder).
            #
            # The cap is derived from evidence, never from the solver's freedom:
            #
            # * a model or configuration that states a standby draw allows that
            #   draw (plus the measurement tolerance) -- the device may genuinely
            #   consume it while "off";
            # * no stated standby means no evidence of *any* draw, so the cap is
            #   exactly zero.
            #
            # Without the second rule the row was only bounded and the solve was
            # under-determined: two devices on one branch CT returned an
            # arbitrary 190/10 split and credited 10 W to a lamp the user had
            # declared off.  Adding a tolerance floor "so standby can be
            # measured" fabricated attribution out of the solver's least-norm
            # tie-break, which is the one thing this project never does.
            #
            # A declared load signal is evidence in its own right (ADR-0012), so
            # the constraint must not require a learnt model.  Without this, a
            # shared circuit where every device is modelled only by its switch
            # stayed permanently unidentifiable -- the state told us which lamps
            # were dark and the engine ignored it.  The requirement is a
            # *declared* load entity: an undeclared state key proves nothing.
            cfg = configs.get(did)
            has_load_signal = bool(cfg is not None and cfg.load_entity_ids)
            if _is_off(states.get(did), state_overrides.get(did)) and (
                view is not None or has_load_signal
            ):
                standby = 0.0
                if view is not None:
                    if view.params.standby_power_w is not None:
                        standby = abs(float(view.params.standby_power_w))
                    elif view.params.off_power_w is not None:
                        standby = abs(float(view.params.off_power_w))
                if standby <= 0.0 and cfg is not None and cfg.expected_standby_w is not None:
                    standby = abs(float(cfg.expected_standby_w))
                # No evidence of any draw means the cap really is zero.  The
                # bounded solver requires a strictly positive interval, so an
                # exact zero is expressed as the smallest representable cap and
                # the device is reported as pinned at zero.
                upper.append(
                    2.0 * standby if standby > 0.0 else _PINNED_ZERO_W
                )
                state_pinned.append(did)
                continue
            # A model bound is a *prior*, never a cap on a measurement.  When
            # the device is alone in its attribution row the CT is a direct
            # observation of that device, so the measurement wins and no upper
            # bound is applied.  Bounds only help where the device shares a row
            # with others and would otherwise be free to absorb unknown load.
            if view is None or view.upper_bound_w is None or _is_solo_row(matrix, j):
                upper.append(None)
            else:
                upper.append(float(view.upper_bound_w))

        # A shared row carries loads nobody declared, and the solver has no way
        # to tell that surplus from a claimant's own draw: whichever column has
        # the widest bound absorbs it.  Measured on the reference installation,
        # ``lights_ct2`` peaked at 1 036 W while its three fixtures declared
        # 243.6 / 112.3 / 112.7 W; with one fixture ON and its siblings
        # positively OFF the whole row was credited to that one lamp -- 4.25x its
        # declared draw, graded ``tier_3_measured``.
        #
        # A *declared* wattage is the one bound that can prevent this.  Like a
        # state pin, it is user evidence rather than a learnt prior, so it is
        # never widened by the relaxation pass below.  On a solo row the CT is
        # still a direct observation of the device and the measurement wins.
        declared_bound: list[str] = []
        for k, did in enumerate(sub_ids):
            cfg = configs.get(did)
            if cfg is None or cfg.config_ceiling_w is None:
                continue
            j = solve_cols[k]
            if _is_solo_row(matrix, j):
                continue
            if did in state_pinned:
                continue
            # The declaration wins over a learnt model's bound: both are
            # evidence, but only the model's is explicitly revisable, and the
            # user's stated wattage is the stronger claim.
            upper[k] = float(cfg.config_ceiling_w)
            declared_bound.append(did)
        if declared_bound:
            diagnostics["declared_ceiling_applied"] = sorted(declared_bound)

        active_mask = mask & row_has_support
        if not np.any(active_mask):
            diagnostics["warnings"].append("no_usable_measurement_rows")
        active_idx = np.flatnonzero(active_mask)

        # A slack has no model, but it is still bounded by physics: because
        # every device power is >= 0, the unknown part of a main row cannot
        # exceed that row's measured value.  A finite bound also keeps the
        # bounded solver well conditioned (no ``inf`` in the active bounds).
        for row in slack_rows:
            row_value = float(y_sub[row]) if row < len(y_sub) else 0.0
            upper.append(max(0.0, row_value) + 1.0)
        prior.extend([0.0] * len(slack_names))
        all_ids = [*sub_ids, *slack_names]

        if active_idx.size:
            A_act = A_aug[active_idx] * weights[active_idx, None]
            y_act = y_sub[active_idx] * weights[active_idx]
            upper_arr = None
            if any(u is not None for u in upper):
                upper_arr = np.array(
                    [np.inf if u is None else float(u) for u in upper], dtype=float
                )
            ridge = self.model_ridge
            if ridge > 0 and not any(p > 0 for p in prior[:n_sub]):
                ridge = 0.0
            solve_result = solve_constrained_nnls(
                A_act,
                y_act,
                device_ids=all_ids,
                weights=None,
                lower=0.0,
                upper=upper_arr,
                prior=prior if ridge > 0 else None,
                ridge=ridge,
                analyse=False,
            )
            # If a model bound made the fit materially worse than the CT rows
            # allow, relax the bound and refit.  The measurement is trusted over
            # the learnt model, and the relaxation is recorded explicitly.
            #
            # A *state* constraint is never relaxed.  "This lamp is off" is
            # evidence, not a model prior, and the refit exists because the
            # solver's least-norm tie-break otherwise invents a share for an
            # unobserved device.  Relaxing it would reintroduce exactly the
            # fabricated attribution the pin was added to prevent.
            #
            # A *declared* ceiling is treated the same way, for the same reason:
            # the user said what the fixture draws, so a shared row reading more
            # than that is evidence of undeclared load on the circuit, not
            # evidence that the declaration was wrong.
            if solve_result.at_upper_bound:
                relaxable = (
                    set(solve_result.at_upper_bound)
                    - set(state_pinned)
                    - set(declared_bound)
                )
                relaxed = [
                    None if did in relaxable else u
                    for did, u in zip(all_ids, upper, strict=False)
                ]
                if any(u is not None for u in relaxed[:n_sub]):
                    relaxed_arr = np.array(
                        [np.inf if u is None else float(u) for u in relaxed], dtype=float
                    )
                    relaxed_result = solve_constrained_nnls(
                        A_act,
                        y_act,
                        device_ids=all_ids,
                        weights=None,
                        lower=0.0,
                        upper=relaxed_arr,
                        prior=prior if ridge > 0 else None,
                        ridge=ridge,
                        analyse=False,
                    )
                    if relaxed_result.weighted_residual_norm < (
                        0.98 * solve_result.weighted_residual_norm
                    ):
                        diagnostics["warnings"].append("model_upper_bound_relaxed")
                        # Report only what was actually relaxed.  A state-pinned
                        # device keeps its cap even when another device in the
                        # same solve gets a wider bound, so listing it here
                        # would claim evidence was overridden when it was not.
                        diagnostics.setdefault("relaxed_bounds", []).extend(
                            sorted(relaxable)
                        )
                        solve_result = relaxed_result
        else:
            solve_result = solve_constrained_nnls(
                np.zeros((0, len(all_ids))),
                np.zeros(0),
                device_ids=all_ids,
                lower=0.0,
                analyse=False,
            )

        # Identifiability is evaluated on the *device* columns of the same
        # augmented system: nuisance slacks must not be allowed to hide a
        # genuine device ambiguity, and device ambiguity must not be fabricated
        # by the presence of the slack.
        # Only rows that actually contribute to the fit may inform the
        # identifiability analysis.  A row that was excluded for staleness or a
        # missing nested child must not make a device look observable.
        # Identifiability is assessed on the *free* columns only.  A device
        # pinned by an explicit OFF state is fixed by an external constraint,
        # not estimated, so it cannot be part of a null-space ambiguity.
        pinned_set = set(state_pinned)
        free_local = [k for k, did in enumerate(sub_ids) if did not in pinned_set]
        ident_rows = np.flatnonzero(mask & row_has_support)
        ident = analyse_identifiability(
            A_aug[ident_rows][:, free_local + list(range(n_sub, len(all_ids)))],
            [sub_ids[k] for k in free_local] + slack_names,
            weights=weights[ident_rows],
            n_device_columns=len(free_local),
        )
        diagnostics["excluded_rows"].update(excluded)
        diagnostics["active_rows"] = [matrix.row_channel_ids[i] for i in active_idx]
        diagnostics["unknown_slack"] = {
            name.split(":", 1)[1]: float(solve_result.x[n_sub + k])
            for k, name in enumerate(slack_names)
            if k + n_sub < len(solve_result.x)
        }

        x = x_direct.copy()
        for k, j in enumerate(solve_cols):
            x[j] = float(solve_result.x[k]) if k < len(solve_result.x) else 0.0

        # A declared ceiling "binds" only when it is both active *and* the row
        # still carries power the solve could not place.  A lamp that genuinely
        # draws exactly its declared wattage also sits at its upper bound, and
        # that is a measurement, not a cap.  The difference is the residual: with
        # unexplained power on the row, the number the solver returned is the
        # declaration; with none, the row really did measure that much.
        declared_ceiling_hit: set[str] = set()
        if declared_bound and active_idx.size:
            x_dev = np.array(
                [float(solve_result.x[k]) for k in range(len(solve_result.x))],
                dtype=float,
            )
            predicted = A_aug @ x_dev
            for did in declared_bound:
                k = sub_ids.index(did)
                if did not in solve_result.at_upper_bound:
                    continue
                for row_index in active_idx:
                    if abs(float(A_sub[row_index, k])) <= 0.0:
                        continue
                    leftover = float(y_sub[row_index] - predicted[row_index])
                    tolerance = max(
                        3.0,
                        3.0 * float(matrix.row_sigma[row_index])
                        if row_index < len(matrix.row_sigma)
                        else 3.0,
                    )
                    if leftover > tolerance:
                        declared_ceiling_hit.add(did)
                        break
        if declared_ceiling_hit:
            diagnostics["declared_ceiling_hit"] = sorted(declared_ceiling_hit)

        return {
            "x": {d: float(x[idx[d]]) for d in device_ids},
            "index": idx,
            "identifiability": ident,
            "solve_result": solve_result,
            "active_rows": [matrix.row_channel_ids[i] for i in active_idx],
            "state_pinned": state_pinned,
            "at_upper_bound": set(solve_result.at_upper_bound),
            # Devices whose value is the *declared* wattage rather than an
            # observation: the row carried power beyond what the declaration
            # allows, so the surplus is undeclared load.  Callers must not grade
            # such a value as a measurement.
            "declared_ceiling_hit": declared_ceiling_hit,
        }

    # ------------------------------------------------------------------ #
    # Ambiguity
    # ------------------------------------------------------------------ #
    def _detect_ambiguity(
        self,
        transitions: Mapping[str, _Transition],
        graph: TopologyGraph,
        ident: Identifiability,
        diagnostics: dict[str, Any],
    ) -> set[str]:
        changed = [d for d, t in transitions.items() if t.direction is not None]
        if len(changed) < 2:
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

    # ------------------------------------------------------------------ #
    # Per-device hierarchy
    # ------------------------------------------------------------------ #
    def _infer_device(
        self,
        device_id: str,
        *,
        graph: TopologyGraph,
        window: CoercedWindow,
        direct: DirectReading | None,
        model: ModelView | None,
        state: str | None,
        transition: _Transition | None,
        step: StepChange | None,
        attribution_step: StepChange | None = None,
        siblings_stable: bool = False,
        row_is_shared: bool = False,
        fit_value: float | None,
        identifiable: bool,
        ambiguous: bool,
        fit_at_ceiling: bool = False,
        declared_ceiling_hit: bool = False,
        state_overrides: Mapping[str, str] | None = None,
        config: EffectiveConfig | None = None,
    ) -> InferenceResult:
        discipline = graph.resolve(device_id)
        topology_status = str(discipline.topology_status)
        now = window.at
        own_event = transition is not None and transition.direction is not None
        concurrent_events = len(window.state_transitions)
        monitoring = config.monitoring if config is not None else MonitoringLevel.UNKNOWN
        behavior = config.behavior if config is not None else PowerBehavior.UNKNOWN
        config_notes = list(config.notes) if config is not None else []
        # A state value is only evidence when the topology declares a load entity
        # for this device; an undeclared key in ``device_states`` proves nothing.
        has_load_signal = bool(config is not None and config.load_entity_ids)
        state_on = has_load_signal and _is_on(state, state_overrides)
        state_off = has_load_signal and _is_off(state, state_overrides)
        # A shared CT is not this device's measurement, however the topology
        # labels it: the user has stated that siblings share the circuit, so the
        # row is an upper bound rather than an observation (ADR-0011 rule 2).
        direct_is_ours = config is None or config.allows_direct_measurement
        # Measured dimmer output for *this* device, when the control entity
        # reported one.  ``None`` means "not reported", never "full brightness":
        # a light that is merely ``on`` must not be read as 100 % output.
        level = _measured_level(window.metadata, device_id)

        def finish(
            result: InferenceResult,
            *,
            model_corroborates: bool = False,
            configured_prior: bool = False,
            prior_reason: str = "",
            measured_step: bool = False,
            degraded: bool = False,
        ) -> InferenceResult:
            """Attach the evidence grade to a result without changing its score."""
            tier, score, reasons = grade(
                TierInputs(
                    source=Source(result.source),
                    monitoring=monitoring,
                    behavior=behavior,
                    state_says_on=state_on,
                    state_says_off=state_off,
                    has_declared_load_signal=has_load_signal,
                    model_corroborates=model_corroborates,
                    configured_prior=configured_prior,
                    prior_reason=prior_reason,
                    measured_step=measured_step,
                    stale=result.measurement_age_seconds > self.max_measurement_age_s,
                    degraded=degraded,
                ),
                result.confidence,
            )
            result.confidence = score
            # ``use_enum_values`` only coerces on validation, not on assignment, so
            # assign the plain value to keep every downstream consumer (API JSON,
            # SQLite, comparisons) seeing the same string form.
            result.confidence_tier = (
                tier.value if hasattr(tier, "value") else str(tier)
            )
            result.monitoring_level = (
                monitoring.value if hasattr(monitoring, "value") else str(monitoring)
            )
            result.power_behavior = (
                behavior.value if hasattr(behavior, "value") else str(behavior)
            )
            result.tier_reasons = [*config_notes, *reasons]
            return result

        # ---------------- Level 1: direct measurement --------------------
        if direct_is_ours and direct is not None and direct.usable and direct.power_w is not None:
            explanation = ["direct_ct_measurement"]
            # ``resolve_direct`` clips a negative device-level sum to zero and
            # records the signed value in ``raw_power_w``.  Surface both facts so
            # a consumer can see *why* a zero is reported and can reconcile the
            # reading against the raw sensor value.  Export stays a main-side
            # property; it is never attributed to a device.
            if direct.raw_power_w is not None:
                explanation.append("negative_device_power_clipped")
            if direct.repeated:
                explanation.append("repeated_state_update")
            age = direct.age_s
            quality = _quality_from_age(age, direct.quality)
            stale = age > self.max_measurement_age_s
            # Conflict between a fresh state transition and a direct value is
            # surfaced, but a direct measurement is never replaced.
            conflict = False
            expected = _state_power(model, state)
            if own_event and expected is not None:
                tolerance = max(0.5 * abs(expected), 25.0)
                if abs(direct.power_w - expected) > tolerance:
                    conflict = True
                    explanation.append("state_direct_measurement_conflict")
            confidence = compute_confidence(
                ConfidenceInputs(
                    source=Source.DIRECT_MEASURED,
                    measurement_qualities=[quality],
                    measurement_ages_s=[age],
                    supporting_ct_count=len(direct.ct_ids),
                    model_quality=0.0 if model is None else model.quality,
                    event_isolation=event_isolation_score(
                        own_event=own_event,
                        concurrent_events=concurrent_events,
                        ambiguous=False,
                    ),
                    topology_status=topology_status,
                    residual_w=0.0,
                    residual_sigma_w=self.noise_floor_w,
                    stale=stale,
                    missing_rows=False,
                    ambiguous=False,
                    repeated=direct.repeated,
                )
            )
            stage, stage_status = _stage_for(model, direct.power_w)
            return finish(
                InferenceResult(
                    device_id=device_id,
                    timestamp=now,
                    power_w=float(direct.power_w),
                    source=Source.DIRECT_MEASURED,
                    confidence=(
                        confidence.score if not conflict else min(confidence.score, 0.6)
                    ),
                    model_id=None if model is None else model.model_id,
                    model_version=None if model is None else model.version,
                    operating_stage=stage,
                    stage_status=stage_status,
                    sample_count=0 if model is None else model.sample_count,
                    quality_status=quality,
                    measurement_age_seconds=age,
                    supporting_ct_channels=list(direct.ct_ids),
                    explanation=explanation,
                    provenance={
                        "direct_reasons": list(direct.reasons),
                        "per_leg_w": dict(direct.per_leg_w),
                        "raw_power_w": direct.raw_power_w,
                        "confidence": confidence.as_dict(),
                    },
                    confidence_is_calibrated=False,
                ),
                model_corroborates=(
                    expected is not None
                    and not conflict
                    and abs(direct.power_w - expected)
                    <= max(MODEL_MATCH_REL_TOL * abs(expected), 25.0)
                ),
            )

        # ---------------- Level 2/3/4: no usable direct value -------------
        explanation: list[str] = []
        if direct is not None and not direct.usable:
            explanation.append("direct_ct_unusable")
            explanation.extend(direct.reasons)
        if own_event:
            explanation.append(
                "ha_state_transition_activation"
                if transition and transition.direction == "activation"
                else "ha_state_transition_deactivation"
            )
        if step is not None and own_event and transition is not None:
            agrees = (
                transition.direction == "activation" and step.delta_w > 0
            ) or (transition.direction == "deactivation" and step.delta_w < 0)
            if agrees:
                explanation.append(
                    "branch_power_increase" if step.delta_w > 0 else "branch_power_decrease"
                )
        if ambiguous:
            explanation.append("ambiguous_concurrent_events")

        supporting = [cid for cid in graph.device_channels(device_id)]
        sigma = self._support_sigma(graph, supporting)
        ages = [
            window.readings[cid].age_s
            for cid in supporting
            if cid in window.readings
        ]
        qualities = [
            window.readings[cid].quality
            for cid in supporting
            if cid in window.readings
        ]
        if not qualities:
            qualities = [QualityStatus.MISSING]
        if not ages:
            ages = [float("inf")]
        stale = any(a > self.max_measurement_age_s for a in ages)
        repeated = any(
            window.readings[cid].repeated
            for cid in supporting
            if cid in window.readings
        )
        quality = _quality_from_age(max(ages), _worst(qualities))
        isolation = event_isolation_score(
            own_event=own_event,
            concurrent_events=concurrent_events,
            ambiguous=ambiguous,
        )

        # Ambiguity wins over every estimate.
        if ambiguous:
            confidence = compute_confidence(
                ConfidenceInputs(
                    source=Source.UNKNOWN,
                    measurement_qualities=qualities,
                    measurement_ages_s=ages,
                    supporting_ct_count=len(supporting),
                    model_quality=0.0 if model is None else model.quality,
                    event_isolation=isolation,
                    topology_status=topology_status,
                    residual_w=0.0,
                    residual_sigma_w=sigma,
                    stale=stale,
                    ambiguous=True,
                )
            )
            return finish(
                self._unknown_result(
                    device_id,
                    now,
                    confidence.score,
                    quality,
                    supporting,
                    explanation,
                    provenance={
                        "ambiguity": "concurrent_events_not_separable",
                        "confidence": confidence.as_dict(),
                    },
                ),
                degraded=True,
            )

        fit = 0.0 if fit_value is None else max(float(fit_value), 0.0)
        model_power = _state_power(model, state)
        standby = 0.0
        if model is not None and model.params.standby_power_w is not None:
            standby = abs(float(model.params.standby_power_w))
        off_tolerance = max(self.off_tolerance_w, 2.0 * standby)

        # ---------------- Level 2a: measured step (ADR-0026) --------------
        # A shared row cannot be split by linear algebra, but a *step* on it can
        # be read: the row moved by exactly this many watts while this device
        # changed state, and no other device on the row changed.  That movement
        # is the device's draw, measured.
        #
        # This is deliberately weaker than ADR-0023's "the single ON claimant
        # owns the row", which required every sibling to be positively OFF and
        # therefore credited the whole circuit to one lamp whenever the fixture
        # list was incomplete.  Here a sibling may be running; it only has to
        # have held steady across the transition.
        #
        # Only an *activation* is attributed.  On a deactivation the step tells
        # us what the device drew a moment ago, not what it draws now -- it is
        # off, and reporting the step as its current power would both be wrong
        # for the device page and double count against the row that already
        # dropped.  The deactivation is marked in the explanation so the evidence
        # is visible without being reported as present power.
        step_w = 0.0
        if (
            attribution_step is not None
            and row_is_shared
            and siblings_stable
            and has_load_signal
            and transition is not None
            and transition.direction == "activation"
            and attribution_step.delta_w > 0.0
            and not stale
            and not state_off
        ):
            step_w = float(attribution_step.delta_w)
            row_now = window.readings.get(attribution_step.channel_id)
            row_value = None if row_now is None else row_now.signed_w()
            if row_value is not None:
                # The step is a change *on* the row, so it cannot be larger than
                # the row itself.  Clamping here is a physics guard against a
                # mis-scaled or mis-signed reading, not an estimate.
                step_w = min(step_w, abs(float(row_value)))
            # A stated wattage is a non-relaxable upper bound (ADR-0025), and it
            # applies to a measured step exactly as it applies to a fit.  A
            # 400 W step on a row whose lamp declares 50 W is not that lamp's
            # 50 W lamp being measured; the surplus belongs to something else on
            # the circuit, so the step is not this device's draw at all.
            ceiling = None if config is None else config.config_ceiling_w
            if ceiling is not None and step_w > float(ceiling):
                explanation.append("step_exceeds_declared_wattage")
                step_w = 0.0
            if step_w > 0.0:
                explanation.append("shared_circuit_step_measured")
                if attribution_step.source == "previous_snapshot":
                    explanation.append("step_from_previous_snapshot")
                confidence = compute_confidence(
                    ConfidenceInputs(
                        source=Source.INFERRED,
                        measurement_qualities=qualities,
                        measurement_ages_s=ages,
                        supporting_ct_count=len(
                            _used_support(graph, device_id, supporting)
                        ),
                        model_quality=0.0 if model is None else model.quality,
                        event_isolation=isolation,
                        topology_status=topology_status,
                        residual_w=0.0,
                        residual_sigma_w=float(attribution_step.noise_w),
                        repeated=repeated,
                    )
                )
                stage, stage_status = _stage_for(model, step_w)
                return finish(
                    InferenceResult(
                        device_id=device_id,
                        timestamp=now,
                        power_w=step_w,
                        source=Source.INFERRED,
                        confidence=confidence.score,
                        model_id=None if model is None else model.model_id,
                        model_version=None if model is None else model.version,
                        operating_stage=stage,
                        stage_status=stage_status,
                        sample_count=0 if model is None else model.sample_count,
                        quality_status=quality,
                        measurement_age_seconds=max(ages) if ages else 0.0,
                        supporting_ct_channels=[attribution_step.channel_id],
                        explanation=explanation,
                        provenance={
                            "measured_step_w": step_w,
                            "step_channel_id": attribution_step.channel_id,
                            "step_before_w": attribution_step.before_w,
                            "step_after_w": attribution_step.after_w,
                            "step_threshold_w": attribution_step.threshold_w,
                            "step_source": attribution_step.source,
                            "siblings_stable": True,
                            "siblings": list(_row_siblings(graph, device_id)),
                            "confidence": confidence.as_dict(),
                        },
                        confidence_is_calibrated=False,
                    ),
                    measured_step=True,
                )
        if (
            attribution_step is not None
            and row_is_shared
            and siblings_stable
            and attribution_step.delta_w < 0.0
            and transition is not None
            and transition.direction == "deactivation"
        ):
            # Keep the measurement as evidence without reporting it as current
            # power: the device is off, and the row that dropped has already
            # dropped.  Echoing the step now would double count it against the
            # row and claim the device is drawing when it is not.  The magnitude
            # is recorded so learning can use it.
            explanation.append("shared_circuit_step_measured_on_deactivation")

        # ---------------- Level 2: inferred ------------------------------
        # A row the user declared shared carries power that belongs to siblings we
        # do not model.  The solver already protects the *sum* with a slack
        # column, but a device within it must not absorb the remainder: that would
        # report a lights circuit's whole draw as one lamp.  Such a value is only
        # ever a bounded prior at Level 3b.  When the siblings *are* modelled the
        # row is legitimately separable and inference is allowed.
        shared_attribution_blocked = config is not None and config.shared_unmodelled
        if shared_attribution_blocked:
            explanation.append("shared_circuit_not_individually_measured")
        # A fit pinned at the device's *declared* wattage is not a measurement of
        # the device.  The solver reached the user's own number because the row
        # carried more power than the declaration allows, so the surplus is
        # undeclared load and the honest answer is the declaration itself, graded
        # as a configured prior -- never "a CT measured it".
        declared_bound_blocked = declared_ceiling_hit and config is not None
        if declared_bound_blocked:
            explanation.append("declared_wattage_bounds_row_attribution")
        if (
            identifiable
            and not stale
            and fit > 0.0
            and not shared_attribution_blocked
            and not declared_bound_blocked
        ):
            if _is_off(state, state_overrides) and (fit_at_ceiling or fit > off_tolerance):
                # The circuit carries power, but this device says OFF.  Do not
                # invent an attribution; preserve it as unknown residual.
                explanation.append("unexplained_load_on_device_circuit")
                confidence = compute_confidence(
                    ConfidenceInputs(
                        source=Source.UNKNOWN,
                        measurement_qualities=qualities,
                        measurement_ages_s=ages,
                        supporting_ct_count=len(supporting),
                        model_quality=0.0 if model is None else model.quality,
                        event_isolation=isolation,
                        topology_status=topology_status,
                        residual_w=fit,
                        residual_sigma_w=sigma,
                        stale=stale,
                    )
                )
                return finish(
                    self._unknown_result(
                        device_id,
                        now,
                        confidence.score,
                        quality,
                        supporting,
                        explanation,
                        provenance={
                            "unexplained_power_w": fit,
                            "reason": "device_reported_off",
                            "confidence": confidence.as_dict(),
                        },
                    ),
                    degraded=True,
                )
            explanation.append("constrained_nnls_fit")
            if repeated:
                explanation.append("repeated_state_update")
            if model is not None and model_power is not None:
                tolerance = max(MODEL_MATCH_REL_TOL * max(abs(model_power), 1.0), 25.0)
                if abs(fit - model_power) <= tolerance:
                    explanation.append("historical_model_match")
                else:
                    explanation.append("measurement_disagrees_with_model")
            confidence = compute_confidence(
                ConfidenceInputs(
                    source=Source.INFERRED,
                    measurement_qualities=qualities,
                    measurement_ages_s=ages,
                    supporting_ct_count=len(_used_support(graph, device_id, supporting)),
                    model_quality=0.0 if model is None else model.quality,
                    event_isolation=isolation,
                    topology_status=topology_status,
                    residual_w=0.0,
                    residual_sigma_w=sigma,
                    repeated=repeated,
                )
            )
            stage, stage_status = _stage_for(model, fit)
            agrees = (
                model is not None
                and model_power is not None
                and abs(fit - model_power)
                <= max(MODEL_MATCH_REL_TOL * max(abs(model_power), 1.0), 25.0)
            )
            return finish(
                InferenceResult(
                    device_id=device_id,
                    timestamp=now,
                    power_w=fit,
                    source=Source.INFERRED,
                    confidence=confidence.score,
                    model_id=None if model is None else model.model_id,
                    model_version=None if model is None else model.version,
                    operating_stage=stage,
                    stage_status=stage_status,
                    sample_count=0 if model is None else model.sample_count,
                    quality_status=quality,
                    measurement_age_seconds=max(ages) if ages else 0.0,
                    supporting_ct_channels=supporting,
                    explanation=explanation,
                    provenance={
                        "fit_w": fit,
                        "identifiable": True,
                        "confidence": confidence.as_dict(),
                    },
                    confidence_is_calibrated=False,
                ),
                model_corroborates=agrees,
                degraded=repeated or (config.shared_unmodelled if config else False),
            )

        # ---------------- Level 3: model estimate ------------------------
        model_usable = (
            model is not None
            and model_power is not None
            and (not _is_off(state, state_overrides) or model_power > 0.0)
        )
        if model_usable:
            explanation.append("historical_model_match")
            if not identifiable:
                explanation.append("measurements_do_not_identify_device")
            if stale:
                explanation.append("stale_measurement")
            if fit > 0.0 and identifiable:
                explanation.append("model_below_detection_or_zero_fit")
            confidence = compute_confidence(
                ConfidenceInputs(
                    source=Source.MODEL_ESTIMATED,
                    measurement_qualities=qualities,
                    measurement_ages_s=ages,
                    supporting_ct_count=len(supporting),
                    model_quality=model.quality,
                    event_isolation=isolation,
                    topology_status=topology_status,
                    residual_w=abs(fit - float(model_power)) if fit else 0.0,
                    residual_sigma_w=sigma,
                    stale=stale,
                    missing_rows=not supporting,
                    ambiguous=False,
                    repeated=repeated,
                )
            )
            stage, stage_status = _stage_for(model, float(model_power))
            return finish(
                InferenceResult(
                    device_id=device_id,
                    timestamp=now,
                    power_w=float(model_power),
                    source=Source.MODEL_ESTIMATED,
                    confidence=confidence.score,
                    model_id=model.model_id,
                    model_version=model.version,
                    operating_stage=stage,
                    stage_status=stage_status,
                    sample_count=model.sample_count,
                    quality_status=quality,
                    measurement_age_seconds=0.0 if not ages else max(ages),
                    supporting_ct_channels=supporting,
                    explanation=explanation,
                    provenance={
                        "model_power_w": float(model_power),
                        "identifiable": False,
                        "fit_w": fit,
                        "confidence": confidence.as_dict(),
                    },
                    confidence_is_calibrated=False,
                )
            )

        # ---------- Level 3b: configured prior (ADR-0011) ----------------
        # Nothing measured or modelled this device, but the user has stated a
        # watt value and the device's own control signal says it is running.
        # This is a *bounded prior*, weaker than any measurement: it may explain
        # part of the residual but it never claims to have observed anything.
        # A shared circuit with unmodelled siblings is exactly the case this
        # exists for: the CT row includes neighbours, so it cannot be attributed,
        # yet a declared ``on_w`` is honest knowledge worth reporting.
        prior_power = None
        prior_reason = ""
        if config is not None and not state_off:
            resolved = configured_power(
                config, state, state_is_on=state_on, level=level
            )
            if resolved is not None:
                prior_power, prior_reason = resolved
        if prior_power is not None and prior_power > 0.0:
            explanation.append(prior_reason)
            if config is not None and config.shared_unmodelled:
                explanation.append("shared_circuit_not_attributable_from_ct")
            if config is not None and config.expected_standby_w:
                prior_power = max(float(prior_power), float(config.expected_standby_w))
            confidence = compute_confidence(
                ConfidenceInputs(
                    source=Source.CONFIGURED_PRIOR,
                    measurement_qualities=qualities,
                    measurement_ages_s=ages,
                    supporting_ct_count=0,
                    model_quality=0.0 if model is None else model.quality,
                    event_isolation=isolation,
                    topology_status=topology_status,
                    residual_w=0.0,
                    residual_sigma_w=sigma,
                    stale=stale,
                    missing_rows=not supporting,
                    repeated=repeated,
                )
            )
            return finish(
                InferenceResult(
                    device_id=device_id,
                    timestamp=now,
                    power_w=float(prior_power),
                    source=Source.CONFIGURED_PRIOR,
                    confidence=confidence.score,
                    sample_count=0 if model is None else model.sample_count,
                    quality_status=quality,
                    measurement_age_seconds=0.0 if not ages else max(ages),
                    supporting_ct_channels=supporting,
                    explanation=explanation,
                    provenance={
                        "configured_power_w": float(prior_power),
                        "configuration_source": prior_reason,
                        "fit_w": fit,
                        "confidence": confidence.as_dict(),
                    },
                    confidence_is_calibrated=False,
                ),
                configured_prior=True,
                prior_reason=prior_reason,
                degraded=stale,
            )

        # ---------------- Level 4: unknown -------------------------------
        if identifiable and stale:
            explanation.append("stale_measurement")
        elif not identifiable:
            explanation.append("insufficient_independent_measurements")
        if config is not None and config.mode_entity_ids:
            explanation.append("only_mode_signals_declared")
        if not explanation:
            explanation.append("insufficient_evidence")
        confidence = compute_confidence(
            ConfidenceInputs(
                source=Source.UNKNOWN,
                measurement_qualities=qualities,
                measurement_ages_s=ages,
                supporting_ct_count=len(supporting),
                model_quality=0.0 if model is None else model.quality,
                event_isolation=isolation,
                topology_status=topology_status,
                residual_w=0.0,
                residual_sigma_w=sigma,
                stale=stale,
                repeated=repeated,
            )
        )
        return finish(
            self._unknown_result(
                device_id,
                now,
                confidence.score,
                quality,
                supporting,
                explanation,
                provenance={"fit_w": fit, "confidence": confidence.as_dict()},
            ),
            degraded=stale or repeated,
        )

    # ------------------------------------------------------------------ #
    # Reconciliation
    # ------------------------------------------------------------------ #
    def _reconcile(
        self,
        graph: TopologyGraph,
        window: CoercedWindow,
        results: Sequence[InferenceResult],
        matrix: MeasurementMatrix,
        direct: Mapping[str, DirectReading],
        diagnostics: dict[str, Any],
    ) -> Reconciliation:
        direct_w = sum(r.power_w for r in results if r.source == Source.DIRECT_MEASURED)
        inferred_w = sum(r.power_w for r in results if r.source == Source.INFERRED)
        model_w = sum(r.power_w for r in results if r.source == Source.MODEL_ESTIMATED)
        prior_w = sum(r.power_w for r in results if r.source == Source.CONFIGURED_PRIOR)
        accounted = direct_w + inferred_w + model_w + prior_w

        main: MainPower = main_power_w(
            graph, window.readings, phase_split_l1=self.phase_split_l1
        )
        diagnostics["main_power"] = main.as_dict()

        if main.errors or not main.usable:
            unknown_residual = 0.0
            diagnostics["warnings"].append("main_power_unavailable")
        else:
            unknown_residual = main.total_w - accounted

        # Measurement-fit error: how well all attributed device power explains
        # the CT rows.  This is a *fit* diagnostic, not the power balance.
        prepared = matrix.prepare(window.readings)
        # matrix columns are the topology device order; build the vector by id.
        by_id = {r.device_id: r.power_w for r in results}
        x_all = np.array([by_id.get(d, 0.0) for d in matrix.col_device_ids], dtype=float)
        predictions = matrix.predict(x_all)
        row_errors = np.abs(prepared.y - predictions)
        # A topology whose devices are only visible through the home total has no
        # measurement rows at all.  ``np.array([])`` is float64 while ``mask`` is
        # bool, and ``&`` between them raises instead of yielding an empty result,
        # so build the row-support mask with an explicit dtype (defect #22).
        row_support = np.array(
            [np.any(np.abs(matrix.A[i]) > 0) for i in range(matrix.shape[0])],
            dtype=bool,
        )
        usable_rows = prepared.mask & row_support
        recon_error = float(np.sum(row_errors[usable_rows])) if np.any(usable_rows) else 0.0
        uncertainty = float(
            np.sqrt(np.sum(matrix.row_sigma[usable_rows] ** 2))
        ) if np.any(usable_rows) else 0.0

        denom = abs(main.total_w) if abs(main.total_w) > 1e-9 else None
        ratio = recon_error / denom if denom else 0.0
        if main.complete and main.total_w > 0:
            accounted_fraction = min(max(accounted / main.total_w, 0.0), 1.0)
        else:
            accounted_fraction = 0.0

        quality = QualityStatus.GOOD
        if (
            diagnostics["errors"]
            or diagnostics["ambiguity"]["ambiguous"]
            or diagnostics.get("excluded_rows")
            or main.missing_channels
            or main.errors
        ):
            quality = QualityStatus.DEGRADED
        if not main.complete:
            quality = QualityStatus.MISSING if not main.usable else QualityStatus.DEGRADED

        explanation = [
            "main_power_sum_of_parallel_mains",
            "residual_never_redistributed",
        ]
        if main.has_export:
            explanation.append("negative_power_export_preserved")
        if main.missing_channels:
            explanation.append("main_measurement_incomplete")
        if diagnostics["ambiguity"]["ambiguous"]:
            explanation.append("ambiguous_concurrent_events")
        if diagnostics["errors"]:
            explanation.append("per_device_error_isolated")
        # A negative residual means accounted power exceeds the measured main.
        # That is physically impossible for a snapshot taken at one instant, so
        # it is always a *sample-vintage* artefact: the mains and the branch CTs
        # were sampled at different times.  It is never evidence of double
        # counting (nested children are subtracted, mains are summed once), and
        # the negative value is reported as-is rather than clamped to zero,
        # because hiding it would hide a real data-quality problem.
        if unknown_residual < -max(1.0, 0.002 * abs(main.total_w)):
            explanation.append("negative_residual_sample_vintage_skew")
            diagnostics["warnings"].append("negative_residual_sample_vintage_skew")

        return Reconciliation(
            timestamp=window.at,
            topology_version=graph.topology.version,
            main_power_w=main.total_w,
            l1_power_w=main.l1_w,
            l2_power_w=main.l2_w,
            direct_measured_w=direct_w,
            inferred_w=inferred_w,
            model_estimated_w=model_w,
            configured_prior_w=prior_w,
            unknown_residual_w=unknown_residual,
            reconciliation_error_w=recon_error,
            reconciliation_error_ratio=ratio,
            accounted_fraction=accounted_fraction,
            measurement_uncertainty_w=uncertainty,
            quality_status=quality,
            explanation=explanation,
        )

    # ------------------------------------------------------------------ #
    # Small helpers
    # ------------------------------------------------------------------ #
    def _support_sigma(self, graph: TopologyGraph, channels: Sequence[str]) -> float:
        vals = []
        for cid in channels:
            ch = graph.channels.get(cid)
            if ch is None:
                continue
            res = ch.resolution_w or 0.0
            rel = ch.reliability or 1.0
            vals.append(max(self.noise_floor_w, res / 2.0) / max(rel, 0.05))
        return float(np.sqrt(sum(v * v for v in vals))) if vals else self.noise_floor_w

    def _unknown_result(
        self,
        device_id: str,
        at: datetime,
        confidence: float,
        quality: QualityStatus,
        supporting: Sequence[str],
        explanation: Sequence[str],
        *,
        provenance: Mapping[str, Any] | None = None,
    ) -> InferenceResult:
        return InferenceResult(
            device_id=device_id,
            timestamp=at,
            power_w=0.0,
            source=Source.UNKNOWN,
            confidence=min(max(confidence, 0.0), 0.25),
            quality_status=quality,
            supporting_ct_channels=list(supporting),
            explanation=list(dict.fromkeys([*explanation, "power_left_unattributed"])),
            provenance={
                **dict(provenance or {}),
                "power_w_is_placeholder": True,
                "actual_power_accounted_in": "Reconciliation.unknown_residual_w",
            },
            confidence_is_calibrated=False,
        )

    def _fallback_result(self, device_id: str, at: datetime, exc: Exception) -> InferenceResult:
        return InferenceResult(
            device_id=device_id,
            timestamp=at,
            power_w=0.0,
            source=Source.UNKNOWN,
            confidence=0.05,
            quality_status=QualityStatus.BAD,
            explanation=["device_inference_error", "power_left_unattributed"],
            provenance={
                "error": type(exc).__name__,
                "power_w_is_placeholder": True,
            },
            confidence_is_calibrated=False,
        )


def _is_solo_row(matrix: MeasurementMatrix, col: int) -> bool:
    """True when exactly one device has a non-zero coefficient in each of the
    device's attribution rows (that row measures this device and nothing else)."""
    rows = np.flatnonzero(np.abs(matrix.A[:, col]) > 0)
    if rows.size == 0:
        return False
    for row in rows:
        others = np.count_nonzero(np.abs(np.delete(matrix.A[row], col)) > 0)
        if others > 0:
            return False
    return True


def _worst(qualities: Sequence[QualityStatus]) -> QualityStatus:
    order = {
        QualityStatus.GOOD: 0,
        QualityStatus.DEGRADED: 1,
        QualityStatus.STALE: 2,
        QualityStatus.MISSING: 3,
        QualityStatus.BAD: 4,
    }
    return max(qualities, key=lambda q: order.get(q, 4))


def _used_support(
    graph: TopologyGraph, device_id: str, supporting: Sequence[str]
) -> list[str]:
    attribution = set(graph.attribution_ct_ids(device_id))
    used = [c for c in supporting if c in attribution]
    return used or list(supporting)

