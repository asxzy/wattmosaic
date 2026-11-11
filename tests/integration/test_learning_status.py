"""Learning status surfaced honestly, and the way to unblock it.

Two defects, one report
-----------------------
**L1 — untruthful reporting.** ``binary_sensor.wattmosaic_engine_active_learning``
was ``on`` while every sample was rejected: the topology was a ``candidate``
(``topology_confirmed=false``), so the filter stamped ``ambiguous_topology`` on
each sample.  ``device_models`` stayed empty and ``model_estimated_power`` was
0 W continuously.  The binary sensor must mean "samples are being accepted".

**L2 — no way out.** ``POST /topology/confirm`` existed and the wizard called it,
but nothing could flip ``CONF_TOPOLOGY_CONFIRMED`` afterwards, so a user who never
re-ran the wizard was permanently stuck.  ``button.wattmosaic_confirm_topology``
is that missing action.

``homeassistant`` is not installed here, so this module imports the *pure*
pieces (``entity_values``, ``topology_confirm`` — the decision logic the
entities delegate to) exactly the way ``test_ha_integration.py`` does, and adds
AST checks on the thin HA wiring in ``binary_sensor.py`` / ``button.py``.  The
engine is a recording fake; no real Home Assistant or engine is ever contacted.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PACKAGE_DIR = (
    Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
)
PACKAGE_NAME = "wm_learning_status_under_test"


def _load_package() -> types.ModuleType:
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
entity_values = importlib.import_module(f"{PACKAGE_NAME}.entity_values")
entity_defs = importlib.import_module(f"{PACKAGE_NAME}.entity_defs")
confirm = importlib.import_module(f"{PACKAGE_NAME}.topology_confirm")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Recorded engine
# --------------------------------------------------------------------------- #


class FakeEngine:
    """Records the three topology calls; changes status only when confirmed."""

    def __init__(
        self,
        status: str = "candidate",
        *,
        confirm_error: BaseException | None = None,
        topology_error: BaseException | None = None,
    ) -> None:
        self._status = status
        self._version = 1
        self.confirm_error = confirm_error
        self.topology_error = topology_error
        self.put_calls: list[dict[str, Any]] = []
        self.confirm_calls: list[Any] = []
        self.topology_calls = 0

    async def topology(self) -> dict[str, Any]:
        self.topology_calls += 1
        if self.topology_error is not None:
            raise self.topology_error
        return {
            "version": self._version,
            "status": self._status,
            "channels": [
                {
                    "ct_id": "ct_l1",
                    "entity_id": "sensor.l1",
                    "name": "L1",
                    "ct_type": "main",
                    "phase": "l1",
                    "topology_status": self._status,
                }
            ],
            "devices": [{"device_id": "dev", "name": "Dev", "topology_status": self._status}],
        }

    async def put_topology(self, topology: dict[str, Any]) -> dict[str, Any]:
        self.put_calls.append(dict(topology))
        self._version += 1
        self._status = "candidate"
        return {**topology, "version": self._version, "status": "candidate"}

    async def confirm_topology(self, version: Any = None) -> dict[str, Any]:
        self.confirm_calls.append(version)
        if self.confirm_error is not None:
            raise self.confirm_error
        self._status = "confirmed"
        return await self.topology()


def _assignments() -> dict[str, dict[str, Any]]:
    return {"ct_l1": {"role": "main", "phase": "l1"}}


# --------------------------------------------------------------------------- #
# L1: the binary sensor reports the truth
# --------------------------------------------------------------------------- #


def _health(*, effective: str, seen: int, accepted: int, loop_active: bool = True) -> dict[str, Any]:
    rejected = seen - accepted
    reasons = {"ambiguous_topology": rejected} if rejected else {}
    return {
        "status": "ok",
        "learning_active": loop_active,
        "details": {
            "learning_status": {
                "enabled": True,
                "active": loop_active,
                "effective_state": effective,
                "samples_seen": seen,
                "samples_accepted": accepted,
                "samples_rejected": rejected,
                "acceptance_rate": (accepted / seen) if seen else None,
                "rejection_reasons": reasons,
                "dominant_rejection_reason": "ambiguous_topology" if rejected else None,
                "dominant_rejection_count": rejected,
                "topology_quality": "candidate" if effective == "blocked" else "confirmed",
                "summary": f"learning is {effective}",
                "remedy": "press button.wattmosaic_confirm_topology" if effective == "blocked" else None,
            }
        },
    }


def _values(health: dict[str, Any], *, engine_available: bool = True) -> dict[str, Any]:
    return entity_values.global_values(
        {"timestamp": NOW.isoformat()},
        health,
        engine_available=engine_available,
        now=NOW,
        stale_after=120,
    )


class TestActiveLearningBinarySensor:
    def test_blocked_reports_off_even_though_the_loop_is_active(self) -> None:
        """The exact live defect: loop on, every sample rejected, sensor said on."""
        health = _health(effective="blocked", seen=28, accepted=0, loop_active=True)
        values = _values(health)
        assert values["learning_active"] is False, (
            "active_learning must be off when samples are rejected, even while "
            "the engine's learning loop is active"
        )
        assert values["effective_state"] == "blocked"

    def test_blocked_attributes_carry_reason_counts_and_remedy(self) -> None:
        health = _health(effective="blocked", seen=28, accepted=0)
        values = _values(health)
        assert values["effective_state"] == "blocked"
        assert values["dominant_rejection_reason"] == "ambiguous_topology"
        assert values["samples_seen"] == 28
        assert values["samples_accepted"] == 0
        assert values["samples_rejected"] == 28
        assert values["topology_quality"] == "candidate"
        assert values["accounting_available"] is True
        assert "confirm_topology" in (values["learning_remedy"] or "")
        assert values["learning_summary"]

    def test_learning_reports_on(self) -> None:
        health = _health(effective="learning", seen=10, accepted=7)
        values = _values(health)
        assert values["learning_active"] is True
        assert values["effective_state"] == "learning"
        assert values["samples_accepted"] == 7
        assert values["acceptance_rate"] == 70.0

    def test_off_when_paused(self) -> None:
        health = _health(effective="off", seen=5, accepted=5, loop_active=False)
        values = _values(health)
        assert values["learning_active"] is False
        assert values["effective_state"] == "off"

    def test_off_when_the_engine_is_unreachable(self) -> None:
        health = _health(effective="learning", seen=10, accepted=10)
        values = _values(health, engine_available=False)
        assert values["learning_active"] is False
        assert values["effective_state"] == "off"

    def test_legacy_engine_without_accounting_falls_back_to_the_loop_flag(self) -> None:
        """An older engine must not be reported as blocked from missing data."""
        values = _values({"status": "ok", "learning_active": True})
        assert values["learning_active"] is True
        assert values["effective_state"] == "learning"
        assert values["accounting_available"] is False
        assert values["samples_seen"] is None

    def test_entity_id_is_unchanged(self) -> None:
        """Renaming the entity would break existing dashboards."""
        source = (PACKAGE_DIR / "binary_sensor.py").read_text()
        assert '"learning_active"' in source
        assert const.GLOBAL_BINARY_KEYS == ("engine_reachable", "learning_active")
        assert "learning_active" in entity_defs.GLOBAL_BINARY_SENSORS

    def test_binary_sensor_exposes_the_accounting_as_attributes(self) -> None:
        source = (PACKAGE_DIR / "binary_sensor.py").read_text()
        for attribute in (
            "effective_state",
            "dominant_rejection_reason",
            "samples_accepted",
            "samples_seen",
            "samples_rejected",
            "learning_remedy",
        ):
            assert f'"{attribute}"' in source or f"'{attribute}'" in source, attribute


# --------------------------------------------------------------------------- #
# L2: the confirm button
# --------------------------------------------------------------------------- #


class TestConfirmAvailability:
    def test_available_only_when_candidate_and_a_declaration_exists(self) -> None:
        availability = confirm.confirm_availability({"status": "candidate"}, _assignments())
        assert availability.available is True
        assert availability.reason == confirm.REASON_AVAILABLE
        assert availability.as_attributes()["topology_status"] == "candidate"

    def test_unavailable_once_confirmed(self) -> None:
        availability = confirm.confirm_availability({"status": "confirmed"}, _assignments())
        assert availability.available is False
        assert availability.reason == confirm.REASON_ALREADY_CONFIRMED

    def test_unavailable_without_a_stored_declaration(self) -> None:
        """Confirming an engine-invented topology would fabricate user intent."""
        availability = confirm.confirm_availability({"status": "candidate"}, {})
        assert availability.available is False
        assert availability.reason == confirm.REASON_NO_DECLARATION

    def test_unavailable_when_the_engine_is_unreachable(self) -> None:
        availability = confirm.confirm_availability(
            {"status": "candidate"}, _assignments(), engine_available=False
        )
        assert availability.available is False
        assert availability.reason == confirm.REASON_ENGINE_UNREACHABLE

    def test_unavailable_when_the_status_is_unknown(self) -> None:
        assert confirm.confirm_availability(None, _assignments()).available is False
        assert confirm.confirm_availability({"status": ""}, _assignments()).available is False

    def test_entity_id_is_pinned_and_documented(self) -> None:
        """``blocked`` names this id as the remedy, so it must not drift."""
        assert const.BUTTON_CONFIRM_TOPOLOGY_ENTITY_ID == (
            "button.wattmosaic_confirm_topology"
        )
        assert "confirm_topology" in entity_defs.GLOBAL_BUTTONS
        button = (PACKAGE_DIR / "button.py").read_text()
        assert "button.wattmosaic_confirm_topology" in button
        assert "button" in const.PLATFORMS
        translations = (PACKAGE_DIR / "translations" / "en.json").read_text()
        assert '"confirm_topology"' in translations


class TestConfirmPress:
    def test_press_confirms_once_with_the_stored_assignments(self) -> None:
        engine = FakeEngine("candidate")
        result = run(
            confirm.async_confirm_topology(engine, _assignments(), stored_version=1)
        )
        assert result.ok is True
        assert result.entry_confirmed is True
        assert result.status == "confirmed"
        assert len(engine.confirm_calls) == 1, "confirm exactly once"
        assert len(engine.put_calls) == 1, "the declaration is written once"
        # The write carries the stored declaration, not an invented one.
        sent = {channel["ct_id"]: channel for channel in engine.put_calls[0]["channels"]}
        assert sent["ct_l1"]["ct_type"] == "main"
        assert sent["ct_l1"]["phase"] == "l1"

    def test_press_writes_the_confirmed_status(self) -> None:
        engine = FakeEngine("candidate")
        run(confirm.async_confirm_topology(engine, _assignments(), stored_version=1))
        assert engine.put_calls[0]["status"] == "confirmed"

    def test_double_press_is_idempotent(self) -> None:
        engine = FakeEngine("candidate")
        first = run(confirm.async_confirm_topology(engine, _assignments(), stored_version=1))
        writes_after_first = (len(engine.put_calls), len(engine.confirm_calls))
        second = run(confirm.async_confirm_topology(engine, _assignments(), stored_version=1))

        assert first.ok and second.ok
        assert second.entry_confirmed is True
        assert (len(engine.put_calls), len(engine.confirm_calls)) == writes_after_first, (
            "a second press on a confirmed engine must not write again"
        )
        assert "already confirmed" in second.message.lower()

    def test_press_without_a_declaration_refuses(self) -> None:
        engine = FakeEngine("candidate")
        result = run(confirm.async_confirm_topology(engine, {}))
        assert result.ok is False
        assert result.attempted is False
        assert engine.put_calls == [] and engine.confirm_calls == []

    def test_engine_error_does_not_raise_and_does_not_set_the_entry_flag(self) -> None:
        engine = FakeEngine("candidate", confirm_error=RuntimeError("engine said no"))
        result = run(confirm.async_confirm_topology(engine, _assignments(), stored_version=1))
        assert result.ok is False
        assert result.entry_confirmed is False, (
            "a PUT that landed but was not confirmed must not tell the user "
            "learning is enabled"
        )
        assert result.still_candidate is True

    def test_read_failure_is_reported_not_raised(self) -> None:
        engine = FakeEngine("candidate", topology_error=ConnectionError("down"))
        result = run(confirm.async_confirm_topology(engine, _assignments()))
        assert result.ok is False
        assert result.attempted is False
        assert "reachable" in result.message.lower()

    def test_result_attributes_are_json_safe(self) -> None:
        import json

        engine = FakeEngine("candidate")
        result = run(confirm.async_confirm_topology(engine, _assignments(), stored_version=1))
        assert json.loads(json.dumps(result.as_attributes()))["topology_status"] == "confirmed"


class TestEntryFlag:
    def test_flag_is_set_in_data_for_a_wizard_entry(self) -> None:
        data, options = confirm.confirmed_entry_updates(
            {"topology_confirmed": False, "topology_version": 1}, {}, version=2
        )
        assert data[const.CONF_TOPOLOGY_CONFIRMED] is True
        assert data[const.CONF_TOPOLOGY_VERSION] == 2
        assert const.CONF_TOPOLOGY_CONFIRMED not in options

    def test_flag_is_set_in_options_when_that_is_where_it_lives(self) -> None:
        """The coordinator merges ``{**data, **options}``, so the right container matters."""
        data, options = confirm.confirmed_entry_updates(
            {}, {"topology_confirmed": False, "topology_version": 3}, version=4
        )
        merged = {**data, **options}
        assert merged[const.CONF_TOPOLOGY_CONFIRMED] is True
        assert merged[const.CONF_TOPOLOGY_VERSION] == 4

    def test_flag_defaults_to_data_for_an_entry_that_never_had_it(self) -> None:
        data, options = confirm.confirmed_entry_updates({"base_url": "http://e:1"}, {})
        assert data[const.CONF_TOPOLOGY_CONFIRMED] is True
        assert options == {}

    def test_unrelated_options_are_preserved(self) -> None:
        data, options = confirm.confirmed_entry_updates(
            {"topology_confirmed": False}, {"scan_interval": 30, "energy_entities": {"d": False}}
        )
        assert options["scan_interval"] == 30
        assert options["energy_entities"] == {"d": False}
        assert data[const.CONF_TOPOLOGY_CONFIRMED] is True


# --------------------------------------------------------------------------- #
# The wiring: never automatic, and a button (not a service)
# --------------------------------------------------------------------------- #


def _functions(path: Path) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text())
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


class TestButtonWiring:
    def test_press_never_happens_automatically_on_setup(self) -> None:
        """Confirmation is a user decision; setup must not press it for them."""
        functions = _functions(PACKAGE_DIR / "button.py")
        setup = functions["async_setup_entry"]
        calls = {
            node.func.attr
            for node in ast.walk(setup)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "async_press" not in calls
        # And no other module may drive the confirmation.
        for name in ("__init__.py", "binary_sensor.py", "coordinator.py", "sensor.py"):
            source = (PACKAGE_DIR / name).read_text()
            assert "async_confirm_topology(" not in source, name
            assert "async_press(" not in source, name

    def test_the_only_confirmation_call_site_is_async_press(self) -> None:
        functions = _functions(PACKAGE_DIR / "button.py")
        press = functions["async_press"]
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "async_confirm_topology"
            for node in ast.walk(press)
        )
        for name, node in functions.items():
            if name == "async_press":
                continue
            assert not any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "async_confirm_topology"
                for call in ast.walk(node)
            ), f"async_confirm_topology is called from {name}"

    def test_button_platform_is_a_real_button_entity(self) -> None:
        tree = ast.parse((PACKAGE_DIR / "button.py").read_text())
        bases = [
            ast.unparse(base)
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name.endswith("Button")
            for base in node.bases
        ]
        assert any("ButtonEntity" in base for base in bases), bases

    def test_button_entity_is_registered_in_platforms(self) -> None:
        source = (PACKAGE_DIR / "__init__.py").read_text()
        assert "PLATFORMS" in source
        assert "button" in const.PLATFORMS

    def test_button_reuses_the_shared_replay_not_a_second_merge(self) -> None:
        """One merge rule: the wizard's ``build_topology_update``.

        Checked on the AST (calls), not the prose: the docstring legitimately
        *mentions* the function it delegates to.
        """
        tree = ast.parse((PACKAGE_DIR / "topology_confirm.py").read_text())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "async_replay_topology" in called
        assert "build_topology_update" not in called, (
            "topology_confirm must reuse flow_logic's merge, not re-implement it"
        )
        button_tree = ast.parse((PACKAGE_DIR / "button.py").read_text())
        button_called = {
            node.func.id
            for node in ast.walk(button_tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "build_topology_update" not in button_called

    def test_button_sets_the_entry_flag_and_reloads(self) -> None:
        source = (PACKAGE_DIR / "button.py").read_text()
        assert "confirmed_entry_updates" in source
        assert "async_update_entry" in source

    def test_button_reports_status_and_version_as_attributes(self) -> None:
        source = (PACKAGE_DIR / "button.py").read_text()
        assert "as_attributes()" in source
        assert "topology_version" in (PACKAGE_DIR / "topology_confirm.py").read_text()
