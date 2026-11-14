"""Deprecated *declarations* — never deprecated *measurements*.

The manual ``dedicated_device`` declaration is deprecated (ADR-0029).  The
capability it exercises is **not**: ``Source.DIRECT_MEASURED``, ``resolve_direct``
and ``TopologyGraph.dedicated_channel_for`` keep working unchanged, because the
reference installation's ~1032 W of directly measured power depends on them.

The same fact is stored in three places with no precedence:

* ``CTChannel.device_id`` on a channel whose ``ct_type`` is ``DEDICATED_DEVICE``,
* ``DeviceTopology.dedicated_ct_ids``,
* ``DeviceConfiguration.monitored_by_ct_ids``.

:func:`collect` therefore deduplicates by ``ct_id``: one notice per CT, not one
per place the user wrote the same thing.  :func:`warn_once` emits one ``WARNING``
per ``ct_id`` for the life of the process (resettable for tests), so a per-sample
loop cannot flood the log even if a caller keeps re-collecting.

Nothing here changes watts, ``source``, ``tier`` or ``confidence``.  The module
imports only the contract enum; the scan is duck-typed so a diagnostics caller
may pass stored payloads or plain mappings without constructing a graph.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import CTType

#: Warning/diagnostics code.  Frozen by ``docs/deprecation_dedicated_declaration.md``.
DEDICATED_DECLARATION = "dedicated_declaration"

#: The documented replacement, spelled once.  State the non-conversion
#: explicitly: the notice must never read as "this has been migrated".
_REPLACEMENT = (
    "Declare the channel as 'branch' and let circuit inference classify it; "
    "CircuitClass.DEDICATED at TopologyStatus.CONFIRMED is the evidenced "
    "equivalent. Existing dedicated_device declarations are not auto-converted."
)

_MESSAGE = (
    "CT {ct_id} is declared 'dedicated_device', which is deprecated. "
    "The direct measurement it enables still reports; nothing is auto-converted. "
    "Declare the channel as 'branch' instead and let circuit inference classify "
    "it; CircuitClass.DEDICATED at CONFIRMED is the evidenced equivalent."
)


@dataclass(frozen=True, slots=True)
class DeclarationSpec:
    """Registry entry for one deprecated declaration kind."""

    code: str
    since: str
    replacement: str


#: The vocabulary of deprecated declarations.  A notice is only ever emitted for
#: a code registered here, so adding one is a deliberate act.
DEPRECATED_DECLARATIONS: dict[str, DeclarationSpec] = {
    DEDICATED_DECLARATION: DeclarationSpec(
        code=DEDICATED_DECLARATION,
        since="1.7.0",
        replacement=_REPLACEMENT,
    )
}


@dataclass(frozen=True, slots=True)
class Deprecation:
    """One deprecated declaration, keyed by ``ct_id``.

    Field names are the frozen diagnostics schema; ``as_dict`` is the only
    serialisation used at the API boundary.
    """

    code: str
    ct_id: str
    device_id: str | None
    since: str
    replacement: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "ct_id": self.ct_id,
            "device_id": self.device_id,
            "since": self.since,
            "replacement": self.replacement,
            "message": self.message,
        }


def _value(value: Any) -> Any:
    """Enum or plain value, without importing every contract type."""
    return getattr(value, "value", value)


def _items(source: Any, name: str) -> Iterable[Any]:
    value = source.get(name) if isinstance(source, Mapping) else getattr(source, name, None)
    return value or ()


def _channels(source: Any) -> Iterable[Any]:
    return _items(source, "channels")


def _devices(source: Any) -> Iterable[Any]:
    return _items(source, "devices")


def _dedicated_ct_from_channel(channel: Any) -> tuple[str, str | None] | None:
    ct_id = str(getattr(channel, "ct_id", "") or "")
    if not ct_id:
        return None
    if _value(getattr(channel, "ct_type", None)) != CTType.DEDICATED_DEVICE.value:
        return None
    device_id = getattr(channel, "device_id", None)
    return ct_id, str(device_id) if device_id else None


def _dedicated_cts_from_device(device: Any) -> Iterable[tuple[str, str | None]]:
    device_id = getattr(device, "device_id", None)
    owned = str(device_id) if device_id else None
    for raw in getattr(device, "dedicated_ct_ids", None) or ():
        ct_id = str(raw or "")
        if ct_id:
            yield ct_id, owned


def _declared_cts_from_config(config: Any) -> Iterable[tuple[str, str | None, bool]]:
    """``(ct_id, device_id, dedicated)`` from one device configuration.

    ``monitored_by_ct_ids`` is also how a shared circuit is declared (ADR-0026),
    and a *branch* declaration is the replacement this deprecation recommends —
    reporting it would be backwards.  ``monitoring == DEDICATED`` is the only
    signal available when no topology is supplied; with a topology, the CT's own
    ``ct_type`` decides (see :func:`collect`).
    """
    device_id = getattr(config, "device_id", None)
    owned = str(device_id) if device_id else None
    dedicated = _value(getattr(config, "monitoring", None)) == "dedicated"
    for raw in getattr(config, "monitored_by_ct_ids", None) or ():
        ct_id = str(raw or "")
        if ct_id:
            yield ct_id, owned, dedicated


def declares_dedicated(topology: Any) -> bool:
    """True when the topology itself contains a manual declaration.

    Pure and cheap, with no database access: ``/health`` is polled every few
    seconds, so the common install (no ``dedicated_device`` row) must not pay for
    a read of the per-device configurations just to answer "nothing deprecated".
    """
    for channel in _channels(topology):
        ct_type = getattr(channel, "ct_type", None)
        if _value(ct_type) == CTType.DEDICATED_DEVICE.value:
            return True
    return any(getattr(device, "dedicated_ct_ids", None) for device in _devices(topology))


def collect(*sources: Any) -> list[Deprecation]:
    """Deprecated declarations found in any of the three storage locations.

    ``sources`` may contain a ``Topology`` (channels + devices), a
    ``DeviceConfigurationSet`` (per-device declarations), a mapping with
    ``channels``/``devices`` keys, or a single channel/device object.  Unknown
    objects are ignored: diagnostics must never fail because a caller passed
    something unexpected.

    Deduplication is by ``ct_id``.  The first occurrence wins, so a channel that
    carries ``device_id`` reports that device and a later duplicate cannot
    overwrite it with ``None``.
    """
    found: dict[str, Deprecation] = {}

    def add(ct_id: str, device_id: str | None) -> None:
        if not ct_id or ct_id in found:
            return
        spec = DEPRECATED_DECLARATIONS[DEDICATED_DECLARATION]
        found[ct_id] = Deprecation(
            code=spec.code,
            ct_id=ct_id,
            device_id=device_id,
            since=spec.since,
            replacement=spec.replacement,
            message=_MESSAGE.format(ct_id=ct_id),
        )

    dedicated_in_topology: set[str] = set()
    config_declarations: list[tuple[str, str | None, bool]] = []

    try:
        for source in sources:
            if source is None:
                continue
            channels = list(_channels(source))
            devices = list(_devices(source))
            for channel in channels:
                parsed = _dedicated_ct_from_channel(channel)
                if parsed is not None:
                    dedicated_in_topology.add(parsed[0])
                    add(*parsed)
            for device in devices:
                if hasattr(device, "dedicated_ct_ids"):
                    for ct_id, device_id in _dedicated_cts_from_device(device):
                        dedicated_in_topology.add(ct_id)
                        add(ct_id, device_id)
                elif hasattr(device, "monitored_by_ct_ids"):
                    config_declarations.extend(_declared_cts_from_config(device))
            # A bare channel/device passed directly.
            if not channels and not devices:
                parsed = _dedicated_ct_from_channel(source)
                if parsed is not None:
                    dedicated_in_topology.add(parsed[0])
                    add(*parsed)
                elif hasattr(source, "dedicated_ct_ids"):
                    for ct_id, device_id in _dedicated_cts_from_device(source):
                        dedicated_in_topology.add(ct_id)
                        add(ct_id, device_id)
    except Exception:  # noqa: BLE001 - a diagnostics scan must never raise
        return list(found.values())

    for ct_id, device_id, dedicated in config_declarations:
        # With a topology to compare against, the CT's own type decides: only a
        # row the topology classes as dedicated is a deprecated declaration.  A
        # branch assertion is the advice this notice gives, not the problem.
        if dedicated_in_topology and ct_id not in dedicated_in_topology:
            continue
        if not dedicated_in_topology and not dedicated:
            continue
        add(ct_id, device_id)
    return list(found.values())


# ``ct_id`` -> already warned.  Module-level so repeated topology builds in one
# process emit one line per CT; ``reset_warnings`` exists for tests and for a
# long-lived process that legitimately wants a fresh notice.
_WARNED: set[str] = set()


def reset_warnings() -> None:
    """Forget which CTs were warned about (tests, explicit re-announcement)."""
    _WARNED.clear()


def warn_once(deprecations: Iterable[Deprecation], logger: logging.Logger) -> int:
    """Emit one ``WARNING`` per not-yet-warned ``ct_id``; return the count.

    Deduplicated across calls for the life of the process.  Call this where a
    topology is installed or a configuration changes — never inside a per-sample
    or per-inference loop.
    """
    emitted = 0
    for deprecation in deprecations:
        ct_id = deprecation.ct_id
        if not ct_id or ct_id in _WARNED:
            continue
        _WARNED.add(ct_id)
        logger.warning("%s", deprecation.message)
        emitted += 1
    return emitted


__all__ = [
    "DEDICATED_DECLARATION",
    "DEPRECATED_DECLARATIONS",
    "DeclarationSpec",
    "Deprecation",
    "collect",
    "declares_dedicated",
    "reset_warnings",
    "warn_once",
]
