"""Device-model framework.

Concrete models all implement ``fit`` / ``predict`` / ``validate`` / ``describe``
and expose ``model_type``.  See ``docs/learning.md`` for the model-selection
guidance.
"""

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
from .dimmable import DimmableDeviceModel
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
    "BinaryDeviceModel",
    "CompositeDeviceModel",
    "CycleSummary",
    "DeviceModel",
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
    "enforce_monotonic",
    "error_metrics",
    "model_class_for",
    "persist_residual",
    "segment_power_series",
    "trapezoidal_energy_kwh",
    "window_channels",
]
