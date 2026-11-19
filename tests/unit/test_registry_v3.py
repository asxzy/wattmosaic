"""L1: v3 model registry, append-only lifecycle and ModelSetView (DEF-07).

The tests use a small deterministic in-memory repository defined here (not a
runtime.fakes double — those are for cross-area handoffs).  They prove:

* exactly one active version per subject, including across a class change;
* rollback never reactivates a REJECTED or never-active version;
* ``model_set_id`` changes iff the active set changes;
* an unknown level yields a ``PowerPrediction`` with ``mean_w is None``;
* freeze blocks promotion, not inference;
* every lifecycle operation appends (never rewrites) events.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    AdjustableSpec,
    EvidenceRef,
    FixedSpec,
    LevelStat,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PowerModelEvent,
    PowerModelVersion,
    PredictionStatus,
)
from wattmosaic_engine.models.registry import Registry, derive_active_version_id
from wattmosaic_engine.runtime.interfaces import ModelSetView, PredictionContext

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _at(offset: int) -> datetime:
    return T0 + timedelta(seconds=offset)


class InMemoryModelRepo:
    """An append-only model repository with a strict event log."""

    def __init__(self) -> None:
        self.version_rows: dict[str, dict[str, Any]] = {}
        self.event_rows: list[dict[str, Any]] = []
        self.frozen: set[str] = set()
        self._seq = 0

    def append_version(self, version: PowerModelVersion) -> None:
        key = version.version_id
        if key in self.version_rows:
            raise ValueError("versions are immutable")
        self.version_rows[key] = {
            "version_id": key,
            "subject_id": version.subject_id,
            "subject_kind": version.subject_kind,
            "number": version.number,
            "model_class": version.model_class.value
            if hasattr(version.model_class, "value")
            else version.model_class,
            "basis": version.basis.value if hasattr(version.basis, "value") else version.basis,
            "spec": version.spec,
            "schema_version": version.schema_version,
            "evidence_refs_json": [
                ref.model_dump(mode="json") for ref in version.evidence_refs
            ],
            "topology_version_at_training": version.topology_version_at_training,
            "trainer_version": version.trainer_version,
            "parent_version_id": version.parent_version_id,
            "created_at": version.created_at.isoformat(),
        }

    def append_event(self, event: PowerModelEvent) -> None:
        self._seq += 1
        self.event_rows.append(
            {
                "id": self._seq,
                "version_id": event.version_id,
                "event": event.event,
                "reason": event.reason,
                "at": event.at.isoformat(),
            }
        )

    def events(self, version_id: str) -> list[dict[str, Any]]:
        return [dict(e) for e in self.event_rows if e["version_id"] == version_id]

    def versions(self, subject_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.version_rows.values()
            if row["subject_id"] == subject_id
        ]

    def subject_ids(self) -> list[str]:
        return sorted({row["subject_id"] for row in self.version_rows.values()})

    def set_model_frozen(self, subject_id: str, frozen: bool) -> None:
        if frozen:
            self.frozen.add(subject_id)
        else:
            self.frozen.discard(subject_id)

    def is_model_frozen(self, subject_id: str) -> bool:
        return subject_id in self.frozen

    def active_version(self, subject_id: str) -> dict[str, Any] | None:
        # Derive from the event log: last activated wins, rejected/never-active
        # never eligible (DEF-07).
        by_version: dict[str, list[dict[str, Any]]] = {}
        for event in self.event_rows:
            vid = event["version_id"]
            row = self.version_rows.get(vid)
            if row and row["subject_id"] == subject_id:
                by_version.setdefault(vid, []).append(event)
        winner = derive_active_version_id(by_version)
        return dict(self.version_rows[winner]) if winner else None


def _version(
    subject: str = "dev-1",
    number: int = 1,
    *,
    model_class: ModelClass = ModelClass.FIXED,
    spec: Any | None = None,
) -> PowerModelVersion:
    if spec is None:
        if model_class == ModelClass.ADJUSTABLE:
            spec = AdjustableSpec(control_variable="brightness", knots_x=[1.0], knots_w=[10.0])
        else:
            spec = FixedSpec(levels={PowerLevelV3.ACTIVE: LevelStat(median_w=42.0, n=5)})
    return PowerModelVersion(
        version_id=f"mdl-{subject}-{number}",
        subject_kind="device",
        subject_id=subject,
        number=number,
        model_class=model_class,
        basis=ModelBasis.DELTA,
        spec=spec,
        evidence_refs=[EvidenceRef(kind="event_group", id=f"g{number}")],
        trainer_version="test",
        created_at=_at(number),
    )


def _registry(*, clock_offset: int = 0) -> tuple[Registry, InMemoryModelRepo]:
    repo = InMemoryModelRepo()
    now = [T0 + timedelta(seconds=clock_offset)]
    clock = lambda: now[0]  # noqa: E731
    return Registry(repo, clock), repo


# --------------------------------------------------------------------------- #
# ModelSetView contract.
# --------------------------------------------------------------------------- #


def test_registry_satisfies_model_set_view() -> None:
    registry, _ = _registry()
    assert isinstance(registry, ModelSetView)


def test_empty_active_set_has_stable_id() -> None:
    registry, _ = _registry()
    assert registry.active_set().model_set_id == "mset-" + __import__("hashlib").sha256(b"").hexdigest()[:12]


def test_unknown_level_is_none_never_zero() -> None:
    registry, repo = _registry()
    version = _version()
    registry.propose(version)
    registry.activate(version.version_id)
    ctx = PredictionContext(at=T0, level=PowerLevelV3.TRANSIENT)
    prediction = registry.predict("dev-1", ctx)
    assert prediction.status == PredictionStatus.UNKNOWN
    assert prediction.mean_w is None

    # Unknown subject is also honest.
    unknown = registry.predict("nope", ctx)
    assert unknown.status == PredictionStatus.UNKNOWN
    assert unknown.mean_w is None


# --------------------------------------------------------------------------- #
# Lifecycle: exactly one active per subject, append-only, DEF-07 rollback.
# --------------------------------------------------------------------------- #


def test_exactly_one_active_version_across_a_class_change() -> None:
    registry, repo = _registry()
    fixed = _version(number=1, model_class=ModelClass.FIXED)
    adjustable = _version(number=2, model_class=ModelClass.ADJUSTABLE)
    registry.propose(fixed)
    registry.propose(adjustable)
    registry.activate(fixed.version_id)
    registry.activate(adjustable.version_id)

    active = registry.active_version("dev-1")
    assert active is not None
    assert active.version_id == adjustable.version_id
    assert active.model_class == ModelClass.ADJUSTABLE
    assert len(registry.versions("dev-1")) == 2
    # Exactly one activation is the latest event; the old one was superseded.
    assert sum(
        1 for r in repo.event_rows if r["event"] == "superseded" and r["version_id"] == fixed.version_id
    ) == 1


def test_rollback_never_reactivates_rejected_or_never_active() -> None:
    # A monotonically advancing clock keeps every appended event ordered.
    now = [T0]

    def tick() -> datetime:
        now[0] += timedelta(seconds=10)
        return now[0]

    repo = InMemoryModelRepo()
    registry = Registry(repo, clock=tick)

    v1 = _version(number=1)
    rejected = _version(number=2)
    never = _version(number=3)
    for v in (v1, rejected, never):
        registry.propose(v)
    registry.activate(v1.version_id)
    registry.reject(rejected.version_id)

    # With v1 still active, rollback finds no *previously* activated version
    # to restore (v2 rejected, v3 never-active).
    assert registry.rollback("dev-1") is None
    assert repo.active_version("dev-1")["version_id"] == v1.version_id

    # Activating a second version supersedes v1; then reject it so no version
    # is active and only v1 has ever been activated.
    registry.activate(never.version_id)
    registry.reject(never.version_id)
    assert repo.active_version("dev-1") is None

    # Rollback reactivates the last version whose log shows ACTIVE (v1) — it
    # never picks the rejected v2 or the just-rejected v3 (DEF-07).
    target = registry.rollback("dev-1")
    assert target == v1.version_id
    assert target not in {rejected.version_id, never.version_id}
    assert repo.active_version("dev-1")["version_id"] == v1.version_id


def test_all_lifecycle_operations_append() -> None:
    registry, repo = _registry()
    v1 = _version(number=1)
    registry.propose(v1)
    registry.activate(v1.version_id)
    registry.freeze("dev-1", reason="review")
    registry.unfreeze("dev-1", reason="done")
    events = repo.event_rows
    assert [e["event"] for e in events] == [
        "candidate",
        "activated",
        "frozen",
        "unfrozen",
    ]
    # Ids are monotone; nothing was rewritten.
    assert [e["id"] for e in events] == list(range(1, len(events) + 1))


def test_freeze_blocks_promotion_not_inference() -> None:
    registry, repo = _registry()
    v1 = _version(number=1)
    registry.propose(v1)
    registry.activate(v1.version_id)
    before = registry.predict("dev-1", PredictionContext(at=T0, level=PowerLevelV3.ACTIVE))

    registry.freeze("dev-1")
    after = registry.predict("dev-1", PredictionContext(at=T0, level=PowerLevelV3.ACTIVE))
    assert after == before  # inference still works while frozen
    assert after.status == PredictionStatus.KNOWN

    v2 = _version(number=2)
    registry.propose(v2)
    with pytest.raises(ValueError, match="frozen"):
        registry.activate(v2.version_id)
    assert repo.active_version("dev-1")["version_id"] == v1.version_id


# --------------------------------------------------------------------------- #
# model_set_id changes iff the active set changes.
# --------------------------------------------------------------------------- #


def test_model_set_id_changes_iff_active_set_changes() -> None:
    registry, _ = _registry()
    assert registry.model_set_id.startswith("mset-")
    empty = registry.model_set_id

    v1 = _version("dev-1", 1)
    registry.propose(v1)
    registry.activate(v1.version_id)
    registry.refresh()
    one = registry.model_set_id
    assert one != empty

    # Reading predictions/active sets does not change the id.
    assert registry.active_set().model_set_id == one
    registry.predict("dev-1", PredictionContext(at=T0, level=PowerLevelV3.ACTIVE))
    assert registry.refresh() == one

    # Proposing (candidate only) does not change the active set.
    v2 = _version("dev-1", 2)
    registry.propose(v2)
    assert registry.refresh() == one

    # Activating a second subject's model changes it.
    v3 = _version("dev-2", 1)
    registry.propose(v3)
    registry.activate(v3.version_id)
    two = registry.refresh()
    assert two != one

    # Rolling back to the previous active set restores the earlier id.
    registry._repo.append_event(
        PowerModelEvent(
            version_id=v3.version_id,
            event="superseded",
            reason="test",
            at=T0 + timedelta(seconds=100),
        )
    )
    registry.refresh()
    assert registry.model_set_id == one


# --------------------------------------------------------------------------- #
# Single prediction path.
# --------------------------------------------------------------------------- #


def test_transition_prior_and_expected_window() -> None:
    from wattmosaic_engine.contracts import KernelPhase, ResponseKernelSpec

    fixed = FixedSpec(
        levels={PowerLevelV3.ACTIVE: LevelStat(median_w=60.0, n=5)},
        transition_delta={"on": LevelStat(median_w=56.0, mad_w=2.0, n=4)},
    )
    registry, _ = _registry()
    registry.propose(_version(spec=fixed))
    registry.activate("mdl-dev-1-1")
    prior = registry.transition_prior("dev-1", "on", {})
    assert prior is not None
    assert prior.mean_w == pytest.approx(56.0)
    assert registry.transition_prior("dev-1", "off", {}) is None
    assert registry.expected_window("dev-1", "on", {}, np.array([0.0])) is None

    kernel = ResponseKernelSpec(
        by_transition={
            "on": [
                KernelPhase(
                    phase="steady",
                    delay_s=LevelStat(median_w=0.0),
                    amplitude_w=LevelStat(median_w=60.0),
                    duration_s=LevelStat(median_w=100.0),
                )
            ]
        }
    )
    registry2, _ = _registry()
    registry2.propose(_version(spec=kernel))
    registry2.activate("mdl-dev-1-1")
    window = registry2.expected_window("dev-1", "on", {}, np.array([1.0, 5.0]))
    assert window is not None
    mean, sigma = window
    assert mean[0] == pytest.approx(60.0)
    assert sigma[0] == pytest.approx(0.0) or sigma[0] != sigma[0]  # absent or zero-from-stat


# --------------------------------------------------------------------------- #
# DEF-01 follow-up and HTTP surface.
# --------------------------------------------------------------------------- #


def test_polarity_defect_supersedes_with_reason() -> None:
    registry, repo = _registry()
    v1 = _version(number=1)
    registry.propose(v1)
    registry.activate(v1.version_id)
    assert registry.active_version("dev-1") is not None

    marked = registry.mark_polarity_defect("dev-1")
    assert marked == [v1.version_id]
    assert registry.active_version("dev-1") is None
    assert any(
        e["event"] == "superseded" and e["reason"] == "polarity_defect"
        for e in repo.event_rows
    )


def test_models_v3_http_endpoint(tmp_path: Any) -> None:
    """GET /models/{subject_id} returns the v3 versions + event log."""
    import asyncio

    from aiohttp import ClientSession, web
    from wattmosaic_engine.api.server import create_app
    from wattmosaic_engine.models.registry import DatabaseModelRepository
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_path / "models-v3.db")
    registry = DatabaseModelRepository(db)
    v3 = Registry(registry, clock=lambda: T0)
    version = PowerModelVersion(
        version_id="mdl-dev-1",
        subject_kind="device",
        subject_id="dev",
        number=1,
        model_class=ModelClass.FIXED,
        basis=ModelBasis.DELTA,
        spec=FixedSpec(levels={PowerLevelV3.ACTIVE: LevelStat(median_w=42.0, n=3)}),
        trainer_version="test",
        created_at=T0,
    )
    v3.propose(version)
    v3.activate(version.version_id)

    class Facade:
        def __init__(self) -> None:
            self._clock = lambda: T0
            self.model_registry = None
            self._model_registry_v3 = v3

        def health(self) -> dict[str, str]:
            return {"status": "ok"}

        def models_v3(self, subject_id: str) -> dict[str, Any]:
            return {
                "subject_id": subject_id,
                "versions": [m.model_dump(mode="json") for m in v3.versions(subject_id)],
                "events": v3.events(subject_id),
            }

        # The unified facade method on EngineCore delegates to this.
        def models(self, subject_id: str) -> dict[str, Any]:
            return self.models_v3(subject_id)

    async def run() -> None:
        app = create_app(Facade(), api_token="")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        async with (
            ClientSession() as session,
            session.get(f"http://127.0.0.1:{port}/api/v1/models/dev") as resp,
        ):
            assert resp.status == 200
            body = await resp.json()
            assert body["subject_id"] == "dev"
            assert len(body["versions"]) == 1
            assert [e["event"] for e in body["events"]] == ["candidate", "activated"]
        await runner.cleanup()

    try:
        asyncio.run(run())
    finally:
        db.close()
