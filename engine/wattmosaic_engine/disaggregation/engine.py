"""Level 1-4 load disaggregation engine (spec §9).

Public contract (``docs/interface_contracts.md`` §3.4)::

    DisaggregationEngine().infer(
        window, *, device_states, models, topology
    ) -> InferenceBatch

Pipeline
--------
1. Validate/index the topology (``TopologyGraph``).
2. Build ``y = A x`` (``build_measurement_matrix``), residualising nested CTs
   so no physical quantity is counted twice.
3. Solve for every device with bounded NNLS.
4. SVD identifiability analysis decides what may be called ``inferred``.
5. Event detection (observed cadence, MAD threshold) provides evidence and
   concurrency/ambiguity detection.
6. Assign the measurement hierarchy per device, with per-device isolation.
7. Reconcile: ``unknown_residual = main - accounted`` and never redistribute.

Nothing in this module mutates the topology or calls Home Assistant.

Split (X02)
-----------
The heavy helpers now live in cohesive sibling modules.  ``infer`` below is the
wiring; the module-level and private methods that used to hold the logic remain
as thin delegating shims (so existing callers and test patches keep working).
The new modules never import this one, so the dependency direction is one-way.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from wattmosaic_engine.contracts import (
    CONTRACT_VERSION,
    InferenceResult,
    QualityStatus,
    Reconciliation,
    Source,
    Topology,
)

# ``_PINNED_ZERO_W`` is an implementation detail of the solver now; import it
# here so callers that reached in through ``engine`` keep seeing the same value.
from wattmosaic_engine.disaggregation._shared import (
    _PINNED_ZERO_W,
    MODEL_MATCH_REL_TOL,
    OFF_ATTRIBUTION_TOLERANCE_W,
    OFF_STATES,
    ON_STATES,
    SOURCE_IDS,
    UNKNOWN_STATES,
    _is_off,
    _is_on,
)
from wattmosaic_engine.disaggregation.absolute_solve import (
    _declared_leg_partner,
    _exclude_partial_legs,
    _solve_system,
)
from wattmosaic_engine.disaggregation.ambiguity import (
    _detect_ambiguity,
    _group_bounds,
)
from wattmosaic_engine.disaggregation.configuration import effective_configs
from wattmosaic_engine.disaggregation.device_hierarchy import (
    _fallback_result,
    _infer_device,
    _support_sigma,
    _unknown_result,
)
from wattmosaic_engine.disaggregation.events import StepChange, StepTracker, detect_changes
from wattmosaic_engine.disaggregation.ledger import _source_ledger
from wattmosaic_engine.disaggregation.reconcile import (
    _bound_configured_priors_by_row,
    _reconcile,
)
from wattmosaic_engine.disaggregation.transitions import (
    _anchor_steps,
    _attribution_step,
    _collect_transitions,
    _device_state_overrides,
    _device_step,
    _normalize_overrides,
    _row_siblings,
    _siblings_held,
    _state_text,
    _Transition,
    _transition_direction,
)
from wattmosaic_engine.electrical.direct import DEFAULT_MAX_LEG_SKEW_S
from wattmosaic_engine.electrical.matrix import MeasurementMatrix, build_measurement_matrix
from wattmosaic_engine.electrical.models import ModelView, resolve_model
from wattmosaic_engine.electrical.solver import Identifiability
from wattmosaic_engine.electrical.topology import TopologyGraph, build_graph
from wattmosaic_engine.electrical.window import CoercedWindow, coerce_window

# Re-exports kept for backwards compatibility with callers that imported these
# names from ``engine`` before the X02 split.  Their definitions now live in the
# cohesive sibling modules; these aliases are the same objects, not copies.
__all__ = [
    "DisaggregationEngine",
    "InferenceBatch",
    "MODEL_MATCH_REL_TOL",
    "OFF_ATTRIBUTION_TOLERANCE_W",
    "OFF_STATES",
    "ON_STATES",
    "SOURCE_IDS",
    "UNKNOWN_STATES",
    "_PINNED_ZERO_W",
    "_Transition",
    "_anchor_steps",
    "_attribution_step",
    "_collect_transitions",
    "_declared_leg_partner",
    "_detect_ambiguity",
    "_device_state_overrides",
    "_device_step",
    "_exclude_partial_legs",
    "_fallback_result",
    "_group_bounds",
    "_infer_device",
    "_is_off",
    "_is_on",
    "_normalize_overrides",
    "_reconcile",
    "_row_siblings",
    "_siblings_held",
    "_solve_system",
    "_source_ledger",
    "_state_text",
    "_support_sigma",
    "_transition_direction",
    "_unknown_result",
]


@dataclass(slots=True)
class InferenceBatch:
    results: list[InferenceResult]
    reconciliation: Reconciliation
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def by_device(self) -> dict[str, InferenceResult]:
        return {r.device_id: r for r in self.results}

    def get(self, device_id: str) -> InferenceResult | None:
        return self.by_device().get(device_id)


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
        max_leg_skew_s: float = DEFAULT_MAX_LEG_SKEW_S,
    ) -> None:
        self.max_measurement_age_s = float(max_measurement_age_s)
        #: How far apart in time the two legs of a declared double-pole pair
        #: may be before the pair stops describing one instant.  Two legs of
        #: one circuit are sampled within a cycle of each other; a skew larger
        #: than this means one leg is a leftover from an earlier poll, so the
        #: row is a mix of two instants and neither leg may be used.
        #:
        #: The limit itself is defined once, in
        #: :mod:`wattmosaic_engine.electrical.direct`, so the documented rule and
        #: the engine default cannot drift apart.
        self.max_leg_skew_s = float(max_leg_skew_s)
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
        residual_events: Sequence[Any] | None = None,
    ) -> InferenceBatch:
        """Run one inference cycle and publish a source ledger on every result.

        ``residual_events`` is additive (keyword-only, default ``None``): an
        optional stream of unknown-residual events from the residual production
        path.  Supplying it lets the ledger name ``residual_events`` as used;
        existing callers are unaffected.
        """
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
            "contract": CONTRACT_VERSION,
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
            noise_floor_w=self._noise_floors(coerced),
            previous_values=previous_values,
            multipliers={cid: graph.multiplier_for(cid) for cid in coerced.readings},
        )
        state_overrides = _device_state_overrides(topology)
        transitions = _collect_transitions(coerced, device_states, state_overrides)
        # ---- anchored step detection -------------------------------------
        # A CT step only explains a device if it happened when the device
        # switched.  For every transition HA timestamped itself, rescan the
        # channels that observe that device with the transition instant as an
        # anchor, and drop unanchored steps on those channels that sit outside
        # the anchor window.  Without this, a lamp switching on could claim a
        # CT step tens of seconds away and be credited with another circuit's
        # power.
        steps = self._anchor_steps(
            steps,
            transitions,
            graph,
            coerced,
            after_s=after_s,
            cadence_s=diagnostics["cadence_s"],
            noise_floors=self._noise_floors(coerced),
            diagnostics=diagnostics,
        )
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

        # ---- measurement system ------------------------------------------
        matrix = build_measurement_matrix(
            graph, device_ids, noise_floor_w=self.noise_floor_w
        )
        system = self._solve_system(
            matrix,
            graph,
            coerced,
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

        # Publish the honest range of each inseparable group on its member
        # results.  This attaches min_w/max_w/group_members only — no member's
        # power_w is touched, so the unknown/slack stays unattributed.
        ambiguity = diagnostics["ambiguity"]
        if ambiguity["ambiguous"]:
            by_id = {r.device_id: r for r in results}
            for members in ambiguity["groups"]:
                min_w, max_w = self._group_bounds(
                    members,
                    matrix=matrix,
                    window=coerced,
                    models=models_by_device,
                    states=all_states,
                    configs=configs,
                    active_rows=system.get("active_rows", []),
                )
                for did in members:
                    member_result = by_id.get(did)
                    if member_result is None:
                        continue
                    member_result.group_members = sorted(members)
                    member_result.min_w = min_w
                    member_result.max_w = max_w

        self._bound_configured_priors_by_row(graph, coerced, results, diagnostics)
        reconciliation = self._reconcile(
            graph, coerced, results, matrix, diagnostics
        )
        diagnostics["accounted_devices"] = [
            r.device_id for r in results if r.source != Source.UNKNOWN
        ]
        diagnostics["unknown_devices"] = [
            r.device_id for r in results if r.source == Source.UNKNOWN
        ]

        # ---- joint-inference source ledger (additive; no power changes) ----
        # Computed once from the structures above, then attached to every result
        # so each value names the sources that produced it and the sources that
        # were available but unused, each with a code-derived reason.
        sources_used, sources_unused = _source_ledger(
            graph=graph,
            window=coerced,
            matrix=matrix,
            models=models_by_device,
            results=results,
            diagnostics=diagnostics,
            state_pinned=frozenset(system.get("state_pinned") or ()),
            residual_events=residual_events,
        )
        diagnostics["sources_used"] = sources_used
        diagnostics["sources_unused"] = sources_unused
        for result in results:
            result.sources_used = list(sources_used)
            result.sources_unused = list(sources_unused)

        return InferenceBatch(
            results=results,
            reconciliation=reconciliation,
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------------ #
    # Delegating shims -- the logic now lives in cohesive sibling modules.
    # These keep ``DisaggregationEngine._x`` patch points and direct callers
    # working unchanged; ``infer`` above calls them exactly as before.
    # ------------------------------------------------------------------ #
    def _noise_floors(self, window: CoercedWindow) -> dict[str, float]:
        """Per-channel noise floor: the measured MAD where one exists.

        ``noise_floor_w`` on the constructor is a single scalar, but a house has
        a 0.3 W resolution inverter CT next to a 30 W-resolution load manager.
        One scalar is either far too small for the noisy channel (every tick
        becomes an event) or far too large for the quiet one (a real 40 W step
        is invisible).  The quality layer already measures the innovation MAD
        per channel (``SampleAligner.channel_health().noise_mad_w``); the facade
        publishes it as ``metadata["channel_noise_floor_w"]``.

        The scalar stays the *floor* of every entry, not a default that a
        mapping replaces: a measured MAD below the configured floor would
        otherwise lower the threshold below what the caller asked for.  A
        channel with no measurement keeps the scalar, which is the historical
        behaviour, so a capture with no health data behaves exactly as before.
        """
        floors: dict[str, float] = {
            cid: self.noise_floor_w for cid in window.readings
        }
        raw = window.metadata.get("channel_noise_floor_w")
        if not isinstance(raw, Mapping):
            return floors
        for cid, value in raw.items():
            try:
                import math

                measured = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(measured) or measured < 0.0:
                continue
            floors[str(cid)] = max(self.noise_floor_w, measured)
        return floors

    def _anchor_steps(
        self,
        steps: Mapping[str, StepChange],
        transitions: Mapping[str, _Transition],
        graph: TopologyGraph,
        window: CoercedWindow,
        *,
        after_s: float,
        cadence_s: float,
        noise_floors: Mapping[str, float] | None = None,
        diagnostics: dict[str, Any],
    ) -> dict[str, StepChange]:
        return _anchor_steps(
            steps,
            transitions,
            graph,
            window,
            after_s=after_s,
            cadence_s=cadence_s,
            k_mad=self.k_mad,
            min_delta_w=self.min_delta_w,
            noise_floor_w=self.noise_floor_w,
            noise_floors=noise_floors,
            diagnostics=diagnostics,
        )

    def _exclude_partial_legs(
        self,
        matrix: MeasurementMatrix,
        graph: TopologyGraph,
        window: CoercedWindow,
        mask: np.ndarray,
        excluded: dict[str, str],
    ) -> None:
        return _exclude_partial_legs(
            matrix,
            graph,
            window,
            mask,
            excluded,
            max_measurement_age_s=self.max_measurement_age_s,
            max_leg_skew_s=self.max_leg_skew_s,
            declared_leg_partner=_declared_leg_partner,
        )

    @staticmethod
    def _declared_leg_partner(graph: TopologyGraph, ct_id: str) -> str | None:
        return _declared_leg_partner(graph, ct_id)

    def _solve_system(
        self,
        matrix: MeasurementMatrix,
        graph: TopologyGraph,
        window: CoercedWindow,
        device_ids: list[str],
        models: Mapping[str, ModelView | None],
        states: Mapping[str, str | None],
        diagnostics: dict[str, Any],
        state_overrides: Mapping[str, Mapping[str, str]] | None = None,
        configs: Any = None,
    ) -> dict[str, Any]:
        return _solve_system(
            matrix,
            graph,
            window,
            device_ids,
            models,
            states,
            diagnostics,
            state_overrides,
            configs,
            max_measurement_age_s=self.max_measurement_age_s,
            max_leg_skew_s=self.max_leg_skew_s,
            model_ridge=self.model_ridge,
        )

    def _detect_ambiguity(
        self,
        transitions: Mapping[str, _Transition],
        graph: TopologyGraph,
        ident: Identifiability,
        diagnostics: dict[str, Any],
    ) -> set[str]:
        return _detect_ambiguity(transitions, graph, ident, diagnostics)

    @staticmethod
    def _group_bounds(
        members: Sequence[str],
        *,
        matrix: MeasurementMatrix,
        window: CoercedWindow,
        models: Mapping[str, ModelView | None],
        states: Mapping[str, str | None],
        configs: Any,
        active_rows: Sequence[str],
    ) -> tuple[float | None, float | None]:
        return _group_bounds(
            members,
            matrix=matrix,
            window=window,
            models=models,
            states=states,
            configs=configs,
            active_rows=active_rows,
        )

    def _infer_device(
        self,
        device_id: str,
        **kwargs: Any,
    ) -> InferenceResult:
        return _infer_device(
            device_id,
            max_measurement_age_s=self.max_measurement_age_s,
            off_tolerance_w=self.off_tolerance_w,
            noise_floor_w=self.noise_floor_w,
            **kwargs,
        )

    def _bound_configured_priors_by_row(
        self,
        graph: TopologyGraph,
        window: CoercedWindow,
        results: Sequence[InferenceResult],
        diagnostics: dict[str, Any],
    ) -> None:
        return _bound_configured_priors_by_row(
            graph,
            window,
            results,
            diagnostics,
            max_measurement_age_s=self.max_measurement_age_s,
        )

    def _reconcile(
        self,
        graph: TopologyGraph,
        window: CoercedWindow,
        results: Sequence[InferenceResult],
        matrix: MeasurementMatrix,
        diagnostics: dict[str, Any],
    ) -> Reconciliation:
        return _reconcile(
            graph,
            window,
            results,
            matrix,
            diagnostics,
            max_measurement_age_s=self.max_measurement_age_s,
            phase_split_l1=self.phase_split_l1,
        )

    def _support_sigma(self, graph: TopologyGraph, channels: Sequence[str]) -> float:
        return _support_sigma(graph, channels, noise_floor_w=self.noise_floor_w)

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
        return _unknown_result(
            device_id,
            at,
            confidence,
            quality,
            supporting,
            explanation,
            provenance=provenance,
        )

    def _fallback_result(self, device_id: str, at: datetime, exc: Exception) -> InferenceResult:
        return _fallback_result(device_id, at, exc)

    @staticmethod
    def _source_ledger(
        *,
        graph: TopologyGraph,
        window: CoercedWindow,
        matrix: MeasurementMatrix,
        models: Mapping[str, ModelView | None],
        results: Sequence[InferenceResult],
        diagnostics: Mapping[str, Any],
        state_pinned: frozenset[str] | set[str] = frozenset(),
        residual_events: Sequence[Any] | None = None,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        return _source_ledger(
            graph=graph,
            window=window,
            matrix=matrix,
            models=models,
            results=results,
            diagnostics=diagnostics,
            state_pinned=state_pinned,
            residual_events=residual_events,
        )
