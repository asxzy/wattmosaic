"""Candidate population and the frozen ``discover/placements`` response.

Why this file exists
--------------------
Auto placement used to be starved: it reasoned only about entities the topology
already declared, which on a real installation is one device's entities.  The
fix is a structural candidate population (Home Assistant *domains* that prove
runtime, minus what is already accounted for).  These tests pin that rule and the
response contract around it, because both are easy to regress in a way that looks
like "the engine found nothing":

* a ``light.*`` with no registry row must still be considered (registry absence
  is not disqualifying), while a ``climate.*`` must be refused as a mode entity;
* a declared entity must not be re-offered, and WattMosaic's own output must
  never be a candidate;
* the response must carry every frozen key even when nothing was analysed, and
  ``groups`` must be present (empty) when tier-B fitting is unavailable;
* nothing may be written unless ``apply`` and the stored opt-in agree.

The recorder is a fake throughout: these tests never touch the network.
"""

from __future__ import annotations

import asyncio
import math
import sys
import types
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from wattmosaic_engine.api.facade import InvalidRequest, NullFacade
from wattmosaic_engine.api.server import APIServer, create_app
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    Phase,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.ingestion.candidate_entities import (
    REASON_ALREADY_DECLARED,
    REASON_MODE_ENTITY,
    REASON_WATTMOSAIC_ENTITY,
    CandidateEntity,
    candidate_state_entities,
)
from wattmosaic_engine.ingestion.ha_source import ConnectionState
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

#: The frozen §3.3 keys.  A response missing any of these is a contract break.
RESPONSE_KEYS = {
    "ok",
    "applied",
    "lookback_hours",
    "topology_version",
    "stats",
    "accepted",
    "refused",
    "groups",
    "withheld",
    "skipped",
    "notes",
}

STATS_KEYS = {
    "candidate_entities",
    "refused_entities",
    "branch_rows",
    "observations",
    "accepted",
    "groups",
    "withheld",
}

BASELINE_W = 8.35
FIXTURE_W = 19.57
CYCLES = 3


# --------------------------------------------------------------------------- #
# Candidate population (pure)
# --------------------------------------------------------------------------- #


def _state(entity_id: str, *, state: str = "off", **attributes: Any) -> dict[str, Any]:
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


def _registries() -> dict[str, Any]:
    return {
        "entities": [
            {
                "entity_id": "light.with_device",
                "device_id": "dev_lutron",
                "platform": "lutron_caseta",
                "area_id": "area_kitchen",
            },
            {"entity_id": "light.without_device", "platform": "lutron_caseta"},
            {"entity_id": "climate.hallway", "platform": "nest"},
            {"entity_id": "sensor.power_meter", "platform": "refoss"},
            {"entity_id": "sensor.wattmosaic_engine_total_incoming_power", "platform": "wattmosaic"},
        ],
        "devices": [
            {
                "id": "dev_lutron",
                "name": "Lutron",
                "manufacturer": "Lutron Electronics Co., Inc",
            }
        ],
    }


def test_candidate_set_is_domain_based_and_registry_absence_is_not_disqualifying() -> None:
    states = [
        _state("light.with_device", friendly_name="Island Lights"),
        _state("light.without_device", friendly_name="Porch Light"),
        _state("climate.hallway", friendly_name="Hallway"),
        _state("sensor.power_meter", friendly_name="Meter"),
    ]

    candidates, refused = candidate_state_entities(states, _registries())

    by_id = {candidate.entity_id: candidate for candidate in candidates}
    assert set(by_id) == {"light.with_device", "light.without_device"}
    assert [c.entity_id for c in candidates] == sorted(by_id), "output must be sorted"

    with_device = by_id["light.with_device"]
    assert with_device.registry_device_id == "dev_lutron"
    assert with_device.manufacturer == "Lutron Electronics Co., Inc"
    assert with_device.area_id == "area_kitchen"
    assert with_device.friendly_name == "Island Lights"
    assert with_device.evidence == ("HA light entity", "Lutron Electronics Co., Inc")

    without = by_id["light.without_device"]
    assert without.registry_device_id is None
    assert without.area_id is None
    assert without.evidence == ("HA light entity (no registry device)",)

    assert refused["climate.hallway"] == REASON_MODE_ENTITY
    assert refused["sensor.power_meter"] == "not_a_load_domain"


def test_all_light_candidates_are_offered_when_none_are_declared() -> None:
    """The starvation defect: 81 linked lights must all be candidates."""
    registries = {
        "entities": [
            {"entity_id": f"light.{index:02d}", "device_id": f"dev_{index:02d}"}
            for index in range(81)
        ],
        "devices": [{"id": f"dev_{index:02d}"} for index in range(81)],
    }
    states = [_state(f"light.{index:02d}") for index in range(81)]

    candidates, refused = candidate_state_entities(states, registries)

    assert len(candidates) == 81
    assert refused == {}


def test_a_declared_state_entity_is_not_re_offered() -> None:
    topology = Topology(
        channels=[],
        devices=[
            {
                "device_id": "dev_island",
                "metadata": {"state_entity_ids": ["light.with_device"]},
            }
        ],
    )
    states = [_state("light.with_device"), _state("light.without_device")]

    candidates, refused = candidate_state_entities(states, _registries(), topology)

    assert [c.entity_id for c in candidates] == ["light.without_device"]
    assert refused["light.with_device"] == REASON_ALREADY_DECLARED


def test_wattmosaic_own_entities_are_never_candidates() -> None:
    """The engine must not ingest its own estimate as if it were a load."""
    states = [
        _state("sensor.wattmosaic_engine_total_incoming_power"),
        # Platform is authoritative even when the id carries no prefix.
        _state("light.estimated_fixture"),
    ]
    registries = {
        "entities": [
            {"entity_id": "light.estimated_fixture", "platform": "wattmosaic"},
        ],
        "devices": [],
    }

    candidates, refused = candidate_state_entities(states, registries)

    assert candidates == []
    assert refused["sensor.wattmosaic_engine_total_incoming_power"] == REASON_WATTMOSAIC_ENTITY
    assert refused["light.estimated_fixture"] == REASON_WATTMOSAIC_ENTITY


def test_candidate_entity_is_a_frozen_value_object() -> None:
    candidate = CandidateEntity(
        entity_id="light.a", domain="light", friendly_name="A"
    )
    with pytest.raises(FrozenInstanceError):
        candidate.entity_id = "light.b"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Engine wiring
# --------------------------------------------------------------------------- #


def _sample(at: datetime, value: Any) -> dict[str, Any]:
    return {"s": str(value), "lu": at.timestamp()}


def _fill(rows: list[dict[str, Any]], start: datetime, end: datetime, watts: float) -> None:
    moment = start + timedelta(seconds=20)
    while moment < end - timedelta(seconds=5):
        rows.append(_sample(moment, round(watts, 3)))
        moment += timedelta(seconds=30)


def _history() -> dict[str, list[dict[str, Any]]]:
    start = datetime.now(UTC) - timedelta(hours=4)
    ct: list[dict[str, Any]] = [_sample(start, BASELINE_W)]
    fixture: list[dict[str, Any]] = [_sample(start, "off")]
    moment = start
    for _ in range(CYCLES):
        on_at = moment + timedelta(minutes=10)
        off_at = on_at + timedelta(minutes=3)
        _fill(ct, moment, on_at, BASELINE_W)
        fixture.append(_sample(on_at, "on"))
        _fill(ct, on_at, off_at, BASELINE_W + FIXTURE_W)
        fixture.append(_sample(off_at, "off"))
        moment = off_at
    _fill(ct, moment, moment + timedelta(minutes=5), BASELINE_W)
    return {"sensor.ct1_power": ct, "light.island": fixture}


class _FakeSource:
    """States, registries and recorder history; never touches the network."""

    connected = True
    state = ConnectionState.CONNECTED
    last_event_at = None

    def __init__(self, *, history: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.payload = history if history is not None else _history()
        self.calls: list[list[str]] = []

    async def state_snapshot(self) -> list[dict[str, Any]]:
        return [
            _state("light.island", friendly_name="Island Lights"),
            _state("climate.hallway", friendly_name="Hallway"),
            _state("sensor.power_meter", friendly_name="Meter"),
        ]

    async def registries(self) -> dict[str, Any]:
        return {
            "entities": [{"entity_id": "light.island", "device_id": "dev_lutron"}],
            "devices": [{"id": "dev_lutron", "manufacturer": "Lutron"}],
        }

    async def history(
        self,
        entity_ids: Any,
        *,
        start: datetime,
        end: datetime | None = None,
        include_attributes: bool = False,
        timeout_s: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        self.calls.append(list(entity_ids))
        wanted = set(entity_ids)
        return {key: rows for key, rows in self.payload.items() if key in wanted}


class _WideSource:
    """Two candidate lights, so a limit of one has something to omit."""

    connected = True
    state = ConnectionState.CONNECTED
    last_event_at = None

    async def state_snapshot(self) -> list[dict[str, Any]]:
        return [
            _state("light.island", friendly_name="Island Lights"),
            _state("light.porch", friendly_name="Porch Light"),
        ]

    async def registries(self) -> dict[str, Any]:
        return {"entities": [], "devices": []}

    async def history(
        self,
        entity_ids: Any,
        *,
        start: datetime,
        end: datetime | None = None,
        include_attributes: bool = False,
        timeout_s: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        payload = _history()
        payload["light.porch"] = payload["light.island"]
        wanted = set(entity_ids)
        return {key: rows for key, rows in payload.items() if key in wanted}


def _topology() -> Topology:
    return Topology(
        status=TopologyStatus.CONFIRMED,
        version=1,
        channels=[
            CTChannel(
                ct_id="main",
                entity_id="sensor.main_power",
                ct_type=CTType.MAIN,
                phase=Phase.BOTH,
            ),
            CTChannel(
                ct_id="ct1",
                entity_id="sensor.ct1_power",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
            ),
        ],
        devices=[],
    )


def _core(
    tmp_path: Path, *, auto_placement: bool = False
) -> tuple[EngineCore, Database, _FakeSource, list[str]]:
    db = Database(tmp_path / "wm.db")
    config = EngineConfig(
        database_path=str(tmp_path / "wm.db"),
        ha_token="not-used",
        auto_placement=auto_placement,
    )
    core = EngineCore(config, db)
    source = _FakeSource()
    core.hasource = source  # type: ignore[assignment]
    topology = _topology()
    db.save_topology(topology, note="test")
    core.topology_cache = topology

    writes: list[str] = []
    original_save_configurations = db.save_device_configurations
    original_save_topology = db.save_topology

    def save_configurations(configurations: Any) -> None:
        writes.append("save_device_configurations")
        original_save_configurations(configurations)

    def save_topology(stored: Any, note: str = "") -> int:
        writes.append("save_topology")
        return original_save_topology(stored, note=note)

    db.save_device_configurations = save_configurations  # type: ignore[method-assign]
    db.save_topology = save_topology  # type: ignore[method-assign]
    return core, db, source, writes


def test_response_carries_every_frozen_key_and_groups_is_empty_without_group_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Make the guarded import fail deterministically even once group_fit lands.
    monkeypatch.setitem(sys.modules, "wattmosaic_engine.disaggregation.group_fit", None)
    core, _, _, _ = _core(tmp_path)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    assert set(result) >= RESPONSE_KEYS
    assert set(result["stats"]) >= STATS_KEYS
    assert result["groups"] == []
    assert result["stats"]["candidate_entities"] == 1
    assert result["stats"]["refused_entities"] == 2
    assert result["accepted"], "the correlated fixture must still be reported"
    accepted = result["accepted"][0]
    assert accepted["entity_id"] == "light.island"
    assert accepted["name"] == "Island Lights"
    assert accepted["ct_label"] == "ct1"
    assert accepted["applied"] is False


def test_include_groups_false_still_reports_an_empty_group_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_group_fit(monkeypatch)
    core, _, _, _ = _core(tmp_path)

    result = asyncio.run(
        core.discover_placements(lookback_hours=24, apply=False, include_groups=False)
    )

    assert "groups" in result
    assert result["groups"] == []
    assert result["stats"]["groups"] == 0


def test_group_fit_is_called_with_main_rows_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier-B groups are evidence only, and mains are never group targets.

    Contract §4 freezes the keyword set of ``fit_shared_rows`` and forbids a
    group on a ``main``/``dedicated_device`` row.  The engine therefore filters
    the *inputs* rather than passing an extra keyword: a strict implementation
    of the frozen signature must not have to accept anything new, and a main row
    must be absent from ``row_readings`` rather than merely ignored downstream.
    """
    recorded: dict[str, Any] = {}

    def fake_fit(**kwargs: Any) -> tuple[Any, ...]:
        recorded.update(kwargs)
        return (
            _StubGroupFit(
                ct_id="ct1",
                members=(
                    _StubGroupMember("light.island", None),
                    _StubGroupMember("light.porch", None),
                ),
            ),
        )

    monkeypatch.setitem(
        sys.modules,
        "wattmosaic_engine.disaggregation.group_fit",
        types.SimpleNamespace(fit_shared_rows=fake_fit),
    )
    core, _, _, _ = _core(tmp_path)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    assert set(recorded) == {
        "row_readings",
        "entity_states",
        "entity_levels",
        "row_noise_w",
    }, "the call must match the frozen contract §4 signature exactly"
    assert set(recorded["row_readings"]) == {"ct1"}, "a main row is never a group target"
    assert result["groups"] and result["groups"][0]["individually_identifiable"] is False
    assert result["groups"][0]["members"][0]["estimated_w"] is None
    # A member that was measured well enough to place solo is not "withheld" --
    # it got a real device -- but its co-toggling neighbour is.
    assert "light.island" not in result["withheld"]
    assert result["withheld"]["light.porch"] == "co_toggles_with_another_fixture_on_ct1"
    # A group never writes a device.
    assert [item["entity_id"] for item in result["accepted"]] == ["light.island"]
    assert result["applied"] is False


def test_max_candidates_truncates_and_says_so(tmp_path: Path) -> None:
    """A truncated run must not read as "there was nothing else to analyse"."""
    core, _, _, _ = _core(tmp_path)
    core.hasource = _WideSource()  # type: ignore[assignment]

    result = asyncio.run(
        core.discover_placements(lookback_hours=24, apply=False, max_candidates=1)
    )

    assert result["stats"]["candidate_entities"] == 1
    assert any("max_candidates=1" in note for note in result["notes"])


def test_placed_fixture_is_not_reported_again_as_a_group_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixture has exactly one wattage story, and it is the measured one.

    Real production data produced a group containing the same fixture tier A had
    just placed, so the payload carried both a measured ``on_w`` (Placed) and a
    fitted ``total_w`` (Group) for one lamp.  Two numbers for one device is the
    contradiction the two tiers exist to avoid, and the aggregate fit is the
    weaker claim, so it must yield.
    """

    def fake_fit(**_: Any) -> tuple[Any, ...]:
        return (
            _StubGroupFit(
                ct_id="ct1",
                members=(
                    _StubGroupMember("light.island", 70.6),
                    _StubGroupMember("light.porch", None),
                ),
            ),
        )

    monkeypatch.setitem(
        sys.modules,
        "wattmosaic_engine.disaggregation.group_fit",
        types.SimpleNamespace(fit_shared_rows=fake_fit),
    )
    core, _, _, _ = _core(tmp_path)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    assert [item["entity_id"] for item in result["accepted"]] == ["light.island"]
    members = [m["entity_id"] for group in result["groups"] for m in group["members"]]
    assert "light.island" not in members, "the placed fixture must not be grouped too"
    assert members == ["light.porch"]
    assert result["withheld"]["light.porch"] == "co_toggles_with_another_fixture_on_ct1"


def test_a_single_fixture_group_reports_its_wattage_as_identifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag must describe the evidence, not be a constant.

    Half of the groups on the production install contain one fixture, whose
    coefficient the aggregate fit genuinely identified.  Hard-coding ``False``
    made the payload deny a measured wattage the engine actually held and told the
    UI to caveat a number that was sound.
    """

    def fake_fit(**_: Any) -> tuple[Any, ...]:
        return (
            _StubGroupFit(ct_id="ct1", members=(_StubGroupMember("light.porch", 42.0),)),
        )

    monkeypatch.setitem(
        sys.modules,
        "wattmosaic_engine.disaggregation.group_fit",
        types.SimpleNamespace(fit_shared_rows=fake_fit),
    )
    core, _, _, _ = _core(tmp_path)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    group = result["groups"][0]
    assert group["individually_identifiable"] is True
    assert group["members"][0]["estimated_w"] == 42.0
    # Identified by the aggregate fit is *not* the same claim as measured by a
    # transition pair, so the honest reason still refuses to make it a device.
    assert result["withheld"]["light.porch"] == "identified_by_aggregate_fit_on_ct1"


def test_a_two_fixture_group_still_reports_co_toggling_after_one_is_placed_solo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The withholding reason is about the original group, not the survivors.

    Once the placed member is filtered out of the member list, a two-fixture
    group is a one-element list.  Deriving the reason from that filtered size
    would claim the survivor "co-toggles with another fixture" when the only
    other fixture was the one that got its own measured device.
    """

    def fake_fit(**_: Any) -> tuple[Any, ...]:
        return (
            _StubGroupFit(
                ct_id="ct1",
                members=(
                    _StubGroupMember("light.island", 70.6),
                    _StubGroupMember("light.porch", None),
                ),
            ),
        )

    monkeypatch.setitem(
        sys.modules,
        "wattmosaic_engine.disaggregation.group_fit",
        types.SimpleNamespace(fit_shared_rows=fake_fit),
    )
    core, _, _, _ = _core(tmp_path)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    assert len(result["groups"][0]["members"]) == 1
    assert result["withheld"]["light.porch"] == "co_toggles_with_another_fixture_on_ct1"


def _install_group_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the guarded group-fit import resolve to a deterministic stub."""
    monkeypatch.setitem(
        sys.modules,
        "wattmosaic_engine.disaggregation.group_fit",
        types.SimpleNamespace(fit_shared_rows=lambda **_: ()),
    )


@dataclass(frozen=True, slots=True)
class _StubGroupMember:
    entity_id: str
    estimated_w: float | None


@dataclass(frozen=True, slots=True)
class _StubGroupFit:
    ct_id: str
    members: tuple[_StubGroupMember, ...]
    total_w: float = 363.8
    r2: float = 0.79
    r2_null_p95: float = 0.14
    r2_margin: float = 0.65
    evidence: tuple[str, ...] = ("stub evidence",)

    @property
    def individually_identifiable(self) -> bool:
        """Mirror the real ``GroupFit``: derived from measured coefficients.

        The engine reads this property rather than assuming a constant, so the
        stub must derive it the same way or the two would disagree about whether
        a single-fixture group carries a trustworthy wattage.
        """
        return any(member.estimated_w is not None for member in self.members)


def test_apply_false_writes_nothing(tmp_path: Path) -> None:
    core, _, _, writes = _core(tmp_path, auto_placement=True)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    assert result["applied"] is False
    assert all(item["applied"] is False for item in result["accepted"])
    assert writes == []


def test_apply_true_without_the_opt_in_writes_nothing_and_explains_why(
    tmp_path: Path,
) -> None:
    # An explicit ``apply=True`` is the caller's consent, so the *stored* option
    # is what must gate it here -- false means "not enabled".
    core, db, _, writes = _core(tmp_path, auto_placement=False)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=True))

    assert result["applied"] is False
    assert "disabled" in result["detail"]
    assert writes == []
    assert db.load_device_configurations().devices == []


def test_apply_and_opt_in_writes_and_reports_applied(tmp_path: Path) -> None:
    core, db, _, writes = _core(tmp_path, auto_placement=True)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=True))

    assert result["applied"] is True
    assert all(item["applied"] is True for item in result["accepted"])
    assert "save_device_configurations" in writes
    assert "save_topology" in writes
    configurations = db.load_device_configurations().devices
    assert [config.state_entity_ids[0] for config in configurations] == ["light.island"]
    assert result["topology_version"] == 2


def test_a_candidate_counter_and_refusals_report_the_mode_entity(tmp_path: Path) -> None:
    core, _, _, _ = _core(tmp_path)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    # ``climate`` and ``sensor`` are refused, and the climate refusal is named
    # (a sensor is only counted, because a whole-house snapshot has hundreds).
    assert result["refused"].get("climate.hallway") == REASON_MODE_ENTITY
    assert result["stats"]["refused_entities"] == 2
    assert any("not candidates" in note for note in result["notes"])


# --------------------------------------------------------------------------- #
# Learning unblock (§6)
# --------------------------------------------------------------------------- #


class _BlockedBackend:
    def learning_status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "active": True,
            "effective_state": "blocked",
            "samples_seen": 109,
            "samples_accepted": 0,
            "samples_rejected": 109,
            "rejection_reasons": {"ambiguous_topology": 109},
            "dominant_rejection_reason": "ambiguous_topology",
            "blocked_by": "observed_rejections",
            "topology_quality": "candidate",
            "summary": "Learning is running but the topology is still a candidate.",
        }


def test_blocked_learning_names_the_unconfirmed_topology(tmp_path: Path) -> None:
    core, _, _, _ = _core(tmp_path)
    core.inference_backend = _BlockedBackend()

    learning = core.status()["learning"]

    assert learning["effective_state"] == "blocked"
    assert learning["blocked_reason_detail"] == "topology_not_confirmed"
    assert "Confirm the topology" in learning["remedy"]
    # No behaviour change: the topology is the user's call, never auto-confirmed.
    assert core._active_topology().status == TopologyStatus.CONFIRMED


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #


class _StubPlacementFacade(NullFacade):
    def __init__(self) -> None:
        super().__init__("stub")
        self.calls: list[dict[str, Any]] = []

    async def discover_placements(
        self,
        *,
        lookback_hours: float = 24.0,
        apply: bool | None = None,
        include_groups: bool = True,
        max_candidates: int = 200,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "lookback_hours": lookback_hours,
                "apply": apply,
                "include_groups": include_groups,
                "max_candidates": max_candidates,
            }
        )
        return {
            "ok": True,
            "applied": False,
            "lookback_hours": lookback_hours,
            "topology_version": 3,
            "stats": dict.fromkeys(STATS_KEYS, 0),
            "accepted": [],
            "refused": {},
            "groups": [],
            "withheld": {},
            "skipped": {},
            "notes": [],
        }


async def _with_server(facade: Any, fn: Any) -> Any:
    server = APIServer(facade, host="127.0.0.1", port=0, api_token="")
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            return await fn(session, f"http://127.0.0.1:{server.port}/api/v1")
    finally:
        await server.stop()


def test_route_is_registered_and_returns_the_shaped_answer() -> None:
    facade = _StubPlacementFacade()

    async def run(session: aiohttp.ClientSession, base: str) -> Any:
        async with session.post(f"{base}/discover/placements", json={}) as response:
            assert response.status == 200, await response.text()
            return await response.json()

    body = asyncio.run(_with_server(facade, run))

    assert body["ok"] is True
    assert body["groups"] == []
    assert facade.calls == [
        {
            "lookback_hours": 24.0,
            "apply": None,
            "include_groups": True,
            "max_candidates": 200,
        }
    ]


def test_null_facade_reports_unavailable_without_raising() -> None:
    """No engine yet must still answer in the frozen shape (contract §3.2)."""
    data = NullFacade().discover_placements()

    assert data == {
        "ok": False,
        "detail": "not available",
        "accepted": [],
        "refused": {},
        "groups": [],
    }


def test_route_forwards_valid_overrides() -> None:
    facade = _StubPlacementFacade()

    async def run(session: aiohttp.ClientSession, base: str) -> Any:
        async with session.post(
            f"{base}/discover/placements",
            json={
                "lookback_hours": 6,
                "apply": True,
                "include_groups": False,
                "max_candidates": 50,
            },
        ) as response:
            assert response.status == 200, await response.text()
            return await response.json()

    asyncio.run(_with_server(facade, run))

    assert facade.calls == [
        {
            "lookback_hours": 6.0,
            "apply": True,
            "include_groups": False,
            "max_candidates": 50,
        }
    ]


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "0"])
def test_route_rejects_a_bad_lookback(value: str) -> None:
    facade = _StubPlacementFacade()

    async def run(session: aiohttp.ClientSession, base: str) -> Any:
        async with session.post(
            f"{base}/discover/placements",
            data=f'{{"lookback_hours": {value}}}',
            headers={"Content-Type": "application/json"},
        ) as response:
            return response.status, await response.json()

    status, body = asyncio.run(_with_server(facade, run))

    assert status == 400
    assert body["error"] == "bad_request"
    assert facade.calls == []


def test_route_rejects_an_oversized_lookback() -> None:
    facade = _StubPlacementFacade()

    async def run(session: aiohttp.ClientSession, base: str) -> Any:
        async with session.post(
            f"{base}/discover/placements", json={"lookback_hours": 2161}
        ) as response:
            return response.status

    assert asyncio.run(_with_server(facade, run)) == 400


def test_route_rejects_a_non_boolean_apply() -> None:
    facade = _StubPlacementFacade()

    async def run(session: aiohttp.ClientSession, base: str) -> Any:
        async with session.post(
            f"{base}/discover/placements", json={"apply": "yes"}
        ) as response:
            return response.status

    assert asyncio.run(_with_server(facade, run)) == 400


def test_core_rejects_a_non_finite_lookback(tmp_path: Path) -> None:
    """The command path validates the same bound as the route."""
    core, _, _, _ = _core(tmp_path)

    for hours in (math.inf, math.nan, -1.0, 0.0, 2161.0):
        with pytest.raises(InvalidRequest):
            asyncio.run(core.discover_placements(lookback_hours=hours, apply=False))


def test_create_app_registers_the_route() -> None:
    app = create_app(facade=_StubPlacementFacade())
    resources = {route.resource for route in app.router.routes()}
    paths = {getattr(resource, "_path", None) for resource in resources}
    assert "/api/v1/discover/placements" in paths
