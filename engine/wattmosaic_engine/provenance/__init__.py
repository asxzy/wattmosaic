"""Provenance resolution for contract 3.0 (D4, HB §18).

The package answers the nine HB §18 questions for any published estimate and
resolves typed :class:`~wattmosaic_engine.contracts.EvidenceRef` values against a
snapshot (and, optionally, an external evidence store).  It is pure: no database
connection is opened here and no wall clock is read.
"""

from __future__ import annotations

from .resolver import (
    QUESTION_KEYS,
    EvidenceLookup,
    ProvenanceAnswer,
    ProvenanceResolver,
    SnapshotEvidenceLookup,
    answer_for,
)

__all__ = [
    "QUESTION_KEYS",
    "EvidenceLookup",
    "ProvenanceAnswer",
    "ProvenanceResolver",
    "SnapshotEvidenceLookup",
    "answer_for",
]
