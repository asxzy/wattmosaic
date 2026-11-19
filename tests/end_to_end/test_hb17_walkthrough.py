"""V1: the HB §17 walkthrough end to end through ``EngineCore``'s three loops.

The X07 HB §17 house is replayed record by record through
:class:`~wattmosaic_engine.realtime.EngineHarness`: HA-shaped ``state_changed``
messages and reader samples enter the real fast loop (``on_ha_event`` /
``on_reading``), the event loop's own bundle → ``infer_group`` → publish path
runs after every record, and the steady loop runs on the injected clock's
heartbeat.  No wall clock is read and the simulator's truth frame never crosses
the observation seam (the replay driver asserts it).

After each HB §17 step the test asserts what a user would see:

* the published :class:`InferenceSnapshot` (``core._v3_snapshot()``);
* the ``/matrix`` payload (``core.matrix_payload()``) — after step 6 only the
  ``light × {Main, Subpanel, Circuit A}`` cells changed;
* the HA entity values, computed by the integration's pure modules
  (``custom_components/wattmosaic/util.coerce_v3_snapshot`` and
  ``entity_values.device_values`` / ``global_values``);
* the HB §18 provenance answers (``core.provenance``).

Declared configuration (what a user sets up before step 1, never truth): one
physical device per HA light and the TV, the HA entity links, the
subject → endpoint mapping (lights on Circuit A, the TV on its plug) and the
plug's single-subject scope (DEC-05).  The
fridge has no HA entity and is not declared (HB §17 step 7).

The house gets a 15-minute quiet prelude (``lead_in_minutes``): the P3 detector
trusts its noise estimate only after 64 samples, so a step at sample 30 of a
cold start is never detected.  HB §17 step 1 ("establish the current
measurement state") is exactly that prelude.

Every step, including the step 5 identifiability history and the step 9 new
mapping candidate, is reached through the live runtime (no xfail).
"""

from __future__ import annotations

import importlib
import importlib.util
import math
import sys
import types
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.api.facade import NotFound
from wattmosaic_engine.contracts import (
    EventGroupStatus,
    FixedSpec,
    LevelStat,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PowerModelVersion,
)
from wattmosaic_engine.contracts.entities import PhysicalDevice
from wattmosaic_engine.core.runtime import EvidenceHistory
from wattmosaic_engine.realtime import EngineHarness

from tools.simulator import build_hb17
from tools.simulator.frames import find_ground_truth_keys
from tools.simulator.hb17 import BASELINE_W, FRIDGE_ON_W, LIGHT_ON_W, TV_STANDBY_W

_PACKAGE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
_PACKAGE_NAME = "wm_under_test"


def _integration_package() -> types.ModuleType:
    """The HA integration's package handle, without running its ``__init__``.

    ``__init__`` imports Home Assistant; the H1 pure modules (``util``,
    ``entity_values``) do not.  Same loader as ``tests/integration/test_ha_integration.py``.
    """
    if _PACKAGE_NAME in sys.modules:
        return sys.modules[_PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME, _PACKAGE_DIR / "__init__.py", submodule_search_locations=[str(_PACKAGE_DIR)]
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(_PACKAGE_DIR)]  # type: ignore[attr-defined]
    sys.modules[_PACKAGE_NAME] = module
    return module


_integration_package()
util = importlib.import_module(f"{_PACKAGE_NAME}.util")
entity_values = importlib.import_module(f"{_PACKAGE_NAME}.entity_values")

SEED = 17
LEAD_IN_MIN = 15.0
L1, L2 = "light.living_room_1", "light.living_room_2"
LIGHTS = (L1, L2)
NESTED = ("ct_a", "ct_sub", "ct_main")  # Circuit A ⊂ Subpanel ⊂ Main
FRIDGE_PATH = ("ct_b", "ct_sub", "ct_main")
#: Declared up front by the user (HB §17 preamble), not inferred from truth.
DEVICES = {L1: "ct_a", L2: "ct_a", "tv": "ct_plug"}
ENTITY_LINKS = ((L1, L1), (L2, L2), ("switch.tv_plug", "tv"))
#: Step deltas are graded at the CT noise floor (design §3 σ_min = 1 W plus the
#: simulator's sub-watt perturbation): a detected step within 2 W of the
#: scripted physics.
STEP_TOL_W = 2.0
STALE_AFTER_S = 3600.0


def _configure(start):
    def configure(core: Any) -> None:
        at = start - timedelta(days=1)
        for device_id in (*LIGHTS, "tv"):
            core.db.repos.entities.upsert_device(
                PhysicalDevice(device_id=device_id, name=device_id, origin="user", created_at=at)
            )
        # The plug is a smart plug that measures the TV alone (DEC-05: a
        # single-subject scope is declared by the endpoint, never inferred).
        core.db.repos.entities.upsert_endpoint(
            {"endpoint_id": "ct_plug", "kind": "smart_plug", "entity_id": "sensor.tv_plug_power",
             "scope": "single_subject", "scope_status": "confirmed", "scope_subject_id": "tv",
             "enabled": True, "created_at": at}
        )
        for entity_id, target in ENTITY_LINKS:
            core.db.repos.entities.add_entity_link(
                {
                    "entity_id": entity_id,
                    "target_kind": "device",
                    "target_id": target,
                    "role": "state",
                    "valid_from": at,
                    "declared_by": "user",
                }
            )
        _declare_tv_model(core, at)

    return configure


def _declare_tv_model(core: Any, at: datetime) -> None:
    """The TV's active power model: a FIXED model with a STANDBY level.

    A published level comes only from the subject's power model (Z3a/A1), so
    the step-8 STANDBY level needs the TV to have one -- as a model learned from
    earlier plug readings would.  Its spread covers the plug's sampling noise.
    """
    registry = core.model_registry_v3
    version = registry.propose(
        PowerModelVersion(
            version_id="pm-tv-1",
            subject_kind="device",
            subject_id="tv",
            number=1,
            model_class=ModelClass.FIXED,
            basis=ModelBasis.ABSOLUTE_ANCHOR,
            spec=FixedSpec(
                levels={PowerLevelV3.STANDBY: LevelStat(median_w=TV_STANDBY_W, mad_w=0.5, n=20)}
            ),
            trainer_version="test-declared",
            created_at=at,
        )
    )
    registry.activate(version.version_id)
    registry.refresh()


def _cells(core: Any) -> dict[tuple[str, str], dict[str, Any]]:
    payload = core.matrix_payload().model_dump(mode="json")
    return {(cell["row_id"], cell["reader_id"]): cell for cell in payload["cells"]}


def _ha(snapshot: Any) -> dict[str, Any]:
    raw = snapshot.model_dump(mode="json") if snapshot is not None else None
    return util.coerce_v3_snapshot(raw)


def _device_entity(coerced: Mapping[str, Any], device_id: str, now) -> dict[str, Any]:
    return entity_values.device_values(
        coerced["results_by_id"].get(device_id),
        engine_available=True,
        now=now,
        stale_after=STALE_AFTER_S,
    )


class _Walk:
    """Everything captured while replaying the co-switched house step by step."""

    def __init__(self) -> None:
        self.house = build_hb17(SEED, minutes=12, variant="co_switched", lead_in_minutes=LEAD_IN_MIN)
        self.observation = self.house.observation_view()
        for record in self.observation:
            assert not find_ground_truth_keys(record), "observation leaks truth"
        self.harness = EngineHarness.start(
            self.observation,
            start_time=self.house.start_time,
            devices=DEVICES,
            configure=_configure(self.house.start_time),
        )
        self.core = self.harness.core
        at = {step: self.house.step_at(step).at_s for step in (3, 7, 8)}

        def window(lo: float, hi: float) -> list[dict[str, Any]]:
            return [r for r in self.observation if lo <= float(r["t_s"]) < hi]

        # Step 1/2: the quiet prelude.
        self.harness.run(window(0.0, at[3]))
        self.prelude_results = list(self.harness.results)
        self.prelude_snapshot = self.harness.snapshot
        try:
            self.core.snapshot_v3()
            self.prelude_snapshot_route = "published"
        except NotFound:
            self.prelude_snapshot_route = "404"
        self.prelude_now = self.harness.now
        self.prelude_cells = _cells(self.core)
        # The last sample each reader delivered whose own timestamp is not in
        # the future of the injected clock (Circuit A carries a +30 s skew).
        self.prelude_last_samples = {
            reader: float(sample["value_w"])
            for record in window(0.0, at[3])
            for reader, sample in record["reader_samples"].items()
            if datetime.fromisoformat(sample["state_timestamp"]) <= self.harness.now
        }
        self.prelude_held = {
            reader: self.core.reader_state.held(reader, self.harness.now)
            for reader in self.core.reader_state.readers()
        }
        # Steps 3-6: the lights event, up to the fridge.
        self.harness.run(window(at[3], at[7]))
        self.lights_results = [
            r
            for r in self.harness.results
            if getattr(r, "event_group_id", None) is not None
            and any(e.subject_id in LIGHTS for e in r.device_estimates)
        ]
        self.lights_snapshot = self.harness.snapshot
        self.lights_now = self.harness.now
        self.lights_cells = _cells(self.core)
        self.lights_provenance = {sid: self.core.provenance("device", sid) for sid in LIGHTS}
        # Step 7: the fridge compressor, up to the TV standby report.
        before = len(self.harness.results)
        self.harness.run(window(at[7], at[8]))
        self.fridge_results = [
            r
            for r in self.harness.results[before:]
            if getattr(r, "event_group_id", None) is not None
        ]
        self.fridge_snapshot = self.harness.snapshot
        self.fridge_now = self.harness.now
        self.fridge_cells = _cells(self.core)
        # Step 8: the TV plug's standby report and the rest of the stream.
        self.harness.run(window(at[8], math.inf))
        self.final_snapshot = self.harness.snapshot
        self.final_now = self.harness.now

    def group(self, result: Any) -> Any:
        return self.core.grouping_engine.current(result.event_group_id)


@pytest.fixture(scope="module")
def walk():
    state = _Walk()
    yield state
    state.harness.close()


# --------------------------------------------------------------------------- #
# Step 1 — establish the current measurement state.
# --------------------------------------------------------------------------- #


def test_step1_measurement_state_is_established_without_fabrication(walk: _Walk) -> None:
    held = walk.prelude_held
    assert set(held) == {"ct_main", "ct_sub", "ct_a", "ct_b", "ct_c", "ct_plug", "ct_hvac", "ct_blind"}
    for reader, reading in held.items():
        # The zero-order hold is the last delivered sample, normalised as-is.
        assert reading.value is not None, reader
        assert reading.value == pytest.approx(walk.prelude_last_samples[reader]), reader
    # The quiet house: every branch sits at its declared baseline (plus the
    # TV's 3 W standby on Circuit A and its parents).
    for reader, baseline in BASELINE_W.items():
        standby = TV_STANDBY_W if reader in NESTED else 0.0
        assert held[reader].value == pytest.approx(baseline + standby, abs=STEP_TOL_W), reader
    # The plug reads the TV's 3 W standby: a real non-zero value, not a default.
    assert held["ct_plug"].value == pytest.approx(TV_STANDBY_W, abs=1.0)
    # No event has happened.  The only thing published is what is *measured*:
    # the TV through its single-subject plug (steady pass, HB §17 step 8 state).
    assert walk.prelude_results, "the plug reading was never published"
    for result in walk.prelude_results:
        assert getattr(result, "event_group_id", None) is None  # steady, not an event
        assert [e.subject_id for e in result.device_estimates] == ["tv"]
    snapshot = walk.prelude_snapshot
    assert snapshot is not None and walk.prelude_snapshot_route == "published"
    assert [e.subject_id for e in snapshot.device_estimates] == ["tv"]
    tv = snapshot.device_estimates[0]
    assert str(tv.status) == "measured"
    assert tv.source_reader_ids == ["ct_plug"]
    assert tv.point_w == pytest.approx(held["ct_plug"].value, abs=1.0)
    assert snapshot.group_estimates == []
    # Matrix: only the TV × plug cell exists; no light cell before the event.
    assert set(walk.prelude_cells) == {("tv", "ct_plug")}
    # HA: the TV shows its measured standby; the lights have no entity value.
    # The house total is the topology root's measured reading (Z3a/A3: the
    # main reading, not a residual and not chosen by name); the unknown total
    # stays "not computed" (None), never 0 W.
    coerced = _ha(snapshot)
    assert set(coerced["results_by_id"]) == {"tv"}
    tv_values = _device_entity(coerced, "tv", walk.prelude_now)
    assert tv_values["power"] == pytest.approx(TV_STANDBY_W, abs=1.0)
    assert tv_values["estimate_status"] == "measured"
    values = entity_values.global_values(
        coerced, {"status": "ok"}, engine_available=True, now=walk.prelude_now,
        stale_after=STALE_AFTER_S,
    )
    main = {m.reader_id: m for m in snapshot.reader_measurements}["ct_main"]
    assert main.active_power_w is not None
    assert values["total_power"] == pytest.approx(main.active_power_w)
    assert snapshot.reconciliation.main_w == pytest.approx(main.active_power_w)
    assert values["unknown_power"] is None


# --------------------------------------------------------------------------- #
# Step 2 — maintain topology.
# --------------------------------------------------------------------------- #


def test_step2_validated_topology_drives_the_operator_and_matrix_columns(walk: _Walk) -> None:
    versions = walk.core.version_service
    triple = versions.current()
    edges = {
        (e.source_endpoint_id, e.target_endpoint_id)
        for e in versions.topology_view(triple.topology_version_id).edges()
    }
    assert edges == {
        ("ct_main", "ct_sub"),
        ("ct_sub", "ct_a"),
        ("ct_sub", "ct_b"),
        ("ct_main", "ct_c"),
    }
    mapping = versions.mapping_view(triple.mapping_version_id)
    assert set(mapping.subjects_on("ct_a")) == {("device", L1), ("device", L2)}
    assert mapping.subjects_on("ct_plug") == (("device", "tv"),)
    # Matrix columns are the multi-level reader organisation (HB §9.1).
    payload = walk.core.matrix_payload().model_dump(mode="json")

    def readers(node: Mapping[str, Any], level: str | None = None) -> list[tuple[str, str]]:
        out = [(node["reader_id"], node["level"])] if node.get("reader_id") else []
        for child in node.get("children") or ():
            out.extend(readers(child))
        return out

    flat = [pair for column in payload["columns"] for pair in readers(column)]
    assert {reader for reader, _level in flat} >= set(NESTED) | {"ct_b", "ct_c"}
    assert len(flat) == len({reader for reader, _level in flat}), "a reader column is duplicated"
    assert payload["versions"]["topology_version_id"] == triple.topology_version_id


# --------------------------------------------------------------------------- #
# Step 3 — detect a load event.
# --------------------------------------------------------------------------- #


def test_step3_one_group_holds_both_ha_changes_and_the_three_nested_steps(walk: _Walk) -> None:
    assert walk.lights_results, "the lights event published no result"
    group = walk.group(walk.lights_results[-1])
    deltas = {d.reader_id: d.delta_w for d in group.reader_deltas}
    assert set(deltas) == set(NESTED)
    for reader in NESTED:
        assert deltas[reader] == pytest.approx(LIGHT_ON_W, abs=STEP_TOL_W), reader
    ha_events = walk.core.grouping_engine.ha_events(group.group_id)
    assert {e.entity_id for e in ha_events} >= set(LIGHTS)
    kinds = [str(getattr(s.kind, "value", s.kind)) for s in group.sub_events]
    assert kinds.count("reader_step") == 3
    assert kinds.count("ha_state") >= 2
    # Each sub-event keeps its own timestamp (Circuit A carries a +30 s skew).
    stamps = {s.observed_at for s in group.sub_events if s.kind == "reader_step"}
    assert len(stamps) >= 2
    # The three nested observations are ONE event group, not three.
    lights_groups = {r.event_group_id for r in walk.lights_results}
    assert lights_groups == {group.group_id}


# --------------------------------------------------------------------------- #
# Step 4 — dynamic matching.
# --------------------------------------------------------------------------- #


def test_step4_both_lights_are_candidates_and_one_load_is_not_counted_thrice(walk: _Walk) -> None:
    result = walk.lights_results[-1]
    candidates = {c.subject_id: {str(s) for s in c.sources} for c in result.candidates}
    for light in LIGHTS:
        assert light in candidates, light
        assert "ha_state" in candidates[light]
    # The unknown-load alternative stays available (HB §7.5).
    unknown_scopes = {
        c.subject_id for c in result.candidates if str(c.subject_kind) == "unknown_scope"
    }
    assert unknown_scopes == set(NESTED)
    # The same +45 W seen on three nested readers is one physical load: no
    # unknown scope keeps a share of it and nothing is published three times.
    for unknown in result.unknown_estimates:
        if unknown.delta_w is not None:
            assert abs(unknown.delta_w) < 1.0, unknown
    recon = walk.lights_snapshot.reconciliation
    total = sum(v for v in (recon.identified_w, recon.group_w, recon.unknown_w) if v is not None)
    assert total < LIGHT_ON_W + STEP_TOL_W


# --------------------------------------------------------------------------- #
# Step 5 — joint inference.
# --------------------------------------------------------------------------- #


def test_step5_lights_are_not_split_into_fabricated_individual_values(walk: _Walk) -> None:
    snapshot = walk.lights_snapshot
    estimates = {e.subject_id: e for e in snapshot.device_estimates}
    for light in LIGHTS:
        estimate = estimates[light]
        # One co-switched event cannot separate the lights: no number is
        # invented for either (HB §8.4/§8.5), not 22.5 W, not 45 W, not 0 W.
        assert estimate.point_w is None
        assert str(estimate.status) == "unknown"
        assert str(estimate.identifiability) in {"insufficient_evidence", "non_identifiable"}
        assert estimate.bounds.lower_w == 0.0
        assert set(estimate.source_reader_ids) == set(NESTED)
        assert estimate.versions == snapshot.versions
    # HA: the entity is available with no state, plus the status axes.
    coerced = _ha(snapshot)
    for light in LIGHTS:
        values = _device_entity(coerced, light, walk.lights_now)
        assert values["available"] is True
        assert values["power"] is None
        assert values["estimate_status"] == "unknown"
        assert values["identifiability"] == estimates[light].identifiability
        assert values["lower_w"] == 0.0


def test_step5_confirmed_lights_event_becomes_identifiability_history(walk: _Walk) -> None:
    group = walk.group(walk.lights_results[-1])
    assert group.status == EventGroupStatus.CONFIRMED
    assert group.closed_at is not None
    rows = EvidenceHistory(walk.core.db.repos.evidence).events_touching(
        list(LIGHTS), walk.house.start_time
    )
    assert any(row.group_id == group.group_id for row in rows)


# --------------------------------------------------------------------------- #
# Step 6 — matrix update.
# --------------------------------------------------------------------------- #


def test_step6_only_the_light_cells_change(walk: _Walk) -> None:
    before, after = walk.prelude_cells, walk.lights_cells
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    expected = {(light, reader) for light in LIGHTS for reader in NESTED}
    assert changed == expected
    for light in LIGHTS:
        assert after[(light, "ct_a")]["mapping"]["observability"] == "direct"
        for parent in ("ct_sub", "ct_main"):
            assert after[(light, parent)]["mapping"]["observability"] == "expected"
        for reader in NESTED:
            power = after[(light, reader)]["power"]
            # The contribution is not invented per column (HB §9.7): no cell
            # shows a watt value for a light the evidence cannot separate.
            assert power["estimated_power_w"] is None
            assert power["measurement_status"] == "unknown"


def test_step6_provenance_answers_for_a_light(walk: _Walk) -> None:
    snapshot = walk.lights_snapshot
    for light in LIGHTS:
        answer = walk.lights_provenance[light]
        assert {m.reader_id for m in answer.reader_measurements} == set(NESTED)
        for measurement in answer.reader_measurements:
            assert measurement.active_power_w is not None and measurement.active_power_w > 0
        assert answer.model_version_id is None  # no power model was used
        assert answer.topology_version_id == snapshot.versions.topology_version_id
        assert answer.mapping_version_id == snapshot.versions.mapping_version_id
        assert str(answer.status) == "unknown"
        assert str(answer.identifiability) != "identifiable"
        assert answer.lower_w == 0.0 and answer.upper_w is None
        assert answer.bounds_basis == "non_negativity"
        assert answer.point_w is None
        # Event associations: exactly the HA changes of its event group, resolved
        # to the persisted HA events (entity, instant, transition); nothing the
        # estimate cites is left unresolved.
        group = walk.group(walk.lights_results[-1])
        group_events = {
            e.event_id: e for e in walk.core.grouping_engine.ha_events(group.group_id)
        }
        assert {e.entity_id for e in group_events.values()} >= set(LIGHTS)
        assert answer.unresolved_refs == ()
        associations = {a.event_id: a for a in answer.event_associations}
        assert set(associations) == set(group_events)
        for event_id, association in associations.items():
            assert association.entity_id == group_events[event_id].entity_id
            assert association.observed_at == group_events[event_id].observed_at
            assert association.to_state == group_events[event_id].to_state
        assert answer.updated_at == walk.lights_results[-1].computed_at


# --------------------------------------------------------------------------- #
# Step 7 — unknown refrigerator activity.
# --------------------------------------------------------------------------- #


def test_step7_fridge_step_without_ha_is_unknown_load_counted_once(walk: _Walk) -> None:
    assert walk.fridge_results, "the fridge step published no result"
    result = walk.fridge_results[-1]
    group = walk.group(result)
    deltas = {d.reader_id: d.delta_w for d in group.reader_deltas}
    assert set(deltas) == set(FRIDGE_PATH)
    for reader in FRIDGE_PATH:
        assert deltas[reader] == pytest.approx(FRIDGE_ON_W, abs=STEP_TOL_W), reader
    # No HA evidence did not prevent detection; no device is invented.
    assert walk.core.grouping_engine.ha_events(group.group_id) == ()
    assert result.device_estimates == []
    assert all(str(c.subject_kind) == "unknown_scope" for c in result.candidates)
    unknown = {u.scope_reader_id: u.delta_w for u in result.unknown_estimates}
    assert unknown["ct_b"] == pytest.approx(FRIDGE_ON_W, abs=STEP_TOL_W)
    for parent in ("ct_sub", "ct_main"):
        assert abs(unknown.get(parent) or 0.0) < 1.0, parent
    # Snapshot and HA: one 150 W unknown, not 450 W over three nested readers.
    recon = walk.fridge_snapshot.reconciliation
    assert recon.unknown_w == pytest.approx(FRIDGE_ON_W, abs=STEP_TOL_W)
    values = entity_values.global_values(
        _ha(walk.fridge_snapshot), {"status": "ok"}, engine_available=True,
        now=walk.fridge_now, stale_after=STALE_AFTER_S,
    )
    assert values["unknown_power"] == pytest.approx(FRIDGE_ON_W, abs=STEP_TOL_W)
    # No matrix cell changed: the fridge is not a mapped device.
    assert walk.fridge_cells == walk.lights_cells


# --------------------------------------------------------------------------- #
# Step 8 — Smart Plug standby.
# --------------------------------------------------------------------------- #


def test_step8_tv_standby_is_measured_from_its_plug_never_zero(walk: _Walk) -> None:
    snapshot = walk.final_snapshot
    plug = {m.reader_id: m for m in snapshot.reader_measurements}["ct_plug"]
    assert plug.active_power_w == pytest.approx(TV_STANDBY_W, abs=1.0)
    # The TV never steps, yet its standby is published: MEASURED through the
    # single-subject plug endpoint (HB §17 step 8, HB §8.5), not 0 W, not None.
    tv = {e.subject_id: e for e in snapshot.device_estimates}["tv"]
    assert str(tv.status) == "measured"
    assert str(tv.identifiability) == "identifiable"
    assert tv.source_reader_ids == ["ct_plug"]
    assert tv.point_w == pytest.approx(TV_STANDBY_W, abs=1.0)
    assert tv.bounds.lower_w == tv.bounds.upper_w == tv.point_w
    assert str(tv.level) == "standby" or getattr(tv.level, "value", None) == "standby"
    values = _device_entity(_ha(snapshot), "tv", walk.final_now)
    assert values["available"] is True
    assert values["power"] == pytest.approx(TV_STANDBY_W, abs=1.0)
    assert values["estimate_status"] == "measured"
    answer = walk.core.provenance("device", "tv")
    assert [m.reader_id for m in answer.reader_measurements] == ["ct_plug"]
    assert answer.point_w == pytest.approx(TV_STANDBY_W, abs=1.0)


# --------------------------------------------------------------------------- #
# Steps 9-10 — the plug moves; reconciliation.
# --------------------------------------------------------------------------- #


class _Moved:
    """The relocated-TV variant: six post-move TV cycles feed the T7 detector.

    Default ``from_hb17`` mapping (the TV on Circuit A) plus the one declared HA
    link that says which device the plug's switch controls.
    """

    def __init__(self) -> None:
        self.house = build_hb17(SEED, variant="relocated_tv", minutes=60, lead_in_minutes=LEAD_IN_MIN)
        observation = self.house.observation_view()
        start = self.house.start_time

        def configure(core: Any) -> None:
            core.db.repos.entities.add_entity_link(
                {"entity_id": "switch.tv_plug", "target_kind": "device", "target_id": "tv",
                 "role": "state", "valid_from": start - timedelta(days=1), "declared_by": "user"}
            )
            # The plug is a declared single-subject endpoint for the TV (DEC-05);
            # only a declared endpoint feeds the relocation detector (Z3a/A2).
            core.db.repos.entities.upsert_endpoint(
                {"endpoint_id": "ct_plug", "kind": "smart_plug", "entity_id": "sensor.tv_plug_power",
                 "scope": "single_subject", "scope_status": "confirmed", "scope_subject_id": "tv",
                 "enabled": True, "created_at": start - timedelta(days=1)}
            )

        self.harness = EngineHarness.start(observation, start_time=start, configure=configure)
        self.core = self.harness.core
        self.initial = self.core.version_service.current()
        move = self.house.step_at(9).at_s
        self.harness.run([r for r in observation if float(r["t_s"]) < move])
        self.groups_before_move = {
            (g.group_id, g.revision) for g in self.core.db.repos.evidence.groups()
        }
        self.harness.run([r for r in observation if float(r["t_s"]) >= move])
        self.harness.settle()


@pytest.fixture(scope="module")
def moved():
    state = _Moved()
    yield state
    state.harness.close()


def test_step9_plug_move_is_a_confirmed_inferred_configuration_event(moved: _Moved) -> None:
    events = [
        e
        for e in moved.core.v3_configuration_events()
        if str(getattr(e.kind, "value", e.kind)) == "smart_plug_relocation"
    ]
    confirmed = [e for e in events if str(getattr(e.status, "value", e.status)) == "confirmed"]
    assert confirmed
    assert all(str(getattr(e.detection, "value", e.detection)) == "inferred" for e in confirmed)
    assert any(
        "association:ct_a->ct_c" in ref.id for e in confirmed for ref in e.evidence_refs
    )


def test_step10_new_versions_keep_models_mapping_and_history(moved: _Moved) -> None:
    core = moved.core
    versions = core.version_service
    now = versions.current()
    assert now.topology_version_id != moved.initial.topology_version_id
    assert now.mapping_version_id != moved.initial.mapping_version_id
    # Rule 20 / AT-15: the TV's power model is not relearned by a plug move.
    assert now.model_set_id == moved.initial.model_set_id
    # The prior versions stay readable (HB §14).
    old_edges = versions.topology_view(moved.initial.topology_version_id).edges()
    assert old_edges
    # Promoting the relocation does not drop unrelated mapping edges.
    new_map = versions.mapping_view(now.mapping_version_id)
    assert new_map.most_specific_reader(("device", "lights")) == "ct_a"
    # The fridge has no HA entity (HB §17): never declared, before or after.
    assert new_map.most_specific_reader(("device", "fridge")) is None
    assert new_map.subjects_on("ct_b") == ()
    # Historical event evidence is preserved, not rewritten.
    after = {(g.group_id, g.revision) for g in core.db.repos.evidence.groups()}
    assert moved.groups_before_move and moved.groups_before_move <= after
    # Current estimates are re-evaluated: L7 ran for the promoted versions.
    runs = core.reconciler.runs()
    assert any(run.versions.mapping_version_id == now.mapping_version_id for run in runs)
    # The matrix payload reads the new mapping version.
    payload = core.matrix_payload().model_dump(mode="json")
    assert payload["mapping_version_id"] == now.mapping_version_id


def test_step9_relocation_creates_a_new_mapping_candidate(moved: _Moved) -> None:
    versions = moved.core.version_service
    now = versions.current()
    new_map = versions.mapping_view(now.mapping_version_id)
    old_map = versions.mapping_view(moved.initial.mapping_version_id)
    assert old_map.most_specific_reader(("device", "tv")) == "ct_a"
    # The promoted mapping moves the TV off Circuit A onto Circuit C ...
    assert new_map.most_specific_reader(("device", "tv")) == "ct_c"
    assert ("device", "tv") not in new_map.subjects_on("ct_a")
    edge = {(e.subject_id, e.endpoint_id): e for e in new_map.edges()}[("tv", "ct_c")]
    # ... as a new candidate the evidence proposed, not a user declaration.
    assert str(getattr(edge.lifecycle, "value", edge.lifecycle)) == "provisional"
    assert edge.declared_by == "inference"
    # The lights stay where they were.
    assert new_map.most_specific_reader(("device", "lights")) == "ct_a"
