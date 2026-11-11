"""Replaying the stored topology declaration onto the engine.

**Why this file exists**

The user's confirmed CT nesting/aliasing lives only in the config entry
(``CONF_CT_ASSIGNMENTS``).  The engine keeps its own derived copy, and until now
the only writer was the config-flow wizard's confirm step -- so a database reset
or a discovery-driven bootstrap replaced the confirmed topology with a
parentless candidate while the entry still said "confirmed at v5".  The engine
then summed physically nested mains and reported roughly double the real draw.

These tests pin the *replay* contract:

* null parents/aliases in the engine -> exactly one ``PUT`` plus ``confirm``;
* in-sync versions -> no write at all;
* no declaration -> no write (never fabricate);
* engine error -> setup survives, the error is auditable, the next poll retries;
* a newer confirmed engine topology -> never downgraded;
* two consecutive refreshes -> still exactly one push.

Like ``test_ha_integration.py`` this module never imports Home Assistant: the
decision logic is pure (``flow_logic``) and the engine is a recording fake, so
no real engine is ever contacted.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

PACKAGE_DIR = (
    Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
)
PACKAGE_NAME = "wm_under_test"


def _load_package() -> types.ModuleType:
    """Create a package handle for the integration without running ``__init__``."""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # Deliberately do NOT exec the spec: that would import homeassistant.
    module.__path__ = [str(PACKAGE_DIR)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = module
    return module


_load_package()

const = importlib.import_module(f"{PACKAGE_NAME}.const")
flow = importlib.import_module(f"{PACKAGE_NAME}.flow_logic")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Recording fake engine client
# --------------------------------------------------------------------------- #


class FakeEngine:
    """A recording stand-in for ``EngineClient``'s topology methods.

    Only the three methods the replay uses are implemented.  No network, no
    aiohttp, no real Home Assistant.
    """

    def __init__(
        self,
        topology: dict[str, Any] | None = None,
        *,
        put_error: BaseException | None = None,
        confirm_error: BaseException | None = None,
        topology_error: BaseException | None = None,
    ) -> None:
        self._topology = dict(topology or {})
        self.put_error = put_error
        self.confirm_error = confirm_error
        self.topology_error = topology_error
        self.put_calls: list[dict[str, Any]] = []
        self.confirm_calls: list[Any] = []
        self.topology_calls = 0

    async def topology(self) -> dict[str, Any]:
        self.topology_calls += 1
        if self.topology_error is not None:
            raise self.topology_error
        return dict(self._topology)

    async def put_topology(self, topology: dict[str, Any]) -> dict[str, Any]:
        self.put_calls.append(dict(topology))
        if self.put_error is not None:
            raise self.put_error
        stored = dict(topology)
        stored["version"] = int(self._topology.get("version") or 0) + 1
        self._topology = stored
        return dict(stored)

    async def confirm_topology(self, version: Any = None) -> dict[str, Any]:
        self.confirm_calls.append(version)
        if self.confirm_error is not None:
            raise self.confirm_error
        self._topology["status"] = "confirmed"
        for channel in self._topology.get("channels") or []:
            channel["topology_status"] = "confirmed"
        return dict(self._topology)


# --------------------------------------------------------------------------- #
# Fixtures: a panel with reassigned channels the way production had them
# --------------------------------------------------------------------------- #


def _engine_topology(*, version: int = 1, status: str = "candidate") -> dict[str, Any]:
    """Six physically nested mains, all parentless -- the measured bug."""
    return {
        "version": version,
        "status": status,
        "channels": [
            {"ct_id": "main_load_manager_l1", "entity_id": "sensor.mm_l1",
             "name": "Main L1", "ct_type": "main", "phase": "l1",
             "polarity": "normal", "parent_ct_id": None, "alias_of_ct_id": None},
            {"ct_id": "main_load_manager_l2", "entity_id": "sensor.mm_l2",
             "name": "Main L2", "ct_type": "main", "phase": "l2",
             "polarity": "normal", "parent_ct_id": None, "alias_of_ct_id": None},
            {"ct_id": "refoss_main_panel_l1", "entity_id": "sensor.pm_l1",
             "name": "Panel L1", "ct_type": "main", "phase": "l1",
             "polarity": "normal", "parent_ct_id": None, "alias_of_ct_id": None},
            {"ct_id": "refoss_main_panel_l2", "entity_id": "sensor.pm_l2",
             "name": "Panel L2", "ct_type": "main", "phase": "l2",
             "polarity": "normal", "parent_ct_id": None, "alias_of_ct_id": None},
            {"ct_id": "refoss_sub_panel_l1", "entity_id": "sensor.sp_l1",
             "name": "Sub L1", "ct_type": "main", "phase": "l1",
             "polarity": "normal", "parent_ct_id": None, "alias_of_ct_id": None},
            {"ct_id": "refoss_sub_panel_l2", "entity_id": "sensor.sp_l2",
             "name": "Sub L2", "ct_type": "main", "phase": "l2",
             "polarity": "normal", "parent_ct_id": None, "alias_of_ct_id": None},
        ],
        "devices": [{"device_id": "dummy", "name": "Dummy"}],
    }


def _assignments() -> dict[str, dict[str, Any]]:
    """The declaration from the production config entry."""
    return {
        "main_load_manager_l1": {"role": "main", "phase": "l1"},
        "main_load_manager_l2": {"role": "main", "phase": "l2"},
        "refoss_main_panel_l1": {
            "role": "main", "phase": "l1",
            "alias_of_ct_id": "main_load_manager_l1",
        },
        "refoss_main_panel_l2": {
            "role": "main", "phase": "l2",
            "alias_of_ct_id": "main_load_manager_l2",
        },
        "refoss_sub_panel_l1": {
            "role": "main", "phase": "l1",
            "parent_ct_id": "main_load_manager_l1",
        },
        "refoss_sub_panel_l2": {
            "role": "main", "phase": "l2",
            "parent_ct_id": "main_load_manager_l2",
        },
    }


def _by_id(topology: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {channel["ct_id"]: channel for channel in topology["channels"]}


# --------------------------------------------------------------------------- #
# The push itself
# --------------------------------------------------------------------------- #


class TestPushOnSetup:
    def test_null_parents_are_replayed_then_confirmed(self) -> None:
        engine = FakeEngine(_engine_topology())
        state = run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=1,
            )
        )

        assert len(engine.put_calls) == 1, "exactly one PUT"
        assert len(engine.confirm_calls) == 1, "confirmation follows the PUT"
        # The confirm names the revision the PUT produced, never a stale one.
        assert engine.confirm_calls[0] == 2

        pushed = _by_id(engine.put_calls[0])
        assert pushed["refoss_main_panel_l1"]["alias_of_ct_id"] == "main_load_manager_l1"
        assert pushed["refoss_main_panel_l2"]["alias_of_ct_id"] == "main_load_manager_l2"
        assert pushed["refoss_sub_panel_l1"]["parent_ct_id"] == "main_load_manager_l1"
        assert pushed["refoss_sub_panel_l2"]["parent_ct_id"] == "main_load_manager_l2"
        assert pushed["main_load_manager_l1"]["parent_ct_id"] is None

        assert state.pushed is True
        assert state.error is None
        assert state.attempts == 1
        assert state.declared_version == 5
        assert state.reason == "declaration_missing"

    def test_matching_versions_do_not_push(self) -> None:
        topology = _engine_topology(version=5, status="confirmed")
        # Build the *already correct* engine view through the same merge rule,
        # so this test cannot pass by accident on an unrelated mismatch.
        correct = flow.build_topology_update(
            topology,
            {ct_id: {**raw, "role": "main"} for ct_id, raw in _assignments().items()},
            confirmed=True,
        )
        correct["version"] = 5
        engine = FakeEngine(correct)

        state = run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=5,
            )
        )

        assert engine.put_calls == []
        assert engine.confirm_calls == []
        assert state.attempts == 0
        assert state.pushed is False
        assert state.reason == "in_sync"

    def test_no_assignments_never_pushes(self) -> None:
        engine = FakeEngine(_engine_topology())
        state = run(
            flow.async_replay_topology(
                engine, {}, confirmed=False, stored_version=None, observed_version=1
            )
        )
        assert engine.put_calls == []
        assert engine.confirm_calls == []
        assert state.reason == "no_assignments"
        # Not even a read: a fresh install must not generate engine traffic.
        assert engine.topology_calls == 0

        state = run(
            flow.async_replay_topology(
                engine, None, confirmed=True, stored_version=5, observed_version=1
            )
        )
        assert engine.put_calls == []
        assert state.reason == "no_assignments"

    def test_engine_error_is_recorded_and_the_next_refresh_retries(self) -> None:
        error = api_error("engine unreachable: TimeoutError")
        engine = FakeEngine(_engine_topology(), put_error=error)
        state = flow.TopologyPushState()

        run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=1,
                state=state,
            )
        )
        assert state.pushed is False
        assert state.error is not None and "unreachable" in state.error
        assert state.attempts == 1

        # Engine recovers; the next poll retries and succeeds.
        engine.put_error = None
        run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=1,
                state=state,
            )
        )
        assert state.pushed is True
        assert state.error is None
        assert state.attempts == 2
        assert len(engine.put_calls) == 2

    def test_confirm_failure_is_retried(self) -> None:
        engine = FakeEngine(
            _engine_topology(), confirm_error=api_error("confirm rejected")
        )
        state = run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=1,
            )
        )
        assert state.pushed is False
        assert state.error is not None and "confirm rejected" in state.error
        engine.confirm_error = None
        run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=1,
                state=state,
            )
        )
        assert state.pushed is True
        assert state.error is None

    def test_a_newer_confirmed_topology_is_never_downgraded(self) -> None:
        topology = _engine_topology(version=7, status="confirmed")
        correct = flow.build_topology_update(
            topology,
            {ct_id: {**raw, "role": "main"} for ct_id, raw in _assignments().items()},
            confirmed=True,
        )
        correct["version"] = 7
        engine = FakeEngine(correct)

        state = run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=7,
            )
        )

        assert engine.put_calls == []
        assert engine.confirm_calls == []
        assert state.pushed is False
        assert state.reason == "engine_newer_confirmed"

    def test_a_differing_declaration_on_a_confirmed_engine_stays_confirmed(self) -> None:
        """A rewrite must never demote a confirmed topology to ``candidate``.

        A candidate disables learning until the user runs the wizard again, so
        "the declaration changed" is not a reason to lose the confirmation.
        """
        engine = FakeEngine(_engine_topology(version=5, status="confirmed"))
        state = run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                # The entry says unconfirmed, but the engine already is.
                confirmed=False,
                stored_version=5,
                observed_version=5,
            )
        )
        assert len(engine.put_calls) == 1
        assert engine.put_calls[0]["status"] == "confirmed"
        assert all(
            channel["topology_status"] == "confirmed"
            for channel in engine.put_calls[0]["channels"]
        )
        assert state.pushed is True

    def test_a_plan_records_that_a_confirmed_engine_is_preserved(self) -> None:
        plan = flow.plan_topology_push(
            _engine_topology(version=5, status="confirmed"),
            _assignments(),
            confirmed=False,
            stored_version=5,
        )
        assert plan.push is True
        assert plan.confirmed is True
        assert plan.preserve_confirmed is True

    def test_two_consecutive_refreshes_push_exactly_once(self) -> None:
        engine = FakeEngine(_engine_topology())
        state = flow.TopologyPushState()

        run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=1,
                state=state,
            )
        )
        version_after_push = state.pushed_version

        # Second poll: the snapshot now reports the version the push produced.
        run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=version_after_push,
                state=state,
            )
        )

        assert len(engine.put_calls) == 1
        assert len(engine.confirm_calls) == 1
        assert state.attempts == 1

    def test_the_periodic_recheck_is_read_only_when_still_in_sync(self) -> None:
        """The version-only blind spot (a reset that reuses the version) is covered."""
        engine = FakeEngine(_engine_topology())
        state = flow.TopologyPushState()
        run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=1,
                state=state,
            )
        )
        version = state.pushed_version
        for _ in range(flow.TOPOLOGY_RECHECK_POLLS):
            run(
                flow.async_replay_topology(
                    engine,
                    _assignments(),
                    confirmed=True,
                    stored_version=5,
                    observed_version=version,
                    state=state,
                )
            )
        assert len(engine.put_calls) == 1
        assert state.pushed is True

    def test_a_second_refresh_with_a_stale_snapshot_also_does_not_repush(self) -> None:
        """A stale reconciliation version must not resurrect the push.

        The engine's topology now matches the declaration, so re-checking is
        harmless -- but it must still not write.
        """
        engine = FakeEngine(_engine_topology())
        state = flow.TopologyPushState()
        for _ in range(2):
            run(
                flow.async_replay_topology(
                    engine,
                    _assignments(),
                    confirmed=True,
                    stored_version=5,
                    observed_version=1,
                    state=state,
                )
            )
        assert len(engine.put_calls) == 1
        assert state.attempts == 1

    def test_an_unconfirmed_entry_never_confirms(self) -> None:
        engine = FakeEngine(_engine_topology())
        state = run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=False,
                stored_version=None,
                observed_version=1,
            )
        )
        assert len(engine.put_calls) == 1
        assert engine.confirm_calls == []
        assert engine.put_calls[0]["status"] == "candidate"
        assert state.pushed is True

    def test_topology_read_failure_is_not_fatal(self) -> None:
        engine = FakeEngine(
            _engine_topology(), topology_error=api_error("engine unreachable")
        )
        state = run(
            flow.async_replay_topology(
                engine,
                _assignments(),
                confirmed=True,
                stored_version=5,
                observed_version=1,
            )
        )
        assert engine.put_calls == []
        assert state.pushed is False
        assert state.error is not None and "unreachable" in state.error

    def test_an_engine_that_ignores_the_declaration_is_not_rewritten_forever(self) -> None:
        """A PUT that the engine accepts but does not reflect must stop retrying.

        Otherwise a schema that silently drops ``parent_ct_id`` would get one
        write per poll forever, and the real fault would never be visible.
        """
        engine = FakeEngine(_engine_topology())
        engine.put_topology = _ignoring_put(engine)  # type: ignore[method-assign]
        state = flow.TopologyPushState()

        for _ in range(6):
            run(
                flow.async_replay_topology(
                    engine,
                    _assignments(),
                    confirmed=True,
                    stored_version=5,
                    observed_version=1,
                    state=state,
                )
            )

        assert len(engine.put_calls) == flow.MAX_IDENTICAL_PUSHES
        assert state.reason == "engine_ignored_declaration"
        assert state.error is not None and "did not reflect" in state.error


def api_error(message: str) -> Exception:
    """A client-shaped exception whose ``str()`` is what diagnostics show."""
    return RuntimeError(message)


def _ignoring_put(engine: FakeEngine) -> Any:
    """A ``PUT`` that reports success but stores the body unchanged.

    Models an engine whose schema silently drops the nesting fields: the
    declaration never lands, so the planner keeps seeing a mismatch.
    """

    async def put_topology(topology: dict[str, Any]) -> dict[str, Any]:
        engine.put_calls.append(dict(topology))
        return {"version": int(engine._topology.get("version") or 0) + 1}

    return put_topology


# --------------------------------------------------------------------------- #
# Planning rules (pure, no client)
# --------------------------------------------------------------------------- #


class TestPlan:
    def test_engine_without_channels_is_never_written(self) -> None:
        plan = flow.plan_topology_push(
            {"version": 0, "channels": []}, _assignments(), stored_version=5
        )
        assert plan.push is False
        assert plan.reason == "engine_topology_empty"

    def test_no_overlapping_ct_ids_is_not_a_push(self) -> None:
        """A PUT cannot create channels, so an unknown id is not a reason to write."""
        plan = flow.plan_topology_push(
            _engine_topology(), {"ct_absent": {"role": "main", "phase": "l1"}},
            stored_version=5,
        )
        assert plan.push is False
        assert plan.reason == "no_matching_channels"

    def test_engine_behind_the_stored_version_repushes(self) -> None:
        plan = flow.plan_topology_push(
            _engine_topology(version=1), _assignments(), stored_version=5
        )
        assert plan.push is True
        assert plan.reason == "declaration_missing"

    def test_confirmed_entry_on_a_candidate_engine_reconfirms(self) -> None:
        topology = _engine_topology(version=5, status="candidate")
        merged = flow.build_topology_update(
            topology,
            {ct_id: {**raw, "role": "main"} for ct_id, raw in _assignments().items()},
            confirmed=True,
        )
        merged["version"] = 5
        merged["status"] = "candidate"
        for channel in merged["channels"]:
            channel["topology_status"] = "candidate"
        plan = flow.plan_topology_push(merged, _assignments(), confirmed=True, stored_version=5)
        assert plan.push is True
        assert plan.reason == "confirmation_missing"


# --------------------------------------------------------------------------- #
# Wiring: the HA-facing modules call the tested helper
# --------------------------------------------------------------------------- #


class TestWiring:
    """``homeassistant`` is not installed, so wiring is checked by source."""

    def test_setup_entry_runs_the_replay_after_the_first_refresh(self) -> None:
        source = (PACKAGE_DIR / "__init__.py").read_text()
        setup = source.index("async def async_setup_entry")
        body = source[setup:source.index("async def async_unload_entry")]
        assert "coordinator.async_config_entry_first_refresh()" in body
        assert "coordinator.async_ensure_topology_pushed()" in body
        assert body.index("async_config_entry_first_refresh") < body.index(
            "async_ensure_topology_pushed"
        )
        # The replay must not be able to fail setup.
        assert "await coordinator.async_ensure_topology_pushed()" in body
        assert "try:" not in body[body.index("async_ensure_topology_pushed") :]

    def test_coordinator_rechecks_on_version_mismatch(self) -> None:
        source = (PACKAGE_DIR / "coordinator.py").read_text()
        assert "async_replay_topology(" in source
        assert "CONF_CT_ASSIGNMENTS" in source
        assert "CONF_TOPOLOGY_CONFIRMED" in source
        assert "CONF_TOPOLOGY_VERSION" in source
        # The replay reuses the wizard's merge; a second implementation would
        # drift from it.
        assert "build_topology_update" not in source

    def test_the_replay_never_writes_channels_itself(self) -> None:
        """Only ``build_topology_update`` may construct the PUT body."""
        source = (PACKAGE_DIR / "flow_logic.py").read_text()
        replay = source[source.index("async def async_replay_topology") :]
        replay = replay[: replay.index("\ndef ")]
        assert "build_topology_update(" in replay
        assert "topology[\"channels\"]" not in replay

    def test_diagnostics_exposes_the_push_audit(self) -> None:
        source = (PACKAGE_DIR / "diagnostics.py").read_text()
        assert "topology_push_diagnostics" in source
        state = flow.TopologyPushState()
        view = state.as_diagnostics()
        for key in (
            "topology_pushed",
            "topology_push_version",
            "topology_push_error",
            "topology_push_attempts",
        ):
            assert key in view, key

    def test_the_client_and_coordinator_expose_the_audit_fields(self) -> None:
        coordinator = (PACKAGE_DIR / "coordinator.py").read_text()
        for key in (
            "topology_pushed",
            "topology_push_version",
            "topology_push_error",
            "topology_push_attempts",
        ):
            assert key in flow.TopologyPushState().as_diagnostics()
        assert "topology_push_diagnostics" in coordinator
        assert "def topology_pushed(" in coordinator
