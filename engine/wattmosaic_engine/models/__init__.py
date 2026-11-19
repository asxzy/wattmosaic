"""Device-model framework.

Concrete models all implement ``fit`` / ``predict`` / ``validate`` / ``describe``
and expose ``model_type``.  See ``docs/learning.md`` for the model-selection
guidance.
"""

from .adjustable import AdjustableModel, fit_adjustable_spec
from .base import (
    DeviceModel,
    build_model,
    context_from_sample,
    enforce_monotonic,
    error_metrics,
    model_class_for,
)
from .binary import BinaryDeviceModel
from .composite import CompositeDeviceModel
from .device_profile import AdjustableResponse, DevicePowerProfile
from .dimmable import DimmableDeviceModel
from .fixed import (
    FixedPowerModel,
    fit_fixed_spec,
    model_class_for_load_shape,
    suggest_model_class,
)
from .learn import learn_power_model
from .multi_state import MultiStateDeviceModel
from .protocols import ModelContextProtocol, SimpleReading, SimpleWindow, window_channels
from .registry import ModelEstimate, ModelRegistry
from .residual import ResidualDeviceModel
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
from .time_varying import CycleSummary, TimeVaryingApplianceModel, trapezoidal_energy_kwh
from .variable_speed import VariableSpeedDeviceModel

__all__ = [
    "AdjustableModel",
    "AdjustableResponse",
    "BinaryDeviceModel",
    "CompositeDeviceModel",
    "CycleSummary",
    "DeviceModel",
    "FixedPowerModel",
    "DevicePowerProfile",
    "DimmableDeviceModel",
    "IncompatibleBasisError",
    "ModelContextProtocol",
    "ModelEstimate",
    "ModelRegistry",
    "MultiStateDeviceModel",
    "ResidualAnalyzer",
    "ResidualAttempt",
    "ResidualDecomposition",
    "ResidualDecompositionConflict",
    "ResidualDeviceModel",
    "ResidualNotComputed",
    "ResidualScope",
    "ResidualTrend",
    "Segment",
    "SegmentationResult",
    "SimpleReading",
    "SimpleWindow",
    "TimeVaryingApplianceModel",
    "VariableSpeedDeviceModel",
    "bases_are_compatible",
    "build_model",
    "context_from_sample",
    "fit_adjustable_spec",
    "fit_fixed_spec",
    "learn_power_model",
    "enforce_monotonic",
    "error_metrics",
    "model_class_for",
    "model_class_for_load_shape",
    "persist_residual",
    "segment_power_series",
    "suggest_model_class",
    "trapezoidal_energy_kwh",
    "window_channels",
]
