"""The hub (engine) device link must not use the deprecated ``via_device`` pair.

Home Assistant 2026.8 deprecated ``DeviceInfo["via_device"]=(domain, identifier)``
in favour of ``via_device_id=<registry id>``.  The deprecation is *not* only a
warning: when the report happens inside a frame whose reporting behaviour is
``ERROR``, Home Assistant raises ``RuntimeError`` and the entity is never added.

That is exactly what took 276 of 322 sensors offline on a live installation:
a registry pass executed inside the ``config`` integration's frame caused every
re-created sensor to report the deprecated call, and each one aborted.

Two things therefore have to hold, and both are pinned here:

1. On a Home Assistant that understands ``via_device_id`` we emit **only** that.
   Both keys at once raise ``HomeAssistantError``; the pair alone raises
   ``RuntimeError`` in a strict frame.
2. On an older Home Assistant (the declared 2024.11 minimum) ``via_device_id``
   is not an accepted keyword at all, so the pair is the only correct form.

These tests run without ``homeassistant`` installed by loading the pure module.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
PACKAGE_NAME = "wm_hub_device_under_test"

# Synthetic identifiers. The real ones are installation-specific and must never
# be committed: a config entry id names a live household's installation, and
# `tools/dev/check_secrets.py` rejects one appearing in the tree.
HUB_ID = "0123456789abcdef0123456789abcdef"
ENTRY_ID = "0123456789ABCDEFGHIJKLMNOPQRSTUV"
IDENTIFIER = ("wattmosaic", f"engine_{ENTRY_ID}")


def _load_package() -> types.ModuleType:
    """Create a package handle without executing ``__init__`` (it imports HA)."""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(PACKAGE_DIR)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = module
    return module


_load_package()
entity_values = importlib.import_module(f"{PACKAGE_NAME}.entity_values")
via_device_kwargs = entity_values.via_device_kwargs


class TestModernHomeAssistant:
    """2026.8+ understands ``via_device_id``; only that key may be emitted."""

    def test_uses_the_registry_id(self) -> None:
        assert via_device_kwargs(
            supports_via_device_id=True,
            hub_device_id=HUB_ID,
            hub_identifier=IDENTIFIER,
        ) == {"via_device_id": HUB_ID}

    def test_never_emits_the_deprecated_pair(self) -> None:
        """Both keys together raise; the pair alone aborts in a strict frame."""
        kwargs = via_device_kwargs(
            supports_via_device_id=True,
            hub_device_id=HUB_ID,
            hub_identifier=IDENTIFIER,
        )
        assert "via_device" not in kwargs

    def test_missing_hub_id_drops_the_link_rather_than_falling_back(self) -> None:
        """A parentless device is harmless; the deprecated pair is not.

        Falling back to ``via_device`` here would reintroduce the exact crash
        this module exists to prevent, so the link is simply omitted.
        """
        for value in (None, "", 0):
            kwargs = via_device_kwargs(
                supports_via_device_id=True,
                hub_device_id=value,
                hub_identifier=IDENTIFIER,
            )
            assert kwargs == {}
            assert "via_device" not in kwargs


class TestLegacyHomeAssistant:
    """2024.11 – 2026.7 reject ``via_device_id``; the pair is the only option."""

    def test_uses_the_identifier_pair(self) -> None:
        assert via_device_kwargs(
            supports_via_device_id=False,
            hub_device_id=HUB_ID,
            hub_identifier=IDENTIFIER,
        ) == {"via_device": IDENTIFIER}

    def test_never_emits_the_unknown_keyword(self) -> None:
        """``async_get_or_create`` on 2024.11 has no ``via_device_id``."""
        kwargs = via_device_kwargs(
            supports_via_device_id=False,
            hub_device_id=None,
            hub_identifier=IDENTIFIER,
        )
        assert "via_device_id" not in kwargs


class TestExactlyOneKey:
    """The two forms are mutually exclusive and must never be combined."""

    def test_each_capability_yields_exactly_one_link_key(self) -> None:
        for supported in (True, False):
            kwargs = via_device_kwargs(
                supports_via_device_id=supported,
                hub_device_id=HUB_ID,
                hub_identifier=IDENTIFIER,
            )
            link_keys = {"via_device", "via_device_id"} & set(kwargs)
            assert len(link_keys) == 1, (supported, kwargs)


class TestCallSites:
    """Every entity that parents itself to the hub goes through the helper.

    A literal ``via_device=`` left behind in a platform module would reintroduce
    the outage for those entities only, which is worse than a uniform failure
    because it looks like a data problem.
    """

    def test_no_platform_module_sets_via_device_literally(self) -> None:
        for name in ("sensor.py", "binary_sensor.py"):
            source = (PACKAGE_DIR / name).read_text()
            assert "via_device=" not in source, f"{name} still hardcodes via_device="

    def test_platforms_use_the_shared_helper(self) -> None:
        for name in ("sensor.py", "binary_sensor.py"):
            source = (PACKAGE_DIR / name).read_text()
            assert "via_device_for_entry(" in source, f"{name} does not use the helper"

    def test_static_test_does_not_treat_the_pair_as_actuation(self) -> None:
        """Sanity: the helper's own identifier pair is not a service call."""
        source = (PACKAGE_DIR / "hub_device.py").read_text()
        assert "async_get_or_create" in source
        assert "async_call" not in source


class TestHubRegistrationOrder:
    """The hub must exist before platforms forward, or the id is rejected."""

    def test_setup_creates_the_hub_before_forwarding_platforms(self) -> None:
        source = (PACKAGE_DIR / "__init__.py").read_text()
        create_at = source.index("async_create_engine_device(")
        forward_at = source.index("async_forward_entry_setups(")
        assert create_at < forward_at, (
            "the engine device is created after platforms are forwarded; "
            "child devices would reference an unknown via_device_id"
        )

    def test_hub_id_is_stored_for_the_platforms_to_read(self) -> None:
        source = (PACKAGE_DIR / "__init__.py").read_text()
        assert '"engine_device_id"' in source
