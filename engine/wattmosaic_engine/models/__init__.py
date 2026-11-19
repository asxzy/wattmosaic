"""Versioned power models (contract 3.0).

The live prediction path is the v3 codec (:mod:`spec`) plus registry
(:mod:`registry`) over the discriminated ``PowerModelSpec`` union.  Concrete
classes (:mod:`fixed`, :mod:`adjustable`, :mod:`dynamic`, :mod:`kernel`,
:mod:`components`) fit and predict those specs.  Declared device profiles adapt
user configuration onto the same prediction surface.

A few archived-row read helpers remain in :mod:`serde` / :mod:`view` for the
one-release read-only legacy tables; they never produce validated live power.
"""

from .adjustable import AdjustableModel, fit_adjustable_spec
from .base import (
    OFF_STATES_DEFAULT,
    ON_STATES_DEFAULT,
    enforce_monotonic,
    error_metrics,
    finite,
    median,
    robust_sigma,
)
from .components import ComponentSumModel, component_ids_from_links, predict_component_sum
from .device_profile import AdjustableResponse, DevicePowerProfile
from .dynamic import DynamicModel, fit_dynamic_spec
from .fixed import (
    FixedPowerModel,
    fit_fixed_spec,
    model_class_for_load_shape,
    suggest_model_class,
)
from .kernel import KernelModel, fit_kernel_from_windows
from .learn import learn_power_model
from .prediction import expected_window_for_spec, predict_with_spec, transition_prior_for_spec
from .protocols import ModelContextProtocol, SimpleReading, SimpleWindow, window_channels
from .registry import DatabaseModelRepository, ModelEstimate, ModelRegistry, Registry
from .residual_power import (
    IncompatibleBasisError,
    ResidualAnalyzer,
    ResidualAttempt,
    ResidualDecomposition,
    ResidualDecompositionConflict,
    ResidualNotComputed,
    ResidualScope,
    ResidualTrend,
    bases_are_compatible,
)
from .residual_power import persist as persist_residual
from .segmentation import Segment, SegmentationResult, segment_power_series
from .serde import LegacyModelRow, LegacyParameters
from .spec import decode_spec, encode_spec
from .view import ModelView, model_upper_bounds, resolve_model

__all__ = [
    "AdjustableModel",
    "AdjustableResponse",
    "ComponentSumModel",
    "DatabaseModelRepository",
    "DevicePowerProfile",
    "DynamicModel",
    "FixedPowerModel",
    "IncompatibleBasisError",
    "KernelModel",
    "LegacyModelRow",
    "LegacyParameters",
    "ModelContextProtocol",
    "ModelEstimate",
    "ModelRegistry",
    "ModelView",
    "OFF_STATES_DEFAULT",
    "ON_STATES_DEFAULT",
    "Registry",
    "ResidualAnalyzer",
    "ResidualAttempt",
    "ResidualDecomposition",
    "ResidualDecompositionConflict",
    "ResidualNotComputed",
    "ResidualScope",
    "ResidualTrend",
    "Segment",
    "SegmentationResult",
    "SimpleReading",
    "SimpleWindow",
    "bases_are_compatible",
    "component_ids_from_links",
    "decode_spec",
    "encode_spec",
    "enforce_monotonic",
    "error_metrics",
    "expected_window_for_spec",
    "fit_adjustable_spec",
    "fit_dynamic_spec",
    "fit_fixed_spec",
    "fit_kernel_from_windows",
    "finite",
    "learn_power_model",
    "median",
    "model_class_for_load_shape",
    "model_upper_bounds",
    "persist_residual",
    "predict_component_sum",
    "predict_with_spec",
    "resolve_model",
    "robust_sigma",
    "segment_power_series",
    "suggest_model_class",
    "transition_prior_for_spec",
    "window_channels",
]
