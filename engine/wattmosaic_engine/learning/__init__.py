"""Automatic learning pipeline (sample → train → validate → promote)."""

from .observations import (
    build_group_observations,
    build_observations,
    observation_for_estimate,
)
from .pipeline import LearningPipeline
from .promotion import PromotionDecision, PromotionManager
from .reporting import (
    DYNAMIC_TOPOLOGY_NOTE,
    DYNAMIC_TOPOLOGY_QUALITIES,
    LearningAccounting,
    build_learning_status,
)
from .sample_collector import SampleCollector, extract_control
from .sample_filter import (
    GateDecision,
    GateInputs,
    GateRejection,
    SampleDecision,
    SampleEvidence,
    SampleFilter,
    evaluate_gate,
    rejection_summary,
)
from .stages import current_stage, recognize_stages, segment_cycle, stage_distinguishable
from .thresholds import LearningThresholds
from .trainer import Trainer, TrainingResult, infer_model_type, training_quality
from .validation import ValidationReport, compare_models, split_holdout, validate_model

__all__ = [
    "DYNAMIC_TOPOLOGY_NOTE",
    "DYNAMIC_TOPOLOGY_QUALITIES",
    "GateDecision",
    "GateInputs",
    "GateRejection",
    "LearningAccounting",
    "LearningPipeline",
    "LearningThresholds",
    "PromotionDecision",
    "PromotionManager",
    "SampleCollector",
    "SampleDecision",
    "SampleEvidence",
    "SampleFilter",
    "Trainer",
    "TrainingResult",
    "ValidationReport",
    "build_group_observations",
    "build_learning_status",
    "build_observations",
    "compare_models",
    "current_stage",
    "evaluate_gate",
    "extract_control",
    "infer_model_type",
    "observation_for_estimate",
    "recognize_stages",
    "rejection_summary",
    "segment_cycle",
    "split_holdout",
    "stage_distinguishable",
    "training_quality",
    "validate_model",
]
