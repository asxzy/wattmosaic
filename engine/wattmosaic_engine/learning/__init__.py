"""Automatic learning pipeline (observation → train → promote) — contract 3.0.

The realtime path is observation-based: :mod:`observations` turns immutable
event evidence into device-attributed :class:`LearningObservation` values,
:mod:`sample_filter` applies the quality gate, :mod:`incremental` and
:mod:`promotion` train and promote per model class, and :mod:`worker` runs that
work off the event loop.  :mod:`reconciliation` replays immutable evidence under
explicit model/topology versions.
"""

from .observations import (
    build_group_observations,
    build_observations,
    observation_for_estimate,
)
from .promotion import PromotionDecisionV2, evaluate_promotion_v2
from .reporting import (
    DYNAMIC_TOPOLOGY_NOTE,
    DYNAMIC_TOPOLOGY_QUALITIES,
    LearningAccounting,
    build_learning_status,
)
from .sample_filter import GateDecision, GateInputs, GateRejection, evaluate_gate
from .stages import current_stage, recognize_stages, segment_cycle, stage_distinguishable
from .thresholds import LearningThresholds
from .validation import (
    ClassCriteria,
    FrozenEvaluation,
    compare_frozen,
    evaluate_frozen,
    per_class_accepts,
    split_by_observation_id,
)

__all__ = [
    "DYNAMIC_TOPOLOGY_NOTE",
    "DYNAMIC_TOPOLOGY_QUALITIES",
    "ClassCriteria",
    "FrozenEvaluation",
    "GateDecision",
    "GateInputs",
    "GateRejection",
    "LearningAccounting",
    "LearningThresholds",
    "PromotionDecisionV2",
    "build_group_observations",
    "build_learning_status",
    "build_observations",
    "compare_frozen",
    "current_stage",
    "evaluate_frozen",
    "evaluate_gate",
    "evaluate_promotion_v2",
    "observation_for_estimate",
    "per_class_accepts",
    "recognize_stages",
    "segment_cycle",
    "split_by_observation_id",
    "stage_distinguishable",
]
