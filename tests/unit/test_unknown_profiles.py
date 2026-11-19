"""T6 — Unknown Load Profile engine and HTTP surface.

Run with::

    .venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_unknown_profiles.py

The engine is pure and depends only on the K03/K04 contracts and a few tiny
injected sinks, so the tests build against those directly (never against
``runtime.fakes`` in production code).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from wattmosaic_engine.api.server import create_app
from wattmosaic_engine.contracts import (
    CandidateSource,
    EventGroupStatus,
    UnknownLoadProfileV3,
)
from wattmosaic_engine.inference import generate_candidates
from wattmosaic_engine.residual.unknown_profiles import (
    MIN_CHARACTERISED_OCCURRENCES,
    DeviceSink,
    MappingEdgeSink,
    ProfilePromotion,
    ProfileSink,
    UnknownOccurrence,
    UnknownProfileView,
    apply_occurrences,
    promote_profile,
    unexplained_scopes,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Small injected sinks (no DB, deterministic).
# --------------------------------------------------------------------------- #


@dataclass
class _DeviceRecorder(DeviceSink):
    created: list = field(default_factory=list)

    def create_device(self, device):
        self.created.append(device)
        return device


@dataclass
class _EdgeRecorder(MappingEdgeSink):
    edges: list = field(default_factory=list)

    def add_mapping_edge(self, edge):
        self.edges.append(edge)
        return edge


@dataclass
class _ProfileRecorder(ProfileSink):
    stored: dict = field(default_factory=dict)

    def put_profile(self, profile):
        self.stored[profile.profile_id] = profile


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _profile(
    *,
    profile_id: str = "ulp-fridge",
    scope: str = "ct_b",
    lifecycle: str = "characterised",
    count: int = 5,
    delta_w: float = 150.0,
    first_seen: datetime = T0,
    last_seen: datetime | None = None,
) -> UnknownLoadProfileV3:
    return UnknownLoadProfileV3(
        profile_id=profile_id,
        scope_endpoint_id=scope,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        signature={
            "delta_w_median": delta_w,
            "peak_w": abs(delta_w),
            "duration_bucket": 0.0,
        },
        event_count=count,
        independent_event_count=count,
        first_seen=first_seen,
        last_seen=last_seen or first_seen + timedelta(days=count),
    )


def _occurrence(
    *,
    at: datetime = T0,
    delta_w: float = 150.0,
    scope: str = "ct_b",
    duration_s: float | None = 10.0,
) -> UnknownOccurrence:
    return UnknownOccurrence(
        at=at, delta_w=delta_w, scope_endpoint_id=scope, duration_s=duration_s
    )


# --------------------------------------------------------------------------- #
# Detection from the HB §17 reference house (reader-only evidence).
# --------------------------------------------------------------------------- #


def test_fridge_without_ha_becomes_characterised() -> None:
    from wattmosaic_engine.realtime import run_unknown_detection

    from tools.simulator.hb17 import build

    observation = build(17).observation_view()
    result = run_unknown_detection(observation)
    assert any(
        e.status.value in ("detected", "matching", "provisional", "confirmed")
        for e in result.event_groups
    )
    assert any(
        CandidateSource.UNKNOWN_SCOPE in c.sources for c in result.candidates
    )

    profiles = result.unknown_profiles
    fridge = next((p for p in profiles if p.scope_endpoint_id == "ct_b"), None)
    assert fridge is not None, "the fridge step must create a ct_b profile"
    for _ in range(MIN_CHARACTERISED_OCCURRENCES):
        result = run_unknown_detection(observation, profiles=result)
        profiles = result.unknown_profiles
    fridge = next(p for p in profiles if p.scope_endpoint_id == "ct_b")
    assert fridge.lifecycle == "characterised"
    assert fridge.event_count >= MIN_CHARACTERISED_OCCURRENCES


def test_profile_is_never_confirmed() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        UnknownLoadProfileV3(
            profile_id="p",
            scope_endpoint_id="ct_b",
            lifecycle="confirmed",  # type: ignore[arg-type]
            first_seen=T0,
            last_seen=T0,
        )
    # Even after many occurrences the lifecycle stays CHARACTERISED, never
    # CONFIRMED (the background detector does not promote a profile).
    profile = _profile(count=1, lifecycle="observing")
    occurrences = [
        _occurrence(at=T0 + timedelta(days=i)) for i in range(50)
    ]
    updated = apply_occurrences([profile], occurrences, now=T0 + timedelta(days=60))
    assert all(p.lifecycle != "confirmed" for p in updated)


# --------------------------------------------------------------------------- #
# Clustering and lifecycle (pure).
# --------------------------------------------------------------------------- #


def test_clusters_within_twenty_percent_and_rejects_other_sign() -> None:
    profile = _profile(lifecycle="observing", count=1, delta_w=150.0)
    same = apply_occurrences(
        [profile], [_occurrence(delta_w=160.0)], now=T0 + timedelta(days=1)
    )
    assert len(same) == 1
    assert same[0].event_count == 2
    assert same[0].profile_id == profile.profile_id

    opposite = apply_occurrences(
        [profile], [_occurrence(delta_w=-150.0)], now=T0 + timedelta(days=1)
    )
    assert len(opposite) == 2, "a different sign must form a new cluster"
    far = apply_occurrences(
        [profile], [_occurrence(delta_w=400.0)], now=T0 + timedelta(days=1)
    )
    assert len(far) == 2, "a magnitude outside ±20 % must form a new cluster"


def test_lifecycle_observing_to_characterised() -> None:
    profile = _profile(lifecycle="observing", count=1)
    occurrences = [_occurrence(at=T0 + timedelta(hours=i)) for i in range(1, 5)]
    updated = apply_occurrences([profile], occurrences, now=T0 + timedelta(days=1))
    assert len(updated) == 1
    assert updated[0].lifecycle == "characterised"
    assert updated[0].event_count == MIN_CHARACTERISED_OCCURRENCES
    assert updated[0].independent_event_count == MIN_CHARACTERISED_OCCURRENCES


def test_periodic_tag_is_set_for_regular_cycles() -> None:
    profile = _profile(lifecycle="observing", count=1)
    occurrences = [
        _occurrence(at=T0 + timedelta(hours=6 * i)) for i in range(1, 6)
    ]
    updated = apply_occurrences([profile], occurrences, now=T0 + timedelta(days=3))
    assert updated[0].signature["periodic"] == 1.0
    assert updated[0].lifecycle == "characterised"


def test_retire_after_thirty_days_and_keep_retired_row() -> None:
    profile = _profile(last_seen=T0)
    # An occurrence long after last_seen first retires the stale profile, then
    # creates a fresh one — the retired row is retained, never deleted.
    updated = apply_occurrences(
        [profile], [_occurrence(at=T0 + timedelta(days=45))], now=T0 + timedelta(days=45)
    )
    assert any(p.lifecycle == "retired" for p in updated)
    assert len(updated) == 2
    retired = next(p for p in updated if p.lifecycle == "retired")
    assert retired.profile_id == profile.profile_id


def test_unexplained_scopes_from_joint_result() -> None:
    from wattmosaic_engine.contracts import (
        JointInferenceResult,
        UnknownEstimate,
        VersionTriple,
    )

    result = JointInferenceResult(
        status=EventGroupStatus.UNRESOLVED,
        device_estimates=[],
        group_estimates=[],
        unknown_estimates=[UnknownEstimate(scope_reader_id="ct_b")],
        candidates=[],
        matches={},
        residual_w_by_reader={"ct_b": None, "ct_sub": 150.0},
        versions=VersionTriple(
            topology_version_id="topo-1",
            mapping_version_id="map-1",
            model_set_id="mset-1",
        ),
        computed_at=T0,
    )
    scopes = unexplained_scopes(result)
    assert "ct_b" in scopes
    # An UNRESOLVED group leaves every stepped residual reader unexplained; a
    # reader whose residual is missing (None) is *absent*, not a step.
    assert "ct_sub" in scopes
    assert result.residual_w_by_reader["ct_b"] is None  # no fabricated 0 W

    # A resolved group with an unexplained estimate still reports its scope but
    # does not sweep in other readers.
    result.status = EventGroupStatus.PROVISIONAL  # type: ignore[assignment]
    assert unexplained_scopes(result) == ("ct_b",)


# --------------------------------------------------------------------------- #
# I1 source-6 view: profiles_for_scope.
# --------------------------------------------------------------------------- #


def test_profiles_for_scope_feeds_i1_unknown_profile_source() -> None:
    from wattmosaic_engine.contracts import EventGroup, ReaderDelta, VersionTriple

    profile = _profile(lifecycle="characterised")
    view = UnknownProfileView(profiles=(profile,))
    assert view.profiles_for_scope("ct_b") == (profile,)
    assert view.profiles_for_scope("ct_other") == ()

    # I1 consumes the view as source 6 through the unknown_profiles seam.
    group = EventGroup(
        group_id="eg-1",
        revision=1,
        status=EventGroupStatus.DETECTED,
        opened_at=T0,
        reader_deltas=[
            ReaderDelta(
                reader_id="Circuit B",
                delta_w=150.0,
                sigma_w=3.0,
                completeness="complete",
            )
        ],
        versions=VersionTriple(
            topology_version_id="topo-1",
            mapping_version_id="map-1",
            model_set_id="mset-1",
        ),
    )
    # I1 consumes the view as the ``unknown_profiles`` provider: it calls
    # ``profiles_for_scope(reader)`` when it sees a ProfileView.
    scoped = UnknownProfileView(
        profiles=(_profile(lifecycle="characterised", scope="Circuit B"),)
    )
    from tests.support.fakes import (
        FakeMapping,
        FakeModelSet,
        FakeTopology,
        fake_operator,
    )

    topology = FakeTopology()
    mapping = FakeMapping.from_pairs({})
    operator = fake_operator(topology, mapping, ("Circuit B",), (), at=T0)
    candidates = generate_candidates(
        group,
        operator,
        FakeModelSet.fixed({}),
        mapping,
        scoped,
    ).all_hypotheses
    assert any(CandidateSource.UNKNOWN_PROFILE in c.sources for c in candidates)


# --------------------------------------------------------------------------- #
# Promotion (user-driven).
# --------------------------------------------------------------------------- #


def test_promotion_creates_device_and_provisional_edge_and_leaves_profile_promoted() -> None:
    from wattmosaic_engine.contracts import EdgeLifecycleV3, Observability

    profile = _profile(lifecycle="characterised")
    devices = _DeviceRecorder()
    edges = _EdgeRecorder()
    profiles = _ProfileRecorder()
    promoted = promote_profile(
        profile,
        devices=devices,
        mapping_edges=edges,
        profiles=profiles,
        clock=lambda: T0 + timedelta(days=1),
    )
    assert isinstance(promoted, ProfilePromotion)
    assert promoted.device.origin == "unknown_profile_promotion"
    assert promoted.mapping_edge.lifecycle == EdgeLifecycleV3.PROVISIONAL
    assert promoted.mapping_edge.observability in (
        Observability.INFERRED,
        Observability.PARTIAL,
    )
    assert promoted.profile.lifecycle == "promoted"
    assert profiles.stored[profile.profile_id].lifecycle == "promoted"
    assert len(devices.created) == 1
    assert len(edges.edges) == 1


def test_promotion_refuses_observing_and_already_promoted_profiles() -> None:
    devices = _DeviceRecorder()
    edges = _EdgeRecorder()
    profiles = _ProfileRecorder()
    for lifecycle in ("observing", "promoted", "retired"):
        profile = _profile(lifecycle=lifecycle)
        assert (
            promote_profile(
                profile,
                devices=devices,
                mapping_edges=edges,
                profiles=profiles,
                clock=lambda: T0,
            )
            is None
        )
    assert devices.created == []
    assert edges.edges == []


def test_residual_row_is_never_counted_as_a_device(tmp_path) -> None:
    """A profile/residual row is not a device; only D2 promotion creates one."""
    from wattmosaic_engine.devices.service import DeviceService
    from wattmosaic_engine.storage import Database

    db = Database(tmp_path / "profiles.db")
    service = DeviceService(db)
    before = len(db.repos.entities.devices())

    profile = _profile(lifecycle="candidate")
    edges = _EdgeRecorder()
    profiles = _ProfileRecorder()
    # The profile row on its own is not a device — no D2 call has been made.
    assert len(db.repos.entities.devices()) == before

    promoted = promote_profile(
        profile,
        devices=service,
        mapping_edges=edges,
        profiles=profiles,
        clock=lambda: T0,
    )
    assert promoted is not None
    assert len(db.repos.entities.devices()) == before + 1
    stored = service.get(promoted.device.device_id)
    assert stored is not None
    assert stored.origin == "unknown_profile_promotion"
    assert not stored.device_id.startswith("dev_")  # never a dev_<ct> device
    db.close()


# --------------------------------------------------------------------------- #
# HTTP route surface.
# --------------------------------------------------------------------------- #


class _ProfileFacade:
    def __init__(self, profile: UnknownLoadProfileV3 | None) -> None:
        self.profile = profile
        self.promoted: list[str] = []

    def unknown_profiles(self, scope: str | None = None):
        if self.profile is None:
            return {"profiles": []}
        if scope is not None and self.profile.scope_endpoint_id != scope:
            return {"profiles": []}
        return {"profiles": [self.profile.model_dump(mode="json")]}

    def promote_unknown_profile(self, profile_id: str):
        if self.profile is None or self.profile.profile_id != profile_id:
            return None  # server maps to 404
        self.promoted.append(profile_id)
        return {
            "device_id": f"pd-ulp-{profile_id}",
            "mapping_edge_id": f"m::unknown_profile::{profile_id}",
            "lifecycle": "promoted",
        }


@pytest.mark.asyncio
async def test_unknown_profiles_routes() -> None:
    facade = _ProfileFacade(_profile())
    app = create_app(facade)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/v1/unknown-profiles")
        assert resp.status == 200
        body = await resp.json()
        assert body["profiles"][0]["scope_endpoint_id"] == "ct_b"

        resp = await client.get("/api/v1/unknown-profiles?scope=ct_other")
        assert resp.status == 200
        assert (await resp.json())["profiles"] == []

        resp = await client.post(
            "/api/v1/unknown-profiles/ulp-fridge/promote", json={}
        )
        assert resp.status == 201
        assert (await resp.json())["lifecycle"] == "promoted"
        assert facade.promoted == ["ulp-fridge"]

        resp = await client.post(
            "/api/v1/unknown-profiles/does-not-exist/promote", json={}
        )
        assert resp.status == 404


# --------------------------------------------------------------------------- #
# Purity / no fakes in production.
# --------------------------------------------------------------------------- #


def test_production_module_does_not_import_runtime_fakes() -> None:
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "engine/wattmosaic_engine"
    for path in (
        root / "residual/unknown_profiles.py",
        root / "api/routes/unknown_profiles.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (
                    node.module and "runtime" in node.module and "fakes" in node.module
                ), f"{path} imports runtime.fakes"
            if isinstance(node, ast.Import):
                assert not any(
                    "runtime" in alias.name and "fakes" in alias.name
                    for alias in node.names
                ), f"{path} imports runtime.fakes"
