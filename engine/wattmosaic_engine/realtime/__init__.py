"""Real-time ingestion primitives for contract 3.0 (P1).

The normalizer turns raw HA states into canonical :class:`ReaderMeasurement`
records (polarity and multiplier applied exactly once) and device-free
:class:`HAEvent` records.  The candidate ring holds events of unlinked entities
in memory only (DEC-17).
"""

from __future__ import annotations

from ..residual.unknown_profiles import (
    UnknownDetectionResult,
    UnknownProfileView,
    run_unknown_detection,
)
from .candidate_ring import CandidateRing
from .harness import EngineHarness
from .normalize import (
    QuarantineCounter,
    QuarantineReason,
    ReadingNormalizer,
    compatible,
    normalize_reading,
)

__all__ = [
    "CandidateRing",
    "EngineHarness",
    "QuarantineCounter",
    "QuarantineReason",
    "ReadingNormalizer",
    "UnknownDetectionResult",
    "UnknownProfileView",
    "compatible",
    "normalize_reading",
    "run_unknown_detection",
]
