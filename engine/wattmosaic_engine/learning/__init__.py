"""Automatic learning pipeline (sample → train → validate → promote)."""

from .pipeline import LearningPipeline
from .promotion import PromotionDecision, PromotionManager
from .sample_collector import SampleCollector, extract_control
from .sample_filter import SampleDecision, SampleEvidence, SampleFilter, rejection_summary
from .stages import current_stage, recognize_stages, segment_cycle, stage_distinguishable
from .thresholds import LearningThresholds
from .trainer import Trainer, TrainingResult, infer_model_type, training_quality
from .validation import ValidationReport, compare_models, split_holdout, validate_model

__all__ = [
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
    "compare_models",
    "current_stage",
    "extract_control",
    "infer_model_type",
    "recognize_stages",
    "rejection_summary",
    "segment_cycle",
    "split_holdout",
    "stage_distinguishable",
    "training_quality",
    "validate_model",
]
