"""Learning status surfaced honestly.

**L1 — untruthful reporting.** ``binary_sensor.wattmosaic_engine_active_learning``
was ``on`` while every sample was rejected: the topology was a ``candidate``
(``topology_confirmed=false``), so the filter stamped ``ambiguous_topology`` on
each sample.  ``device_models`` stayed empty and ``model_estimated_power`` was
0 W continuously.  The binary sensor must mean "samples are being accepted".

The confirm-topology button (the old "no way out" remedy) was removed at the
3.0 cutover; topology changes now go through the per-edge v3 routes.  A small
class below asserts that removal.

``homeassistant`` is not installed here, so this module imports the *pure*
pieces (``entity_values`` — the decision logic the entities delegate to)
exactly the way ``test_ha_integration.py`` does, and adds AST checks on the thin
HA wiring in ``binary_sensor.py``.  No real Home Assistant or engine is ever
contacted.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
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


NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


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
# 3.0 cutover: the confirm-topology button is gone
# --------------------------------------------------------------------------- #


class TestConfirmButtonRemoved:
    def test_button_platform_and_module_are_gone(self) -> None:
        """Topology changes moved to the per-edge v3 routes, not a button."""
        assert "button" not in const.PLATFORMS
        assert not (PACKAGE_DIR / "button.py").exists()
        assert not (PACKAGE_DIR / "topology_confirm.py").exists()
        assert not hasattr(const, "BUTTON_CONFIRM_TOPOLOGY_ENTITY_ID")
        assert not hasattr(entity_defs, "GLOBAL_BUTTONS")

    def test_no_confirm_service_or_translation(self) -> None:
        services = (PACKAGE_DIR / "services.yaml").read_text()
        assert "confirm_topology" not in services
        for name in ("strings.json", "translations/en.json"):
            data = json.loads((PACKAGE_DIR / name).read_text())
            assert "button" not in data.get("entity", {})
