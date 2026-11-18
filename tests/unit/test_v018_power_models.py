"""Migration v018: power models v2, model events and learning observations (S18).

The fixture is a genuine pre-migration database built with raw ``sqlite3``:
migrations 1-17 plus legacy ``device_model_versions`` rows, with ``user_version``
left at 17.  ``Database.__init__`` migrates immediately, so it cannot produce a
pre-migration fixture; the repositories are reached through ``db.repos.models``
and ``db.repos.learning``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wattmosaic_engine.contracts import (
    AdjustableSpec,
    EvidenceRef,
    FixedSpec,
    IdentifiabilityStatus,
    LearningObservation,
    LevelStat,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PowerModelEvent,
    PowerModelVersion,
    observation_id,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    current_version,
)
from wattmosaic_engine.storage.migrations.v018_power_models import (
    MIGRATION as V018,
)
from wattmosaic_engine.storage.migrations.v018_power_models import (
    TIERS,
)
from wattmosaic_engine.storage.repositories.models import LegacySpec

PRE_MIGRATION_VERSION = 17
NOW = datetime(2026, 11, 1, tzinfo=UTC)
AT = lambda offset: NOW + timedelta(seconds=offset)  # noqa: E731


def _build_pre_migration_db(path: Path) -> sqlite3.Connection:
    """Apply migrations 1-17, seed legacy model rows, stamp version 17.

    The fixture works from the schema-11 base too: ``device_model_versions`` is
    created by a released migration and never dropped.  Legacy rows deliberately
    include two types on one device sharing version 1 (the S18 renumbering case).
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        for migration in MIGRATIONS:
            if migration.version > PRE_MIGRATION_VERSION:
                break
            for statement in migration.statements:
                conn.execute(statement)
            if migration.apply is not None:
                migration.apply(conn)
        conn.execute(f"PRAGMA user_version = {PRE_MIGRATION_VERSION}")
        seed = (
            "INSERT INTO device_model_versions(device_id, model_type, version, status, "
            "created_at, parameters_json) VALUES(?,?,?,?,?,?)"
        )
        conn.executemany(
            seed,
            [
                (
                    "dev-1",
                    "binary",
                    1,
                    "active",
                    "2026-10-01T00:00:00+00:00",
                    json.dumps({"on_power_w": 50.0, "off_power_w": 0.0}),
                ),
                (
                    "dev-1",
                    "dimmable",
                    1,
                    "candidate",
                    "2026-10-02T00:00:00+00:00",
                    json.dumps({"curve": [[1.0, 10.0], [2.0, 20.0]]}),
                ),
                (
                    "dev-2",
                    "multi_state",
                    3,
                    "active",
                    "2026-10-03T00:00:00+00:00",
                    json.dumps({"state_map": {"on": 80.0}}),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return conn


def _open(path: Path) -> Database:
    db = Database(path)
    assert db.schema_version == LATEST_VERSION
    return db


def _version(
    subject_id: str,
    number: int,
    model_class: ModelClass = ModelClass.FIXED,
    at: datetime = NOW,
) -> PowerModelVersion:
    spec = FixedSpec(levels={PowerLevelV3.ACTIVE: LevelStat(median_w=42.0, n=5)})
    if model_class == ModelClass.ADJUSTABLE:
        spec = AdjustableSpec(control_variable="brightness", knots_x=[1.0], knots_w=[10.0])
    return PowerModelVersion(
        version_id=f"mdl-{subject_id}-{number}",
        subject_kind="device",
        subject_id=subject_id,
        number=number,
        model_class=model_class,
        basis=ModelBasis.DELTA,
        spec=spec,
        trainer_version="test",
        created_at=at,
    )


def _event(version_id: str, event: str, at: datetime, reason: str = "test") -> PowerModelEvent:
    return PowerModelEvent(version_id=version_id, event=event, reason=reason, at=at)


def _observation(
    obs_id: str,
    subject_id: str,
    status: str = "confirmed",
    at: datetime = NOW,
) -> LearningObservation:
    return LearningObservation(
        observation_id=obs_id,
        subject_kind="device",
        subject_id=subject_id,
        endpoint_id="reader-1",
        event_group_ref=EvidenceRef(kind="event_group", id="group-1"),
        transition="on",
        delta_w=42.0,
        status=status,  # type: ignore[arg-type]
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        created_at=at,
    )


# --------------------------------------------------------------------------- #
# Migration contract, legacy data rules and idempotency / atomicity
# --------------------------------------------------------------------------- #


def test_module_declares_the_reserved_version_and_tiers() -> None:
    assert V018.version == 18
    assert TIERS == {
        "power_model_versions_v3": "L4",
        "power_model_events": "L4",
        "learning_observations": "L4",
    }


def test_legacy_rows_are_copied_flagged_and_none_is_active(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _build_pre_migration_db(path)

    db = _open(path)
    try:
        versions = db._conn.execute(
            "SELECT * FROM power_model_versions_v3 ORDER BY subject_id, number"
        ).fetchall()
        assert len(versions) == 3
        by_number = {(v["subject_id"], v["number"]): v for v in versions}

        # Every copy is LEGACY_ABSOLUTE with a tagged legacy spec; no copy is
        # activated, so none is served as validated power (DEC-31).
        for version in versions:
            assert version["basis"] == "LEGACY_ABSOLUTE"
            assert version["trainer_version"] == "legacy"
            spec = json.loads(version["spec_json"])
            assert spec["kind"] == "legacy"
            assert isinstance(spec["parameters"], dict)

        first = by_number[("dev-1", 1)]
        assert first["model_class"] == "FIXED"
        assert json.loads(first["spec_json"])["parameters"] == {
            "on_power_w": 50.0,
            "off_power_w": 0.0,
        }
        # Same legacy version number on one device is renumbered to the next
        # free number, so the unique (subject, number) invariant holds.
        second = by_number[("dev-1", 2)]
        assert second["model_class"] == "ADJUSTABLE"
        assert json.loads(second["spec_json"])["parameters"]["curve"] == [
            [1.0, 10.0],
            [2.0, 20.0],
        ]
        assert by_number[("dev-2", 3)]["model_class"] == "DYNAMIC"

        events = db._conn.execute(
            "SELECT * FROM power_model_events ORDER BY version_id"
        ).fetchall()
        assert len(events) == 3
        assert all(e["event"] == "superseded" for e in events)
        assert all(e["reason"] == "legacy_absolute" for e in events)

        # No legacy subject has an active version through the repository either.
        models = db.repos.models
        assert models.active_version("dev-1") is None
        assert models.active_version("dev-2") is None
        read = models.versions("dev-1")[0]
        assert isinstance(read["spec"], LegacySpec)
    finally:
        db.close()


def test_legacy_table_is_untouched(tmp_path: Path) -> None:
    path = tmp_path / "untouched.db"
    _build_pre_migration_db(path)
    before = sqlite3.connect(path)
    try:
        count = before.execute("SELECT COUNT(*) FROM device_model_versions").fetchone()[0]
    finally:
        before.close()
    db = _open(path)
    try:
        after = db._conn.execute("SELECT COUNT(*) FROM device_model_versions").fetchone()[0]
        assert after == count == 3
    finally:
        db.close()


def test_apply_callable_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.db"
    _build_pre_migration_db(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        assert V018.apply is not None
        V018.apply(conn)
        first_versions = conn.execute("SELECT * FROM power_model_versions_v3").fetchall()
        first_events = conn.execute("SELECT * FROM power_model_events").fetchall()
        V018.apply(conn)
        second_versions = conn.execute("SELECT * FROM power_model_versions_v3").fetchall()
        second_events = conn.execute("SELECT * FROM power_model_events").fetchall()
        assert [dict(r) for r in first_versions] == [dict(r) for r in second_versions]
        assert [dict(r) for r in first_events] == [dict(r) for r in second_events]
        assert len(second_versions) == 3
        assert len(second_events) == 3
    finally:
        conn.close()


def test_failure_mid_apply_leaves_user_version_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "failure.db"
    _build_pre_migration_db(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        conn.execute("BEGIN IMMEDIATE")
        try:
            assert V018.apply is not None
            V018.apply(conn)
            # Inject a failure after the schema/backfill but before COMMIT;
            # the migration framework's ROLLBACK restores user_version.
            raise RuntimeError("injected failure")
        except RuntimeError:
            conn.execute("ROLLBACK")
        assert current_version(conn) == PRE_MIGRATION_VERSION
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "power_model_versions_v3" not in tables
    finally:
        conn.close()

    db = _open(path)
    try:
        assert db.schema_version == LATEST_VERSION
    finally:
        db.close()


def test_migration_is_part_of_the_discovered_registry() -> None:
    assert any(m.version == V018.version and m.name == V018.name for m in MIGRATIONS)


# --------------------------------------------------------------------------- #
# Repository: append-only versions, event-derived active, observations
# --------------------------------------------------------------------------- #


def test_active_version_is_derived_from_the_event_log(tmp_path: Path) -> None:
    path = tmp_path / "active.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        models = db.repos.models
        v1 = _version("subject-1", 1, at=AT(0))
        v2 = _version("subject-1", 2, at=AT(10))
        models.append_version(v1)
        models.append_version(v2)

        # A candidate alone is never active.
        models.append_event(_event(v1.version_id, "candidate", AT(1)))
        assert models.active_version("subject-1") is None

        # Activation makes it active.
        models.append_event(_event(v1.version_id, "activated", AT(2)))
        assert models.active_version("subject-1")["version_id"] == v1.version_id

        # A later supersede of v1 plus activation of v2 moves the single active
        # version to v2 (a class change is handled the same way).
        models.append_event(_event(v1.version_id, "superseded", AT(11), "promoted"))
        models.append_event(_event(v2.version_id, "activated", AT(12)))
        active = models.active_version("subject-1")
        assert active["version_id"] == v2.version_id
    finally:
        db.close()


def test_rollback_and_rejected_versions_are_never_active(tmp_path: Path) -> None:
    """DEF-07: a rejected or never-activated version is never reactivated."""
    path = tmp_path / "rollback.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        models = db.repos.models
        v1 = _version("subject-1", 1, at=AT(0))
        rejected = _version("subject-1", 2, at=AT(10))
        never = _version("subject-1", 3, at=AT(20))
        for version in (v1, rejected, never):
            models.append_version(version)

        models.append_event(_event(v1.version_id, "activated", AT(1)))
        models.append_event(_event(rejected.version_id, "candidate", AT(11)))
        models.append_event(_event(rejected.version_id, "rejected", AT(12)))
        models.append_event(_event(never.version_id, "candidate", AT(21)))
        # Supersede v1, then roll back its replacement: only a never-activated
        # and a rejected candidate remain, so there must be no active version.
        models.append_event(_event(v1.version_id, "superseded", AT(30), "rolled_forward"))
        assert models.active_version("subject-1") is None

        # Explicit rollback of the last active version never resurrects a
        # rejected/never-activated version.
        models.append_event(_event(never.version_id, "rolled_back", AT(31)))
        assert models.active_version("subject-1") is None

        # Reactivating the previously activated v1 is the only eligible target.
        models.append_event(_event(v1.version_id, "activated", AT(40), "rollback"))
        assert models.active_version("subject-1")["version_id"] == v1.version_id
        rolled_back = models.append_event(_event(v1.version_id, "rolled_back", AT(50)))
        assert rolled_back is None
        assert models.active_version("subject-1") is None
    finally:
        db.close()


def test_exactly_one_active_version_across_a_class_change(tmp_path: Path) -> None:
    path = tmp_path / "class_change.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        models = db.repos.models
        fixed = _version("subject-1", 1, ModelClass.FIXED, at=AT(0))
        adjustable = _version("subject-1", 2, ModelClass.ADJUSTABLE, at=AT(10))
        models.append_version(fixed)
        models.append_version(adjustable)
        models.append_event(_event(fixed.version_id, "activated", AT(1)))
        models.append_event(_event(adjustable.version_id, "activated", AT(11)))
        models.append_event(_event(fixed.version_id, "superseded", AT(12)))

        active = models.active_version("subject-1")
        assert active["version_id"] == adjustable.version_id
        assert active["model_class"] == "ADJUSTABLE"
        all_versions = models.versions("subject-1")
        assert len(all_versions) == 2
        assert {v["model_class"] for v in all_versions} == {"FIXED", "ADJUSTABLE"}
    finally:
        db.close()


def test_append_version_is_immutable(tmp_path: Path) -> None:
    path = tmp_path / "immutable.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        version = _version("subject-1", 1)
        db.repos.models.append_version(version)
        with pytest.raises(sqlite3.IntegrityError):
            db.repos.models.append_version(version)
    finally:
        db.close()


def test_observation_id_is_deterministic() -> None:
    a = observation_id("group-1", 1, "dev-1", "reader-1", "topo-1")
    b = observation_id("group-1", 1, "dev-1", "reader-1", "topo-1")
    c = observation_id("group-1", 2, "dev-1", "reader-1", "topo-1")
    assert a == b
    assert a != c


def test_observation_put_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "observations.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        learning = db.repos.learning
        obs = _observation("obs-1", "dev-1", at=AT(0))
        assert learning.put_observation(obs) is True
        assert learning.put_observation(obs) is False
        rows = learning.observations("dev-1")
        assert len(rows) == 1
        assert rows[0]["observation_id"] == "obs-1"
        assert rows[0]["event_group_ref"] == {"kind": "event_group", "id": "group-1"}
        assert rows[0]["delta_w"] == 42.0

        # A different id, status or time filter selects only matching rows.
        learning.put_observation(_observation("obs-2", "dev-1", "provisional", AT(5)))
        assert len(learning.observations("dev-1")) == 2
        assert [r["observation_id"] for r in learning.observations("dev-1", "confirmed")] == [
            "obs-1"
        ]
        assert [
            r["observation_id"] for r in learning.observations("dev-1", since=AT(1))
        ] == ["obs-2"]
    finally:
        db.close()


def test_dirty_subjects_derives_from_confirmed_observations(tmp_path: Path) -> None:
    path = tmp_path / "dirty.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        models = db.repos.models
        learning = db.repos.learning

        # Use a subject with no legacy copy so version numbers are free.
        subject = "learner-1"

        # No active version: any confirmed observation makes the subject dirty.
        learning.put_observation(_observation("obs-1", subject, "confirmed", AT(0)))
        assert learning.dirty_subjects() == [("device", subject)]

        # Provisional observations do not make a subject dirty.
        learning.put_observation(_observation("obs-2", "learner-2", "provisional", AT(0)))
        assert learning.dirty_subjects() == [("device", subject)]

        # An active version created *after* the observation clears dirtiness.
        version = _version(subject, 1, at=AT(10))
        models.append_version(version)
        models.append_event(_event(version.version_id, "activated", AT(11)))
        assert learning.dirty_subjects() == []

        # A newer confirmed observation makes it dirty again.
        learning.put_observation(_observation("obs-3", subject, "confirmed", AT(20)))
        assert learning.dirty_subjects() == [("device", subject)]
    finally:
        db.close()
