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
from .segmentation import Segment, SegmentationResult, segment_power_series
from .time_varying import CycleSummary, TimeVaryingApplianceModel, trapezoidal_energy_kwh
from .variable_speed import VariableSpeedDeviceModel

__all__ = [
    "BinaryDeviceModel",
    "CompositeDeviceModel",
    "CycleSummary",
    "DeviceModel",
    "DimmableDeviceModel",
    "ModelContextProtocol",
    "ModelEstimate",
    "ModelRegistry",
    "MultiStateDeviceModel",
    "ResidualDeviceModel",
    "Segment",
    "SegmentationResult",
    "SimpleReading",
    "SimpleWindow",
    "TimeVaryingApplianceModel",
    "VariableSpeedDeviceModel",
    "build_model",
    "context_from_sample",
    "enforce_monotonic",
    "error_metrics",
    "model_class_for",
    "segment_power_series",
    "trapezoidal_energy_kwh",
    "window_channels",
]
