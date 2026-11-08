"""Static compatibility checks for the Home Assistant integration.

`homeassistant` is not installed in the development environment (and installing
it pulls a very large dependency tree). These checks therefore validate what can
be validated without it:

* every integration file parses and compiles,
* all intra-package imports resolve to files that exist,
* no integration module imports `homeassistant` at module import time in a file
  that the pure-logic tests import,
* the manifest/services/strings are internally consistent,
* no service-call / actuation API is used anywhere in the integration.

That last one is a hard safety invariant: the integration must never be able to
change a household device.

Run: python -m pytest tests/integration/test_integration_static.py -q
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "custom_components" / "wattmosaic"

FORBIDDEN_ACTUATION_PATTERNS = [
    r"\bhass\.services\.async_call\b",
    r"\bhass\.services\.call\b",
    r"\bservices\.async_call\b",
    r"\bhomeassistant\.components\.[a-z_]+\.services\b",
    r"\.async_turn_on\(",  # only allowed if clearly not a device entity; flagged for review
    r"\.async_turn_off\(",
]


def integration_files() -> list[Path]:
    return sorted(p for p in PKG.glob("*.py"))


def test_package_exists_and_has_code() -> None:
    assert PKG.is_dir(), "custom_components/wattmosaic missing"
    assert integration_files(), "integration has no python modules"


@pytest.mark.parametrize("path", integration_files(), ids=lambda p: p.name)
def test_module_compiles(path: Path) -> None:
    source = path.read_text()
    compile(source, str(path), "exec")
    tree = ast.parse(source)
    assert tree is not None


def test_no_deprecated_device_registry_lookup() -> None:
    """No deprecated device-registry access.

    Three APIs are deprecated inside the supported Home Assistant range:

    * ``device_registry.async_get_device()`` — removed 2027.8;
    * using ``registry.devices`` as a mapping (``.values()``, ``.get()``,
      subscription, device-id membership) — removed 2027.9;
    * ``device.config_entries`` — a device belongs to one config entry now,
      available as ``config_entry_id``; removed 2027.10.

    All three log a deprecation warning for custom integrations.  The
    non-deprecated replacements for the first two do not exist in the 2024.11
    minimum this integration supports, so iteration is used instead — which is
    the supported form on both versions.

    ``async_get(device_id)`` / ``async_get_entity_id`` / ``async_get`` on the
    entity registry are different, current APIs and are allowed.
    """
    mapping_methods = {"values", "get", "get_entry", "items", "keys"}
    offenders: list[str] = []
    for path in integration_files():
        tree = ast.parse(path.read_text())

        # Names that hold a *device registry*: ``from homeassistant.helpers import
        # device_registry as dr`` plus ``name = dr.async_get(hass)``.  This has to
        # be resolved, because ``coordinator.devices`` is an ordinary dict of
        # engine devices and calling ``.get()`` on it is not a registry access.
        registry_aliases = {
            alias.asname
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "homeassistant.helpers"
            for alias in node.names
            if alias.name == "device_registry" and alias.asname
        }
        registry_names: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "async_get"
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id in registry_aliases
            ):
                registry_names.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "async_get_device":
                offenders.append(f"{path.name}:{node.lineno} async_get_device()")
            if not isinstance(func, ast.Attribute) or func.attr not in mapping_methods:
                continue
            inner = func.value
            if not (isinstance(inner, ast.Attribute) and inner.attr == "devices"):
                continue
            if isinstance(inner.value, ast.Name) and inner.value.id in registry_names:
                offenders.append(
                    f"{path.name}:{node.lineno} {inner.value.id}.devices.{func.attr}()"
                )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "config_entries"
                and isinstance(node.value, ast.Name)
                and node.value.id in ("device", "device_entry", "entry_device")
            ):
                offenders.append(f"{path.name}:{node.lineno} device.config_entries")
    assert not offenders, (
        "Deprecated device-registry access (all emit deprecation warnings for "
        "custom integrations and are removed by 2027.10):\n" + "\n".join(offenders)
    )


def test_no_actuation_calls_anywhere() -> None:
    offenders: list[str] = []
    for path in integration_files():
        text = path.read_text()
        for pattern in FORBIDDEN_ACTUATION_PATTERNS:
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}: {pattern}")
    assert not offenders, (
        "Integration must be read-only toward devices; found actuation patterns:\n"
        + "\n".join(offenders)
    )


def test_entity_bases_inherit_their_platform_entity() -> None:
    """Every sensor base must actually be a ``SensorEntity``.

    Home Assistant's ``Entity.state`` reports ``unknown``; only ``SensorEntity``
    consults ``native_value``, and only ``BinarySensorEntity`` consults
    ``is_on``.  A base class that inherits ``CoordinatorEntity`` alone therefore
    produces entities that poll forever, carry fresh attributes, and never
    publish a value — which is exactly what shipped once already.
    """
    expected = {
        "sensor.py": "SensorEntity",
        "binary_sensor.py": "BinarySensorEntity",
    }
    problems: list[str] = []
    for filename, base in expected.items():
        tree = ast.parse((PKG / filename).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not node.bases:
                continue
            base_texts = [ast.unparse(b) for b in node.bases]
            uses_coordinator = any(t.startswith("CoordinatorEntity") for t in base_texts)
            if uses_coordinator and base not in base_texts:
                problems.append(
                    f"{filename}:{node.lineno} {node.name} bases={base_texts} "
                    f"missing {base}"
                )
    assert not problems, (
        "Entities not derived from their platform entity class:\n" + "\n".join(problems)
    )


def test_local_imports_resolve() -> None:
    missing: list[str] = []
    for path in integration_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level >= 1:
                # relative import within the package
                names = [n.name for n in node.names]
                module = node.module
                if module is None:
                    for name in names:
                        if not (PKG / f"{name}.py").exists() and not (PKG / name).is_dir():
                            missing.append(f"{path.name}: from . import {name}")
                else:
                    base = PKG / module.replace(".", "/")
                    if not base.with_suffix(".py").exists() and not base.is_dir():
                        missing.append(f"{path.name}: from .{module} import {names}")
    assert not missing, "unresolved relative imports:\n" + "\n".join(missing)


def test_pure_logic_modules_do_not_import_homeassistant() -> None:
    """These modules are imported by the offline test-suite; they must stay pure."""
    pure = ["util.py", "flow_logic.py", "service_logic.py", "entity_values.py", "entity_defs.py", "const.py"]
    for name in pure:
        path = PKG / name
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("homeassistant"), f"{name} imports {alias.name}"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("homeassistant"), f"{name} imports {node.module}"


def test_manifest_consistency() -> None:
    manifest = json.loads((PKG / "manifest.json").read_text())
    assert manifest["domain"] == "wattmosaic"
    assert manifest["config_flow"] is True
    assert manifest["version"] == "0.1.5"
    assert manifest.get("iot_class") == "local_polling"
    # HA provides aiohttp
    assert "aiohttp" not in json.dumps(manifest.get("requirements", []))
    strings = json.loads((PKG / "strings.json").read_text())
    assert "config" in strings


def test_manifest_version_matches_const() -> None:
    """Two copies of the version must not drift: const.VERSION is reported to HA."""
    manifest = json.loads((PKG / "manifest.json").read_text())
    const_text = (PKG / "const.py").read_text()
    match = re.search(r'^VERSION = "([^"]+)"', const_text, re.MULTILINE)
    assert match, "const.VERSION not found"
    assert match.group(1) == manifest["version"]


def test_config_flow_has_the_hassio_discovery_step() -> None:
    """App-install discovery needs ``async_step_hassio`` with that exact name."""
    text = (PKG / "config_flow.py").read_text()
    assert "async def async_step_hassio(" in text
    assert "async def async_step_hassio_confirm(" in text
    assert "HassioServiceInfo" in text
    assert "discovered_base_url(" in text


def test_app_manifest_declares_the_discovery_domain() -> None:
    """The Supervisor refuses a domain the app did not list.

    ``supervisor/api/discovery.py`` compares the posted service against
    ``app.discovery`` (parsed from ``discovery:`` in config.yaml) and answers 403
    otherwise, so a missing entry makes the whole feature silently dead.
    """
    config = yaml.safe_load((ROOT / "addon" / "config.yaml").read_text())
    assert "wattmosaic" in (config.get("discovery") or [])


def test_discovery_translations_exist_for_the_confirm_step() -> None:
    """An untranslated step renders an empty dialog, not an error."""
    for name in ("strings.json", "translations/en.json"):
        data = json.loads((PKG / name).read_text())
        step = data["config"]["step"].get("hassio_confirm")
        assert step is not None, f"{name} has no config.step.hassio_confirm"
        assert step.get("title")
        assert step.get("description")
        # Placeholders used by the flow must be present in the text.
        for placeholder in ("{url}", "{channel_count}"):
            assert placeholder in step["description"], (name, placeholder)


def test_config_flow_registers_expected_steps() -> None:
    text = (PKG / "config_flow.py").read_text()
    assert "async_step_user" in text
    assert "OptionsFlow" in text or "async_step_init" in text


def test_services_yaml_matches_documented_services() -> None:
    services = yaml.safe_load((PKG / "services.yaml").read_text())
    expected = {
        "start_learning", "pause_learning", "resume_learning", "accept_sample",
        "reject_sample", "rebuild_model", "freeze_model", "unfreeze_model",
        "reset_device_model", "rebuild_topology", "run_diagnostics", "export_device_data",
    }
    assert set(services) == expected
    for name in ("reset_device_model", "rebuild_topology", "reject_sample"):
        field = services[name]["fields"]["confirm"]
        assert field.get("required") is True, f"{name} confirm must be required"
        assert field.get("default") is not True, f"{name} must not default confirm to true"


def test_no_secrets_in_integration() -> None:
    for path in integration_files() + [PKG / "strings.json", PKG / "services.yaml"]:
        text = path.read_text()
        assert not re.search(r"\beyJ[A-Za-z0-9_-]{20,}", text), path.name
        assert "Bearer " not in text or "Authorization" in text, path.name


def test_api_client_redacts_token() -> None:
    text = (PKG / "api_client.py").read_text()
    assert "redact" in text.lower() or "***" in text, "api_client must redact tokens in errors"
    assert "X-API-Token" in text
    assert "X-WattMosaic-Contract-Version" in text
