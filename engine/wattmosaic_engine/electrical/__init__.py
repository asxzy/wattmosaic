"""Electrical topology, measurement matrix and constrained solving.

Public surface (see ``docs/interface_contracts.md`` §3.3)::

    ElectricalModel(topology).build_matrix(device_ids) -> MeasurementMatrix
    ElectricalModel(topology).constraints()            -> list[LinearConstraint]
    ElectricalModel(topology).main_power_w(readings)   -> MainPower
    ElectricalModel(topology).validate()               -> list[str]
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from wattmosaic_engine.contracts import Topology
from wattmosaic_engine.electrical.direct import (
    DEFAULT_MAX_LEG_SKEW_S,
    DirectReading,
    direct_power_w,
    resolve_direct,
)
from wattmosaic_engine.electrical.factors import (
    BasisSample,
    FactorOwnership,
    FactorRegistry,
    FactorResolution,
    classify_basis,
    resolve_factor_ownership,
    scale_reading,
)
from wattmosaic_engine.electrical.hierarchy import (
    HierarchyConfig,
    HierarchyFinder,
    ValidationResult,
    explain,
    persist,
)
from wattmosaic_engine.electrical.main_power import MainPower, main_power_w, phase_power_w
from wattmosaic_engine.electrical.matrix import (
    MeasurementMatrix,
    PreparedSystem,
    build_measurement_matrix,
)
from wattmosaic_engine.electrical.models import (
    ABSOLUTE_DEVICE_CAP_W,
    ModelView,
    expected_power_w,
    model_upper_bounds,
    resolve_model,
)
from wattmosaic_engine.electrical.solver import (
    Identifiability,
    SolveResult,
    analyse_identifiability,
    residual_summary,
    solve_constrained_nnls,
)
from wattmosaic_engine.electrical.topology import Discipline, TopologyGraph, build_graph
from wattmosaic_engine.electrical.window import (
    DEFAULT_CADENCE_S,
    ChannelReading,
    ObservationWindow,
    coerce_window,
    observed_cadence_s,
)


@dataclass(frozen=True, slots=True)
class LinearConstraint:
    """``lower <= a . x <= upper`` over the device power vector."""

    name: str
    coefficients: dict[str, float]
    lower: float = 0.0
    upper: float = float("inf")
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "coefficients": dict(self.coefficients),
            "lower": self.lower,
            "upper": self.upper,
            "reason": self.reason,
        }


@dataclass(slots=True)
class ElectricalModel:
    """Facade implementing the authoritative §3.3 interface."""

    topology: Topology
    phase_split_l1: float = 0.5
    graph: TopologyGraph = field(init=False)

    def __post_init__(self) -> None:
        self.graph = build_graph(self.topology, phase_split_l1=self.phase_split_l1)

    # -- §3.3 ---------------------------------------------------------------
    def build_matrix(self, device_ids: list[str] | None = None) -> MeasurementMatrix:
        return build_measurement_matrix(self.graph, device_ids)

    def constraints(self) -> list[LinearConstraint]:
        out: list[LinearConstraint] = []
        for did in self.graph.device_ids:
            out.append(
                LinearConstraint(
                    name=f"non_negative:{did}",
                    coefficients={did: 1.0},
                    lower=0.0,
                    reason="device consumption cannot be negative in the fit",
                )
            )
            dev = self.graph.device(did)
            if dev and dev.double_pole:
                out.append(
                    LinearConstraint(
                        name=f"double_pole_total:{did}",
                        coefficients={did: 1.0},
                        lower=0.0,
                        reason="double-pole legs are one logical load, not two",
                    )
                )
        return out

    def main_power_w(self, readings: Mapping[str, object]) -> MainPower:
        return main_power_w(self.graph, readings)

    def validate(self) -> list[str]:
        return self.graph.validate()

    # -- convenience ---------------------------------------------------------
    def resolve_direct(
        self,
        window: object,
        *,
        max_age_s: float = 120.0,
        max_leg_skew_s: float = DEFAULT_MAX_LEG_SKEW_S,
    ) -> dict[str, DirectReading]:
        return resolve_direct(
            window,
            self.graph,
            max_age_s=max_age_s,
            max_leg_skew_s=max_leg_skew_s,
        )

    def solve(
        self,
        prepared: PreparedSystem,
        *,
        upper: Sequence[float | None] | None = None,
        prior: Sequence[float] | None = None,
        ridge: float = 0.0,
    ) -> SolveResult:
        A, b, _ = prepared.reduced()
        active_ids = [
            prepared.matrix.col_device_ids[j] for j in range(len(prepared.matrix.col_device_ids))
        ]
        upper_arr = None
        if upper is not None:
            upper_arr = np.array([np.inf if u is None else u for u in upper], dtype=float)
        return solve_constrained_nnls(
            A,
            b,
            device_ids=active_ids,
            lower=0.0,
            upper=upper_arr,
            prior=prior,
            ridge=ridge,
        )


__all__ = [
    "ABSOLUTE_DEVICE_CAP_W",
    "DEFAULT_CADENCE_S",
    "DEFAULT_MAX_LEG_SKEW_S",
    "BasisSample",
    "ChannelReading",
    "Discipline",
    "DirectReading",
    "ElectricalModel",
    "FactorOwnership",
    "FactorRegistry",
    "FactorResolution",
    "HierarchyConfig",
    "HierarchyFinder",
    "Identifiability",
    "LinearConstraint",
    "MainPower",
    "MeasurementMatrix",
    "ModelView",
    "ObservationWindow",
    "PreparedSystem",
    "SolveResult",
    "TopologyGraph",
    "ValidationResult",
    "analyse_identifiability",
    "build_graph",
    "build_measurement_matrix",
    "classify_basis",
    "coerce_window",
    "direct_power_w",
    "expected_power_w",
    "explain",
    "main_power_w",
    "model_upper_bounds",
    "observed_cadence_s",
    "persist",
    "phase_power_w",
    "residual_summary",
    "resolve_direct",
    "resolve_factor_ownership",
    "resolve_model",
    "scale_reading",
    "solve_constrained_nnls",
]
