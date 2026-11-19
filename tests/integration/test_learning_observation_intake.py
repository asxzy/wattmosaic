"""L4 — observation intake persists through S18 idempotently and reports reasons."""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    EvidenceRef,
    IdentifiabilityStatus,
    LearningObservation,
    ModelBasis,
)
from wattmosaic_engine.contracts.measurement import EventGroupStatus
from wattmosaic_engine.learning.pipeline import LearningPipeline
from wattmosaic_engine.learning.sample_filter import GateRejection
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _obs(oid: str, subject: str, **overrides: object) -> LearningObservation:
    data = {
        "observation_id": oid,
        "subject_kind": "device",
        "subject_id": subject,
        "endpoint_id": "ct-row",
        "event_group_ref": EvidenceRef(kind="event_group", id="grp-1"),
        "transition": "on",
        "attributes": {},
        "delta_w": 50.0,
        "level_w": None,
        "status": "confirmed",
        "rejection_reason": None,
        "identifiability": IdentifiabilityStatus.IDENTIFIABLE,
        "basis": ModelBasis.DELTA,
        "created_at": T0,
    }
    data.update(overrides)
    return LearningObservation.model_validate(data)


def test_intake_persists_idempotently_and_accounts_reasons() -> None:
    db = Database(":memory:")
    try:
        pipeline = LearningPipeline(db)
        obs = _obs("obs-1", "dev-a")
        accepted, held = pipeline.intake_observations(
            [obs], edges_confirmed_for=lambda _subject: True
        )
        assert len(accepted) == 1 and held == []
        assert db.repos.learning.observations("dev-a")[0]["status"] == "confirmed"
        # Replay: the id is deterministic and S18 put is idempotent.
        pipeline.intake_observations([obs], edges_confirmed_for=lambda _subject: True)
        assert len(db.repos.learning.observations("dev-a")) == 1
        assert pipeline.accounting.snapshot()["samples_seen"] == 2
    finally:
        db.close()


def test_intake_keeps_rejections_with_reasons() -> None:
    db = Database(":memory:")
    try:
        pipeline = LearningPipeline(db)
        obs = _obs("obs-2", "dev-b", identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE)
        accepted, held = pipeline.intake_observations(
            [obs], edges_confirmed_for=lambda _subject: True
        )
        assert accepted == []
        assert len(held) == 1
        assert held[0].rejection_reason == GateRejection.NON_IDENTIFIABLE.value
        reasons = pipeline.accounting.reasons()
        assert reasons["non_identifiable"] == 1
    finally:
        db.close()


def test_intake_uses_suspected_relationship_to_hold_provisional() -> None:
    db = Database(":memory:")
    try:
        pipeline = LearningPipeline(db)
        obs = _obs("obs-3", "dev-c")
        accepted, held = pipeline.intake_observations(
            [obs],
            group_status=EventGroupStatus.CONFIRMED,
            relationship_suspected_by_subject={"dev-c": True},
            edges_confirmed_for=lambda _subject: True,
        )
        assert accepted == []
        assert held[0].status == "provisional"
        assert held[0].rejection_reason == GateRejection.RELATIONSHIP_SUSPECTED_CHANGE.value
    finally:
        db.close()
