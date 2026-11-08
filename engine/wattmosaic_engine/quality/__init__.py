"""Data-quality evaluation.

See :mod:`wattmosaic_engine.quality.engine` for the scoring rules and the
configurable thresholds.  The public entry point is ``DataQualityEngine``.
"""

from .engine import (
    QUALITY_THRESHOLDS,
    DataQualityEngine,
    QualityThresholds,
    score_to_status,
)

__all__ = ["DataQualityEngine", "QualityThresholds", "QUALITY_THRESHOLDS", "score_to_status"]
