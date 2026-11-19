"""Versioned power models (contract 3.0).

The live prediction path is the v3 codec (:mod:`spec`) plus registry
(:mod:`registry`) over the discriminated ``PowerModelSpec`` union.  Concrete
classes (:mod:`fixed`, :mod:`adjustable`, :mod:`dynamic`, :mod:`kernel`,
:mod:`components`) fit and predict those specs.  Declared device profiles
(:mod:`device_profile`) adapt user configuration onto the same prediction
surface.
"""

from .adjustable import AdjustableModel, fit_adjustable_spec
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
from .numeric import (
    OFF_STATES_DEFAULT,
    ON_STATES_DEFAULT,
    enforce_monotonic,
    error_metrics,
    finite,
    median,
    robust_sigma,
)
from .prediction import expected_window_for_spec, predict_with_spec, transition_prior_for_spec
from .protocols import ModelContextProtocol, SimpleReading, SimpleWindow, window_channels
from .registry import DatabaseModelRepository, Registry
from .segmentation import Segment, SegmentationResult, segment_power_series
from .spec import decode_spec, encode_spec

__all__ = [
    "AdjustableModel",
    "AdjustableResponse",
    "ComponentSumModel",
    "DatabaseModelRepository",
    "DevicePowerProfile",
    "DynamicModel",
    "FixedPowerModel",
    "KernelModel",
    "ModelContextProtocol",
    "OFF_STATES_DEFAULT",
    "ON_STATES_DEFAULT",
    "Registry",
    "Segment",
    "SegmentationResult",
    "SimpleReading",
    "SimpleWindow",
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
    "predict_component_sum",
    "predict_with_spec",
    "robust_sigma",
    "segment_power_series",
    "suggest_model_class",
    "transition_prior_for_spec",
    "window_channels",
]
