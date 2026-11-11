"""Pure configuration-flow logic (no Home Assistant imports).

Everything the UI wizard needs to *decide* lives here so it can be tested on a
machine without ``homeassistant``.  The flow classes in ``config_flow.py`` are
only presentation + HTTP orchestration around these helpers.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Any

from .const import (
    COMMAND_SET_DEVICE_CONFIGURATION,
    CONF_AUTO_PLACEMENT,
    CONNECTIVITY_LEVELS,
    CT_ROLE_BRANCH,
    CT_ROLE_DEDICATED,
    CT_ROLE_MAIN,
    CT_ROLE_UNKNOWN,
    CT_ROLES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_AFTER,
    DEFAULT_UNAVAILABLE_AFTER,
    DEVICE_TYPES,
    LIGHT_TYPES,
    MAX_SCAN_INTERVAL,
    MAX_STALE_AFTER,
    MAX_UNAVAILABLE_AFTER,
    MIN_SCAN_INTERVAL,
    MIN_STALE_AFTER,
    MIN_UNAVAILABLE_AFTER,
    MONITORING_LEVELS,
    PHASE_UNKNOWN,
    PHASES,
    POLARITIES,
    POLARITY_NORMAL,
    POWER_BEHAVIORS,
)
from .state_links import apply_state_link_metadata
from .util import as_bool, as_float, as_int, normalise_base_url, sanitize_text, utcnow

#: Form field for the per-device energy-entity opt-in.  Integration-side only:
#: it is stored in the config entry, never sent to the engine.
ENERGY_ENTITY_FIELD = "create_energy_sensor"

#: Form field that links an engine device to an existing Home Assistant device.
#: Integration-side only: the engine has no concept of an HA device id, and
#: sending one would make a display decision look like electrical knowledge.
DEVICE_LINK_FIELD = "ha_device_id"

#: Default for ``CTChannel.measurement_multiplier``.  The monitor usually applies
#: its "circuit multiplier" itself and already publishes the full load, so
#: the engine must not scale again unless the user says so per circuit.
DEFAULT_MULTIPLIER = 1.0

#: A multiplier is a small integer factor in practice (the vendor app offers
#: 2 or 3).  The bound is deliberately generous but finite: it rejects a typo
#: such as 20 for 2.0 while still allowing a fractional correction.
MIN_MULTIPLIER = 0.01
MAX_MULTIPLIER = 100.0


def _as_multiplier(value: Any) -> float:
    """Coerce a stored multiplier to a float, defaulting to 1.0 when unusable.

    An out-of-range *number* is deliberately preserved rather than clamped.
    Clamping here would silently turn a typed ``200`` into ``1.0`` -- the circuit
    would then be reported at one hundredth of its load with no error shown.
    Range is a *validation* rule (:func:`validate_assignments`), so the value has
    to survive normalisation for that check to be able to see it.
    """
    number = as_float(value)
    if number is None:
        return DEFAULT_MULTIPLIER
    return float(number)

# --------------------------------------------------------------------------- #
# Connection step
# --------------------------------------------------------------------------- #


def validate_connection(base_url: Any, api_token: Any = None) -> dict[str, str]:
    """Validate the connection step.  Returns ``{field: error_key}``.

    The token is never echoed or included in an error message.
    """
    errors: dict[str, str] = {}
    if normalise_base_url(base_url) is None:
        errors["base_url"] = "invalid_url"
    if api_token is not None and not isinstance(api_token, str):
        errors["api_token"] = "invalid_token"
    return errors


def connection_defaults(data: Mapping[str, Any] | None) -> dict[str, Any]:
    data = data or {}
    return {
        "base_url": data.get("base_url", ""),
        # Never pre-fill an existing token: show a flag instead.
        "api_token": "",
        "token_configured": bool(data.get("api_token")),
    }


#: Announced port assumed when the App does not state one.  Matches the gateway
#: port in ``addon/config.yaml`` and ``run.sh``.
DEFAULT_ENGINE_PORT = 8787

#: One DNS label: no leading/trailing hyphen, at most 63 characters.
_LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
#: A bare host name (optionally fully qualified) — no scheme, path, userinfo,
#: port or whitespace.  Discovery data is untrusted, so it is matched, not
#: repaired.
_HOSTNAME_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*\.?$")
#: A bracketed IPv6 literal, which is the only host form containing ``:``.
_IPV6_RE = re.compile(r"^\[[0-9A-Fa-f:.]+\]$")
#: Names that point Home Assistant at itself rather than at an App container.
_LOOPBACK_NAMES = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)
_LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "0.0.0.0", "::1", "[::1]"})


def _is_bare_host(value: str) -> bool:
    """True if ``value`` is a host name only, with no URL syntax around it."""
    if not value or any(ch.isspace() for ch in value):
        return False
    if _IPV6_RE.match(value):
        return True
    return bool(_HOSTNAME_RE.match(value))


def discovered_base_url(config: Mapping[str, Any] | None) -> str | None:
    """The engine URL implied by a Supervisor discovery message.

    The App announces ``{"host": ..., "port": ...}``.  Home Assistant's own
    service-discovery examples put the port in the config as a string, so both
    a string and a number are accepted.  A message without a host is useless and
    returns ``None``.

    The host must be a bare host name: anything carrying a scheme, a path, a
    query, userinfo or its own port is rejected rather than reinterpreted.
    This message comes from another container, so ``user@evil.example`` must not
    become ``http://evil.example`` and ``host/path`` must not fold the port into
    a path.

    The result is normalised through the same ``normalise_base_url`` the manual
    step uses, so a discovered entry and a hand-typed entry for the same engine
    compare equal — which is what makes the unique id (and therefore the
    "already configured" check) consistent across both paths.

    No scheme is honoured from the message: discovery is only ever produced on
    the Supervisor's private network, where plain HTTP is the only option.  A
    message can therefore not downgrade or redirect an existing HTTPS setup.
    """
    if not isinstance(config, Mapping):
        return None
    host = config.get("host")
    if not isinstance(host, str):
        return None
    host = host.strip()
    if not _is_bare_host(host):
        return None
    if host.lower() in _LOOPBACK_NAMES or host.lower() in _LOOPBACK_ADDRESSES:
        # ``localhost`` would point Home Assistant at itself.
        return None
    port = config.get("port", DEFAULT_ENGINE_PORT)
    if isinstance(port, bool) or not isinstance(port, (int, str)):
        port = DEFAULT_ENGINE_PORT
    port_text = str(port).strip()
    if not port_text.isdigit() or not (1 <= int(port_text) <= 65535):
        port_text = str(DEFAULT_ENGINE_PORT)
    return normalise_base_url(f"http://{host}:{port_text}")


# --------------------------------------------------------------------------- #
# Settings step
# --------------------------------------------------------------------------- #


def validate_settings(
    scan_interval: Any,
    stale_after: Any,
    unavailable_after: Any,
) -> dict[str, str]:
    """Validate polling/threshold options.  Returns ``{field: error_key}``."""
    errors: dict[str, str] = {}

    def _check(name: str, value: Any, low: int, high: int, default: int) -> int | None:
        if value in (None, ""):
            return default
        number = as_int(value)
        if number is None:
            errors[name] = "invalid_number"
            return None
        if number < low or number > high:
            errors[name] = "out_of_range"
            return None
        return number

    interval = _check(
        "scan_interval", scan_interval, MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
    )
    stale = _check("stale_after", stale_after, MIN_STALE_AFTER, MAX_STALE_AFTER, DEFAULT_STALE_AFTER)
    unavailable = _check(
        "unavailable_after",
        unavailable_after,
        MIN_UNAVAILABLE_AFTER,
        MAX_UNAVAILABLE_AFTER,
        DEFAULT_UNAVAILABLE_AFTER,
    )

    if interval is not None and stale is not None and stale < interval:
        # A stale threshold below the poll interval would flap every cycle.
        errors["stale_after"] = "stale_below_interval"
    if unavailable is not None and stale is not None and unavailable < stale:
        errors["unavailable_after"] = "unavailable_below_stale"
    return errors


def settings_defaults(config: Mapping[str, Any] | None) -> dict[str, Any]:
    config = config or {}
    return {
        "scan_interval": as_int(config.get("scan_interval")) or DEFAULT_SCAN_INTERVAL,
        "stale_after": as_int(config.get("stale_after")) or DEFAULT_STALE_AFTER,
        "unavailable_after": as_int(config.get("unavailable_after"))
        or DEFAULT_UNAVAILABLE_AFTER,
    }


def normalise_settings(data: Mapping[str, Any]) -> dict[str, int]:
    """Return validated settings (assumes :func:`validate_settings` passed)."""
    defaults = settings_defaults(data)
    return {
        "scan_interval": as_int(data.get("scan_interval")) or defaults["scan_interval"],
        "stale_after": as_int(data.get("stale_after")) or defaults["stale_after"],
        "unavailable_after": as_int(data.get("unavailable_after"))
        or defaults["unavailable_after"],
    }


def auto_placement_enabled(config: Mapping[str, Any] | None) -> bool:
    """Whether the user has opted in to WattMosaic creating devices by itself.

    Off unless explicitly enabled, and only ``True`` counts.  A missing key, a
    string, or any other value is *not* consent: this flag authorises a write to
    the configuration store, so the default has to fail closed.
    """
    config = config or {}
    return as_bool(config.get(CONF_AUTO_PLACEMENT), default=False)


def placement_request_parameters(
    config: Mapping[str, Any] | None,
    parameters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Add the user's stored auto-placement choice to a request.

    The engine's own flag is an environment variable fixed at launch, so it
    cannot reflect a checkbox the user just ticked.  The integration therefore
    states the decision explicitly on every call.

    An explicit ``apply`` in the request always wins: a caller who asked for
    analysis only must not get writes because a stored option says so.  When it
    is absent the stored option decides, and the engine still refuses to write if
    neither grants permission -- two independent no's are better than one.
    """
    merged = dict(parameters or {})
    if merged.get("apply") is None:
        merged["apply"] = auto_placement_enabled(config)
    return merged


# --------------------------------------------------------------------------- #
# Topology / channel normalisation
# --------------------------------------------------------------------------- #


def normalise_channels(topology: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Extract a stable, ordered channel list from a ``Topology`` payload."""
    if not isinstance(topology, Mapping):
        return []
    raw = topology.get("channels")
    if not isinstance(raw, list):
        return []
    channels: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        ct_id = item.get("ct_id")
        if not isinstance(ct_id, str) or not ct_id.strip() or ct_id in seen:
            continue
        seen.add(ct_id)
        channels.append(
            {
                "ct_id": ct_id,
                "entity_id": str(item.get("entity_id") or ""),
                "name": str(item.get("name") or ct_id),
                "ct_type": str(item.get("ct_type") or CT_ROLE_UNKNOWN),
                "phase": str(item.get("phase") or PHASE_UNKNOWN),
                "polarity": str(item.get("polarity") or POLARITY_NORMAL),
                "device_id": item.get("device_id") if isinstance(item.get("device_id"), str) else None,
                "paired_ct_id": item.get("paired_ct_id")
                if isinstance(item.get("paired_ct_id"), str)
                else None,
                "parent_ct_id": item.get("parent_ct_id")
                if isinstance(item.get("parent_ct_id"), str)
                else None,
                "alias_of_ct_id": item.get("alias_of_ct_id")
                if isinstance(item.get("alias_of_ct_id"), str)
                else None,
                "measurement_multiplier": _as_multiplier(item.get("measurement_multiplier")),
                "enabled": bool(item.get("enabled", True)),
                "topology_status": str(item.get("topology_status") or "candidate"),
            }
        )
    return channels


def is_double_pole_ct(channel: Mapping[str, Any]) -> bool:
    """Heuristic used only to *suggest* pairing, never to commit it."""
    ct_id = str(channel.get("ct_id") or "")
    name = str(channel.get("name") or "")
    return str(channel.get("phase") or "") == "both" or bool(
        re.search(r"(?i)(?:^|[_\s-])ct(?:1|2)(?:$|[_\s-])", ct_id + " " + name)
    )


def _raw_sibling(ct_id: str, present: Collection[str]) -> str | None:
    """The naive 1<->2 counterpart of a channel id, or ``None``.

    Only legs 1 and 2 are ever paired.  A third leg (``ct3``) is a separate
    circuit, not half of a 240 V load -- defect #28 is precisely that mistake
    made in the other direction.  Anything that is not a 1 or a 2 therefore
    has no counterpart at all.
    """
    # Preferred: an explicit ``ctN`` marker anywhere in the id, which covers
    # real HA entity ids such as ``panel_a_circuit_ct1_power``.
    match = re.search(r"(?i)ct([1-9])(?![0-9])", ct_id)
    if match:
        leg = match.group(1)
        if leg not in ("1", "2"):
            return None
        other = "2" if leg == "1" else "1"
        sibling = ct_id[: match.start()] + f"ct{other}" + ct_id[match.end() :]
        return sibling if sibling in present else None

    # Fallback: a trailing integer, e.g. ``channel_1`` <-> ``channel_2``.
    match = re.search(r"(?i)(.*?)(\d+)$", ct_id)
    if not match:
        return None
    prefix, number = match.group(1), match.group(2)
    if number not in ("1", "2"):
        return None
    sibling = f"{prefix}{2 if number == '1' else 1}"
    return sibling if sibling in present else None


def suggested_pair(channel: Mapping[str, Any], channels: Sequence[Mapping[str, Any]]) -> str | None:
    """Suggest the sibling CT of a likely double-pole pair (CT1 <-> CT2).

    A suggestion is only offered when it is *reciprocal*.  ``validate_assignments``
    rejects a one-sided pair, and the wizard renders every channel's default from
    this function, so a non-reciprocal guess would make the confirm step unable
    to accept its own defaults -- a dead end the user cannot escape, because the
    fix would be to know which channel to edit by hand.
    """
    ct_id = str(channel.get("ct_id") or "")
    by_id = {str(c.get("ct_id") or ""): c for c in channels}
    present = set(by_id)

    declared = channel.get("paired_ct_id")
    if declared:
        partner = by_id.get(str(declared))
        if partner is not None and str(partner.get("paired_ct_id") or "") == ct_id:
            return str(declared)
        # A declared pair the other side does not repeat is dropped here; the
        # structural guess below may still find a legitimate partner.

    sibling = _raw_sibling(ct_id, present)
    if sibling is None:
        return None
    return sibling if _raw_sibling(sibling, present) == ct_id else None


def descendant_ids(ct_id: str, channels: Sequence[Mapping[str, Any]]) -> set[str]:
    """All CT ids that sit (transitively) under ``ct_id`` in the given channels.

    Used by the wizard to keep a cycle out of the parent dropdown: a CT's own
    descendant can never be offered as its parent.
    """
    children: dict[str, list[str]] = {}
    for channel in channels:
        cid = str(channel.get("ct_id") or "")
        parent = channel.get("parent_ct_id")
        if cid and isinstance(parent, str) and parent:
            children.setdefault(parent, []).append(cid)
    out: set[str] = set()
    stack = list(children.get(ct_id, ()))
    while stack:
        current = stack.pop()
        if current in out:
            continue
        out.add(current)
        stack.extend(children.get(current, ()))
    return out


def nesting_candidates(channel: Mapping[str, Any]) -> list[str]:
    """Parent CT ids that discovery *proposed* for this channel.

    Discovery writes them to ``metadata.nesting_candidates`` after measuring
    containment and closure between channels.  They are proposals only: a
    candidate never becomes a parent link until the user accepts it in the
    wizard, which keeps an inferred wiring fact out of the confirmed topology.
    """
    meta = channel.get("metadata")
    if not isinstance(meta, Mapping):
        return []
    raw = meta.get("nesting_candidates")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item]


def suggested_parent(
    channel: Mapping[str, Any],
    channels: Sequence[Mapping[str, Any]],
) -> str | None:
    """The single credible parent to pre-select, or ``None``.

    A suggestion is only made when discovery found exactly one candidate and that
    candidate is not the channel's own descendant (which would be a cycle).  Two
    competing candidates mean the evidence is ambiguous, and pre-selecting either
    one would present a guess as a measurement -- so the wizard leaves the field
    empty and the user decides.
    """
    ct_id = str(channel.get("ct_id") or "")
    candidates = nesting_candidates(channel)
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    if candidate == ct_id or candidate in descendant_ids(ct_id, channels):
        return None
    return candidate


def alias_candidates(channel: Mapping[str, Any]) -> list[str]:
    """CT ids that discovery proposed as the *same conductor* as this one.

    Recorded in ``metadata.alias_candidates``.  An alias is not a parent: the
    channel still measures a real wire, but another CT already measures that same
    wire, so summing both would double count.  Kept separate from
    ``nesting_candidates`` so the two questions never get confused.
    """
    meta = channel.get("metadata")
    if not isinstance(meta, Mapping):
        return []
    raw = meta.get("alias_candidates")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item]


def suggested_alias(
    channel: Mapping[str, Any],
    channels: Sequence[Mapping[str, Any]],
) -> str | None:
    """The single credible alias to pre-select, or ``None``.

    Same rule as ``suggested_parent``: only an unambiguous, non-self, non-cyclic
    proposal is pre-selected.  A declared pairing or declared parent already
    answers the question, so it suppresses the suggestion rather than competing
    with it.
    """
    ct_id = str(channel.get("ct_id") or "")
    if channel.get("paired_ct_id") or channel.get("parent_ct_id"):
        return None
    candidates = alias_candidates(channel)
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    if candidate == ct_id or candidate in descendant_ids(ct_id, channels):
        return None
    return candidate


def apply_nesting_candidates(
    topology: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach discovery's *proposed* parents and aliases to a copy of the topology.

    The proposals are stored in each channel's ``metadata`` only.
    ``parent_ct_id`` and ``alias_of_ct_id`` are left untouched, so nothing is
    applied: the wizard can pre-select a single unambiguous candidate and the
    user confirms it.

    Two kinds of proposal are recorded, because they answer different questions:

    ``nesting_candidates``
        "what feeds this CT?"  A panel feed that stays inside a container.
    ``alias_candidates``
        "is this CT the same conductor as another?"  A duplicate feed, which must
        not be summed as a second feed.  This is *not* a nesting: a nested feed
        is still a real, separate measurement below its parent, while an alias is
        the same wire seen twice.
    """
    result = dict(topology or {})
    raw_channels = result.get("channels")
    if not isinstance(raw_channels, list):
        return result
    proposals = (payload or {}).get("candidate_parents")
    aliases = (payload or {}).get("candidate_aliases")
    if not isinstance(proposals, Mapping) and not isinstance(aliases, Mapping):
        return result
    channels: list[dict[str, Any]] = []
    for item in raw_channels:
        if not isinstance(item, Mapping):
            continue
        channel = dict(item)
        ct_id = str(channel.get("ct_id") or "")
        parents = proposals.get(ct_id) if isinstance(proposals, Mapping) else None
        alias = aliases.get(ct_id) if isinstance(aliases, Mapping) else None
        metadata = dict(channel.get("metadata") or {})
        if isinstance(parents, list) and parents:
            metadata["nesting_candidates"] = [str(p) for p in parents]
        if isinstance(alias, str) and alias:
            metadata["alias_candidates"] = [alias]
        if metadata != dict(channel.get("metadata") or {}):
            channel["metadata"] = metadata
        channels.append(channel)
    result["channels"] = channels
    return result


def assignment_defaults(
    channel: Mapping[str, Any],
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Form defaults for one CT, never inventing a confirmed mapping."""
    existing = existing or {}
    return {
        "role": str(existing.get("role") or channel.get("ct_type") or CT_ROLE_UNKNOWN),
        "phase": str(existing.get("phase") or channel.get("phase") or PHASE_UNKNOWN),
        "polarity": str(existing.get("polarity") or channel.get("polarity") or POLARITY_NORMAL),
        "paired_ct_id": existing.get("paired_ct_id")
        if existing.get("paired_ct_id") is not None
        else (channel.get("paired_ct_id") or ""),
        "parent_ct_id": existing.get("parent_ct_id")
        if existing.get("parent_ct_id") is not None
        else (channel.get("parent_ct_id") or ""),
        "alias_of_ct_id": existing.get("alias_of_ct_id")
        if existing.get("alias_of_ct_id") is not None
        else (channel.get("alias_of_ct_id") or ""),
        "measurement_multiplier": _as_multiplier(
            existing.get("measurement_multiplier", channel.get("measurement_multiplier"))
        ),
        "device_id": existing.get("device_id")
        if existing.get("device_id") is not None
        else (channel.get("device_id") or ""),
        "enabled": bool(existing.get("enabled", channel.get("enabled", True))),
    }


#: Roles that may contain another CT.  A ``dedicated_device`` CT watches one
#: load, so nothing can sit behind it; an ``unknown`` role is likewise not a
#: container.  ``main`` may nest to any depth (service -> panel -> sub-panel).
CONTAINER_ROLES: tuple[str, ...] = (CT_ROLE_MAIN, "branch")


def normalise_assignment(
    submitted: Mapping[str, Any] | None,
    channel: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Coerce one submitted CT assignment into the stored shape."""
    submitted = submitted or {}
    channel = channel or {}
    role = str(submitted.get("role") or channel.get("ct_type") or CT_ROLE_UNKNOWN).lower()
    phase = str(submitted.get("phase") or channel.get("phase") or PHASE_UNKNOWN).lower()
    polarity = str(submitted.get("polarity") or channel.get("polarity") or POLARITY_NORMAL).lower()
    if role not in CT_ROLES:
        role = CT_ROLE_UNKNOWN
    if phase not in PHASES:
        phase = PHASE_UNKNOWN
    if polarity not in POLARITIES:
        polarity = POLARITY_NORMAL

    def _clean(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    return {
        "role": role,
        "phase": phase,
        "polarity": polarity,
        "paired_ct_id": _clean(submitted.get("paired_ct_id")),
        "parent_ct_id": _clean(submitted.get("parent_ct_id")),
        "alias_of_ct_id": _clean(submitted.get("alias_of_ct_id")),
        "measurement_multiplier": _as_multiplier(
            submitted.get("measurement_multiplier", channel.get("measurement_multiplier"))
        ),
        "device_id": _clean(submitted.get("device_id")),
        "enabled": bool(submitted.get("enabled", channel.get("enabled", True))),
    }


def _parent_cycle(ct_id: str, parents: Mapping[str, str | None]) -> bool:
    """Whether following ``parent`` links from ``ct_id`` returns to it."""
    seen = {ct_id}
    current = parents.get(ct_id)
    while current:
        if current in seen:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


def _alias_cycle(ct_id: str, aliases: Mapping[str, str | None]) -> bool:
    """Whether following ``alias_of`` links from ``ct_id`` returns to it."""
    seen = {ct_id}
    current = aliases.get(ct_id)
    while current:
        if current in seen:
            return True
        seen.add(current)
        current = aliases.get(current)
    return False


def validate_assignments(
    assignments: Mapping[str, Mapping[str, Any]],
    channels: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Validate the whole assignment set.  Returns user-facing error keys.

    Rules (documented in ``docs/integration.md``):

    * a CT cannot pair with itself
    * a ``main`` CT needs a concrete phase (l1/l2/both)
    * a ``dedicated_device`` CT needs a device
    * pairing is reciprocal
    * ``both`` phase requires a paired CT (double-pole halves)
    * a parent CT must exist, must be a container, and must not form a cycle
    * an ``alias_of_ct_id`` must exist, must not be self, and must not cycle
    * a multiplier must be a positive finite number

    Nesting is *not* an error at any depth: a main CT may sit under another
    main CT (service -> panel -> sub-panel), and the engine counts only the
    outermost feed of each branch.  What is rejected is a structurally
    impossible claim -- a parent that does not exist, a parent that is itself a
    single device's CT, or a loop.
    """
    errors: list[str] = []
    known = {str(c.get("ct_id")) for c in channels} if channels else None
    normalised = {str(ct_id): normalise_assignment(raw) for ct_id, raw in assignments.items()}

    roles = {ct_id: a["role"] for ct_id, a in normalised.items()}
    parents: dict[str, str | None] = {ct_id: a["parent_ct_id"] for ct_id, a in normalised.items()}
    aliases: dict[str, str | None] = {
        ct_id: a["alias_of_ct_id"] for ct_id, a in normalised.items()
    }

    for ct_id, assignment in normalised.items():
        role = assignment["role"]
        phase = assignment["phase"]
        pair = assignment["paired_ct_id"]
        device = assignment["device_id"]

        if known is not None and ct_id not in known:
            errors.append(f"unknown_channel:{ct_id}")
            continue
        if role == CT_ROLE_MAIN and phase == PHASE_UNKNOWN:
            errors.append(f"main_needs_phase:{ct_id}")
        if role == CT_ROLE_DEDICATED and not device:
            errors.append(f"dedicated_needs_device:{ct_id}")
        if pair and pair == ct_id:
            errors.append(f"self_pair:{ct_id}")
        if pair:
            other = assignments.get(pair)
            if other is None:
                errors.append(f"pair_missing:{ct_id}:{pair}")
            else:
                other_assignment = normalise_assignment(other)
                if other_assignment["paired_ct_id"] != ct_id:
                    errors.append(f"pair_not_reciprocal:{ct_id}:{pair}")
                elif (
                    role == CT_ROLE_DEDICATED
                    and other_assignment["role"] == CT_ROLE_DEDICATED
                    and device
                    and other_assignment["device_id"]
                    and other_assignment["device_id"] != device
                ):
                    # A pair is the two legs of ONE double-pole load.  Pairing
                    # two different devices would make each report the other's
                    # power as well: the engine sums a declared pair, so the
                    # same watts would be attributed to both.
                    errors.append(f"pair_crosses_devices:{ct_id}:{pair}")
        if phase == "both" and not pair:
            errors.append(f"both_needs_pair:{ct_id}")

        parent = assignment["parent_ct_id"]
        if parent is not None:
            if known is not None and parent not in known:
                errors.append(f"parent_unknown:{ct_id}:{parent}")
            elif parent == ct_id:
                errors.append(f"parent_self:{ct_id}")
            elif not _is_container(parent, roles.get(parent)):
                # Parent is declared in this same submission with a role that
                # cannot contain anything (a single device's CT, or unknown).
                errors.append(f"parent_not_container:{ct_id}:{parent}")
            elif _parent_cycle(ct_id, parents):
                errors.append(f"parent_cycle:{ct_id}")

        alias = assignment["alias_of_ct_id"]
        if alias is not None:
            if known is not None and alias not in known:
                errors.append(f"alias_unknown:{ct_id}:{alias}")
            elif alias == ct_id:
                errors.append(f"alias_self:{ct_id}")
            elif _alias_cycle(ct_id, aliases):
                errors.append(f"alias_cycle:{ct_id}")

        multiplier = assignment["measurement_multiplier"]
        if not isinstance(multiplier, (int, float)) or not (
            MIN_MULTIPLIER <= float(multiplier) <= MAX_MULTIPLIER
        ):
            errors.append(f"multiplier_out_of_range:{ct_id}")

    return errors


def _is_container(ct_id: str, role: str | None) -> bool:
    """Whether a CT with ``role`` may be declared as another CT's parent.

    When the parent's role is unknown (``None``) the check is skipped: the
    wizard may not have reached that channel yet, and blocking a *forward*
    reference would make the step order matter in a way the user cannot see.
    """
    if role is None:
        return True
    return role in CONTAINER_ROLES


def resolve_confirmation(
    validation_errors: Sequence[str],
    uncertain: Sequence[str],
    confirm_uncertain: bool,
) -> tuple[bool | None, str | None]:
    """Decide the outcome of the confirm step: ``(confirmed, error_key)``.

    ``confirmed is None`` means the step must be re-shown with ``error_key``.

    Submitting the form *is* an explicit user action, so an unticked box does
    not block the wizard: it stores the assignments and leaves the topology
    ``candidate``, which is what the documentation promises.  Refusing the
    submission instead would leave no way out whenever any channel is an
    assumption (an unknown phase, say) — and the only exit would be a blanket
    confirmation of every uncertain channel, which is precisely the false
    confirmation this project treats as worse than none.

    The box therefore only ever *adds* confirmation; it is never required in
    order to finish.
    """
    if validation_errors:
        return None, "invalid_mapping"
    return (not uncertain) or bool(confirm_uncertain), None


def is_uncertain(assignment: Mapping[str, Any]) -> bool:
    """Whether an assignment needs explicit user confirmation."""
    normalised = normalise_assignment(assignment)
    return (
        normalised["role"] == CT_ROLE_UNKNOWN
        or normalised["phase"] == PHASE_UNKNOWN
        or (normalised["role"] == CT_ROLE_DEDICATED and not normalised["device_id"])
    )


def uncertainty_summary(assignments: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Human-readable list of assignments that are still assumptions."""
    uncertain: list[str] = []
    for ct_id, raw in sorted(assignments.items()):
        if is_uncertain(raw):
            normalised = normalise_assignment(raw)
            reasons = []
            if normalised["role"] == CT_ROLE_UNKNOWN:
                reasons.append("role unknown")
            if normalised["phase"] == PHASE_UNKNOWN:
                reasons.append("phase unknown")
            if normalised["role"] == CT_ROLE_DEDICATED and not normalised["device_id"]:
                reasons.append("no device")
            uncertain.append(f"{ct_id}: " + ", ".join(reasons))
    return uncertain


def build_topology_update(
    topology: Mapping[str, Any] | None,
    assignments: Mapping[str, Mapping[str, Any]],
    *,
    confirmed: bool = False,
    notes: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Merge assignments into a copy of the engine topology for ``PUT``.

    Unassigned channels keep their previous values.  The result stays
    ``candidate`` unless the caller explicitly confirms.
    """
    topology = dict(topology or {})
    raw_channels = topology.get("channels")
    channels: list[dict[str, Any]] = []
    if isinstance(raw_channels, list):
        for item in raw_channels:
            if not isinstance(item, Mapping):
                continue
            channel = dict(item)
            ct_id = channel.get("ct_id")
            assignment = assignments.get(str(ct_id)) if ct_id is not None else None
            if assignment is not None:
                normalised = normalise_assignment(assignment, channel)
                channel.update(
                    {
                        "ct_type": normalised["role"],
                        "phase": normalised["phase"],
                        "polarity": normalised["polarity"],
                        "paired_ct_id": normalised["paired_ct_id"],
                        "parent_ct_id": normalised["parent_ct_id"],
                        "alias_of_ct_id": normalised["alias_of_ct_id"],
                        "measurement_multiplier": normalised["measurement_multiplier"],
                        "device_id": normalised["device_id"],
                        "enabled": normalised["enabled"],
                        "topology_status": "candidate",
                    }
                )
            channels.append(channel)
    topology["channels"] = channels

    # Mirror dedicated CTs onto device topology entries (engine contract:
    # DeviceTopology.dedicated_ct_ids).
    #
    # This is a *mirror*, not an append-only note.  A CT the user demotes from
    # ``dedicated_device`` to ``branch`` no longer belongs to the device that used
    # to own it, and leaving the id behind makes the graph reject it
    # ("dedicated CT ... has type BRANCH, not dedicated_device") while the device
    # silently keeps zero attribution.  So every device's mirrored list is
    # rebuilt from the assignment that is being submitted, not merged into.
    raw_devices = topology.get("devices")
    if isinstance(raw_devices, list):
        devices: list[dict[str, Any]] = []
        for item in raw_devices:
            if not isinstance(item, Mapping):
                continue
            device = dict(item)
            device_id = device.get("device_id")
            # Snapshot before the dedicated mirror rewrites it: a CT that this
            # submission demotes must still be visible afterwards so it can move
            # to ``branch_ct_ids`` rather than vanishing.
            previous_dedicated = [str(c) for c in (device.get("dedicated_ct_ids") or [])]
            dedicated = [
                ct_id
                for ct_id, raw in assignments.items()
                if normalise_assignment(raw)["role"] == CT_ROLE_DEDICATED
                and normalise_assignment(raw)["device_id"] == device_id
            ]
            # Channels this wizard step did not touch keep whatever they had, so
            # a partial submission cannot drop a mapping the user made earlier.
            if dedicated:
                device["dedicated_ct_ids"] = dedicated
                if str(device.get("topology_status") or "candidate") == "candidate":
                    device.setdefault("metadata", {})
            else:
                # The device may still have a dedicated CT from an untouched
                # channel; drop only ids this submission demoted.
                existing = [
                    str(c)
                    for c in (device.get("dedicated_ct_ids") or [])
                    if assignments.get(str(c)) is None
                    or normalise_assignment(assignments[str(c)])["role"]
                    == CT_ROLE_DEDICATED
                ]
                if existing:
                    device["dedicated_ct_ids"] = existing
                else:
                    device.pop("dedicated_ct_ids", None)

            # A CT demoted from ``dedicated_device`` to ``branch`` still measures
            # the same circuit, so the device must keep that row as a *branch*
            # attribution.  Dropping it from ``dedicated_ct_ids`` without adding
            # it to ``branch_ct_ids`` leaves the device with no row at all: the
            # graph resolves zero attribution and the fixture silently reports
            # nothing forever.  ``branch_ct_ids`` is the mirror discovery writes
            # for a shared circuit, so the demotion lands on that same shape.
            branch = [str(c) for c in (device.get("branch_ct_ids") or [])]
            for cid in previous_dedicated:
                raw = assignments.get(cid)
                if raw is None:
                    continue
                normalised = normalise_assignment(raw)
                if normalised["role"] == CT_ROLE_BRANCH and cid not in branch:
                    branch.append(cid)
            # A row this submission reassigned elsewhere, or turned into a feed,
            # no longer attributes this device.
            retained: list[str] = []
            for cid in branch:
                raw = assignments.get(cid)
                normalised = normalise_assignment(raw) if raw is not None else None
                if normalised is not None and (
                    normalised["role"] in (CT_ROLE_MAIN, CT_ROLE_UNKNOWN)
                    or (
                        normalised["role"] == CT_ROLE_DEDICATED
                        and normalised["device_id"] is not None
                        and normalised["device_id"] != device_id
                    )
                ):
                    continue
                retained.append(cid)
            if retained:
                device["branch_ct_ids"] = retained
            else:
                device.pop("branch_ct_ids", None)
            devices.append(device)
        topology["devices"] = devices

    if confirmed:
        topology["status"] = "confirmed"
        for channel in channels:
            channel["topology_status"] = "confirmed"
        for device in topology.get("devices") or []:
            if isinstance(device, dict):
                device["topology_status"] = "confirmed"
    else:
        topology["status"] = "candidate"

    extra_notes = list(notes or [])
    if extra_notes:
        existing_notes = topology.get("notes")
        topology["notes"] = list(existing_notes or []) + extra_notes
    return topology


# --------------------------------------------------------------------------- #
# Replaying the stored declaration on setup
# --------------------------------------------------------------------------- #

#: Assignment key -> engine channel key for the fields a user declaration owns.
#: Anything not listed here (``ct_id``, ``entity_id``, ``name``, ...) belongs to
#: the engine's discovery and is deliberately never compared.
DECLARED_CHANNEL_FIELDS: dict[str, str] = {
    "role": "ct_type",
    "phase": "phase",
    "polarity": "polarity",
    "paired_ct_id": "paired_ct_id",
    "parent_ct_id": "parent_ct_id",
    "alias_of_ct_id": "alias_of_ct_id",
    "measurement_multiplier": "measurement_multiplier",
    "device_id": "device_id",
    "enabled": "enabled",
}

#: Note appended to a topology the integration replays, so the engine's own
#: history explains where the nesting came from.
TOPOLOGY_REPLAY_NOTE = (
    "Re-applied the Home Assistant topology declaration "
    "(user-confirmed CT assignments)."
)

#: Note appended when the replay also carried resolved device-link state ids.
TOPOLOGY_LINK_NOTE = (
    "Re-applied the resolved device-link state entities "
    "(device_links -> link_state_entity_ids)."
)

#: Upper bound on one replay attempt.  A write must never hold up setup or a
#: poll longer than this; the client has its own per-request timeout, this is
#: the belt-and-braces bound around the whole read+write+confirm sequence.  It
#: is a *bound*, not a delay: an in-sync install returns immediately without
#: touching the network.
TOPOLOGY_PUSH_TIMEOUT = 15.0

#: How many times the *identical* declaration may be written without the engine
#: reflecting it.  An engine that accepts the PUT but drops ``parent_ct_id``
#: would otherwise be rewritten on every poll; two attempts are enough to ride
#: out a restart, and the second failure is reported instead of hidden.
MAX_IDENTICAL_PUSHES = 2

#: Re-verify the declaration every this many polls even when the engine's
#: version has not changed.  Version-only guards have one blind spot: a database
#: reset that happens to recreate the *same* version number.  One read-only
#: ``GET /topology`` per this many polls closes it for a fraction of a request
#: per minute.
TOPOLOGY_RECHECK_POLLS = 60

_LOGGER = logging.getLogger(__name__)


def declaration_signature(
    assignments: Mapping[str, Mapping[str, Any]] | None,
    confirmed: bool,
) -> str:
    """Stable fingerprint of a declaration, used to bound identical rewrites."""
    normalised = {
        str(ct_id): normalise_assignment(raw)
        for ct_id, raw in (assignments or {}).items()
        if isinstance(raw, Mapping)
    }
    return f"{int(bool(confirmed))}:" + json.dumps(normalised, sort_keys=True)


@dataclass
class TopologyPushPlan:
    """Whether setup should replay the stored declaration, and why.

    ``push is False`` always carries a ``reason`` so the decision can be
    audited (diagnostics show it next to the engine's current version).
    """

    push: bool
    reason: str
    assignments: dict[str, dict[str, Any]] = dataclass_field(default_factory=dict)
    confirmed: bool = False
    engine_version: int | None = None
    #: The engine already reports this topology as confirmed.  A rewrite of the
    #: *declared fields* must then stay confirmed: demoting it to ``candidate``
    #: would silently disable learning until the user ran the wizard again.
    preserve_confirmed: bool = False


def _channel_value(channel: Mapping[str, Any], key: str) -> Any:
    """One engine channel field in the same shape as a normalised assignment."""
    raw = channel.get(key)
    if key == "measurement_multiplier":
        return _as_multiplier(raw)
    if key == "enabled":
        return bool(raw) if raw is not None else True
    if key in ("paired_ct_id", "parent_ct_id", "alias_of_ct_id", "device_id"):
        return raw.strip() if isinstance(raw, str) and raw.strip() else None
    if key == "ct_type":
        return str(raw or CT_ROLE_UNKNOWN).strip().lower()
    return str(raw or "").strip().lower()


def assignment_matches_channel(
    assignment: Mapping[str, Any], channel: Mapping[str, Any]
) -> bool:
    """Whether one stored assignment already describes an engine channel.

    Only the *declared* fields are compared: a difference in entity id, name or
    metrics is the engine's business, and treating it as a mismatch would make
    the integration rewrite the topology on every poll.
    """
    normalised = normalise_assignment(assignment, channel)
    for assignment_key, channel_key in DECLARED_CHANNEL_FIELDS.items():
        expected = _channel_value(
            {channel_key: normalised[assignment_key]}, channel_key
        )
        actual = _channel_value(channel, channel_key)
        if assignment_key == "measurement_multiplier":
            if abs(float(expected) - float(actual)) > 1e-9:
                return False
        elif expected != actual:
            return False
    return True


def assignments_match_topology(
    topology: Mapping[str, Any] | None,
    assignments: Mapping[str, Mapping[str, Any]] | None,
) -> bool:
    """Whether every declared channel the engine knows already matches.

    Channels that exist only on one side are ignored: the engine may report CTs
    the wizard never saw, and a declared CT the engine dropped cannot be
    re-created by a ``PUT`` (that would be fabricating a channel), so it is not
    a reason to rewrite the topology.
    """
    channels = {c["ct_id"]: c for c in normalise_channels(topology)}
    for ct_id, raw in (assignments or {}).items():
        channel = channels.get(str(ct_id))
        if channel is None:
            continue
        if not assignment_matches_channel(raw, channel):
            return False
    return True


def plan_topology_push(
    topology: Mapping[str, Any] | None,
    assignments: Mapping[str, Mapping[str, Any]] | None,
    *,
    confirmed: bool = False,
    stored_version: Any = None,
    links_changed: bool = False,
) -> TopologyPushPlan:
    """Decide whether the stored declaration must be replayed onto ``topology``.

    The declaration in the config entry is *user intent*: the wizard recorded
    what the user confirmed, and a database reset or a discovery-driven
    bootstrap that replaces the topology with a parentless candidate must not
    erase it.  Replaying it is not an inference -- every field written here was
    chosen by the user -- so this never fabricates a nesting decision.

    ``links_changed`` says the caller has already resolved a ``device_links``
    entry into explicit state entity ids and the engine topology does not carry
    them yet.  That is a second, independent reason to write: a link that never
    reaches the engine leaves the learning intake empty, which is how a live
    install reported ``samples_seen: 0`` for days.  It still requires the engine
    to have devices to write them onto; a topology with none cannot receive a
    link and inventing one would be fabrication.

    A push is skipped when

    * there is no declaration and no link change;
    * the engine reports no channels (there is nothing to merge onto -- inventing
      a channel would be fabrication);
    * no declared CT id exists in the engine topology;
    * the engine already matches the declaration at the same version, or the
      engine is *confirmed* at a **newer** version and the declaration did not
      change (never downgrade a newer confirmed topology).

    A push is performed when the declaration differs from the engine, when the
    engine is behind the stored version, when the stored entry says the user
    confirmed it and the engine still reports ``candidate``, or when a resolved
    device link has not reached the engine yet.
    """
    stored = {
        str(ct_id): dict(raw)
        for ct_id, raw in (assignments or {}).items()
        if isinstance(raw, Mapping)
    }
    devices = [
        device for device in (topology.get("devices") or []) if isinstance(device, Mapping)
    ] if isinstance(topology, Mapping) else []
    if not stored and not links_changed:
        return TopologyPushPlan(push=False, reason="no_assignments")

    engine_version = as_int(topology.get("version")) if isinstance(topology, Mapping) else None
    channels = normalise_channels(topology)
    # Read before any branch: a link-only write must not demote a confirmed
    # topology.  ``build_topology_update(confirmed=False)`` sets ``candidate``,
    # which would silently disable learning on an install that had already
    # confirmed its rows -- the exact failure ADR-0008 exists to prevent.
    engine_status = str((topology or {}).get("status") or "candidate").strip().lower()
    engine_confirmed = engine_status == "confirmed"

    if links_changed and not devices:
        # Nothing to carry the resolved ids.  Report it rather than writing a
        # no-op revision the engine would accept and the user could not see.
        return TopologyPushPlan(
            push=False, reason="no_devices_for_state_links", engine_version=engine_version
        )

    if links_changed and not stored:
        # A link-only declaration: the channels are the engine's own discovery,
        # and only the state entities are being (re)applied.
        return TopologyPushPlan(
            push=True,
            reason="state_links_missing",
            assignments={},
            confirmed=bool(confirmed) or engine_confirmed,
            engine_version=engine_version,
            preserve_confirmed=bool(confirmed) or engine_confirmed,
        )

    if not channels:
        return TopologyPushPlan(
            push=False, reason="engine_topology_empty", engine_version=engine_version
        )

    known = {channel["ct_id"] for channel in channels}
    if not any(ct_id in known for ct_id in stored):
        if links_changed:
            # The declaration names no channel this engine knows, but the
            # resolved state entities still belong on the devices that do exist.
            return TopologyPushPlan(
                push=True,
                reason="state_links_missing",
                assignments=stored,
                confirmed=bool(confirmed),
                engine_version=engine_version,
            )
        return TopologyPushPlan(
            push=False, reason="no_matching_channels", engine_version=engine_version
        )

    matches = assignments_match_topology(topology, stored)
    declared_version = as_int(stored_version)
    version_known = engine_version is not None and declared_version is not None
    same_version = version_known and engine_version == declared_version
    needs_confirmation = bool(confirmed) and not engine_confirmed

    if matches and not needs_confirmation:
        if links_changed:
            # The declaration already matches; only the resolved device-link
            # state entities are missing from the engine.  Writing them is what
            # turns a silent intake into a working one.
            return TopologyPushPlan(
                push=True,
                reason="state_links_missing",
                assignments=stored,
                confirmed=bool(confirmed) or engine_confirmed,
                engine_version=engine_version,
                preserve_confirmed=engine_confirmed,
            )
        if version_known and engine_version > declared_version:
            return TopologyPushPlan(
                push=False,
                reason="engine_newer_confirmed" if engine_confirmed else "engine_newer",
                engine_version=engine_version,
            )
        return TopologyPushPlan(
            push=False,
            reason="in_sync" if same_version else "engine_matches",
            engine_version=engine_version,
        )

    return TopologyPushPlan(
        push=True,
        reason="confirmation_missing" if matches else "declaration_missing",
        assignments=stored,
        # Never downgrade: if the engine already holds a confirmed topology, a
        # rewrite of the declared fields (or of the entry's confirmation flag)
        # must stay confirmed -- a candidate would silently disable learning.
        confirmed=bool(confirmed) or engine_confirmed,
        engine_version=engine_version,
        preserve_confirmed=engine_confirmed,
    )


@dataclass
class TopologyPushState:
    """Outcome of the last replay attempt, surfaced through diagnostics.

    Everything here is safe to show a user: no token, no URL, only counts,
    versions and the scrubbed error text.
    """

    #: ``True`` once a replay has been accepted by the engine at least once.
    pushed: bool = False
    #: Engine topology version this integration's last successful push produced.
    pushed_version: int | None = None
    #: Version stored in the config entry when the push was made.
    declared_version: int | None = None
    #: Scrubbed last error, or ``None`` after a success.
    error: str | None = None
    #: Total attempts since setup (a failure retries on the next poll).
    attempts: int = 0
    #: When the last *successful* push happened.
    pushed_at: datetime | None = None
    #: Stable reason key for the last decision (``in_sync``, ``declaration_missing``...).
    reason: str = "not_attempted"
    #: Engine version observed when the last decision was taken.
    observed_version: int | None = None
    #: Engine version this integration has already reconciled against.  The
    #: coordinator re-checks only when the engine reports a different version
    #: (a reset or a bootstrap replaced the topology) or the last attempt failed.
    verified_version: int | None = None
    #: The ``confirmed`` flag of the request that produced ``verified_version``.
    #: The idempotency guard is only valid for the *same* intent: a normal
    #: poll reconciles with ``confirmed=False`` and settles on
    #: ``reason="engine_matches"``, and reusing that verdict for a user who just
    #: pressed ``button.wattmosaic_confirm_topology`` would swallow the one
    #: action that unblocks learning.  The confirmation is a different request,
    #: so it must be allowed to re-examine the engine even at the same version.
    verified_confirmed_request: bool = False
    #: A ``PUT`` landed but ``POST /topology/confirm`` did not.  Retrying the
    #: confirm alone avoids a second topology revision for the same declaration.
    pending_confirm: int | None = None
    #: Fingerprint of the declaration the last push wrote, and how many times it
    #: has been written without the engine reflecting it.  Bounds a rewrite loop
    #: against an engine that accepts the PUT but drops the nesting fields.
    last_declaration: str | None = None
    identical_pushes: int = 0
    #: Polls elapsed since the last verification.  Drives the periodic re-read
    #: that catches a reset which recreated the same version number.
    polls_since_check: int = 0
    #: Fingerprint of the device-link *inputs* (stored links + registry rows)
    #: the last completed check examined.  A version guard alone cannot see a
    #: link the user adds *after* setup -- the engine's revision does not change
    #: -- so the resolved state entities would never be written.  Comparing this
    #: re-opens exactly that check and nothing else.
    checked_links: str | None = None

    def as_diagnostics(self) -> dict[str, Any]:
        """The public, secret-free view used by ``diagnostics.py``."""
        return {
            "topology_pushed": self.pushed,
            "topology_push_version": self.pushed_version,
            "topology_push_error": self.error,
            "topology_push_attempts": self.attempts,
            "topology_push_reason": self.reason,
            "topology_push_declared_version": self.declared_version,
            "topology_push_at": self.pushed_at.isoformat() if self.pushed_at else None,
            "topology_engine_version_seen": self.observed_version,
        }


async def async_replay_topology(
    client: Any,
    assignments: Mapping[str, Mapping[str, Any]] | None,
    *,
    confirmed: bool = False,
    stored_version: Any = None,
    state: TopologyPushState | None = None,
    observed_version: Any = None,
    now: datetime | None = None,
    logger: logging.Logger | None = None,
    state_links: Any = None,
    link_signature: str | None = None,
) -> TopologyPushState:
    """Re-apply the config entry's topology declaration to the engine.

    Why this exists
    ---------------
    The nesting/aliasing the user confirmed in the config-flow wizard is
    **user intent**, not an inference.  It lives in the config entry
    (``CONF_CT_ASSIGNMENTS``) and is the only record of it.  Until now the sole
    code path writing it to the engine was the wizard's confirm step, so a
    database reset -- or a discovery-driven bootstrap that installs a
    parentless candidate -- silently erased the declaration while the entry
    still said "confirmed at v5".  The engine then summed six physically nested
    mains and reported roughly double the real draw.

    Replaying a *stored user decision* is not fabrication: nothing here invents
    a channel, a parent or an alias.  Every field written comes from
    ``CONF_CT_ASSIGNMENTS``; the merge is :func:`build_topology_update`, the
    same function the wizard uses, so there is one and only one merge rule.

    Semantics
    ---------
    * No assignments and no resolved device link -> nothing happens (a fresh
      install has no declaration).
    * A resolved device link whose state entities the engine does not carry ->
      one ``PUT`` carrying ``metadata.link_state_entity_ids`` on the devices,
      even when no CT assignment changed.
    * In sync -> nothing happens; ``pushed`` stays truthful from the last run.
    * Declaration differs, or the engine is behind the stored version, or the
      user confirmed but the engine reports ``candidate`` -> one ``PUT`` and,
      when the entry is confirmed, one ``POST /topology/confirm``.
    * The engine reports a *newer* topology that already matches the
      declaration -> never downgrade it, never write.
    * A rewrite of a topology the engine already reports as *confirmed* stays
      confirmed: demoting it to ``candidate`` would silently disable learning.
    * Any engine error -> state records it and the caller retries on the next
      poll.  This function never raises: setup must not fail or block because a
      replay did not land.
    * The identical declaration is written at most :data:`MAX_IDENTICAL_PUSHES`
      times without the engine reflecting it, so a schema that drops the
      nesting fields cannot turn into one write per poll.

    Idempotency is a version guard: this exact engine revision is reconciled
    once, and a *different* revision (a reset, a bootstrap, a manual edit)
    re-opens the check.  A periodic read-only re-verification
    (:data:`TOPOLOGY_RECHECK_POLLS`) covers the case a version number cannot: a
    reset that recreated the same number.

    ``state_links`` is a :class:`state_links.LinkResolution` built by the caller
    from the Home Assistant registry; it never travel as an HA device id.  Only
    the resolved entity ids are written, and only into
    ``metadata.link_state_entity_ids``, so evidence-owned ``state_entity_ids``
    and ``config_load_entity_ids`` survive an unlink byte for byte.
    ``link_signature`` fingerprints those resolution *inputs* so a link added
    after setup re-opens the check even though the engine's version did not
    change.
    """
    log = logger or _LOGGER
    state = state or TopologyPushState()
    state.observed_version = as_int(observed_version)

    # A resolved device link is a second, independent reason to talk to the
    # engine.  Without it the state entities never reach the topology and the
    # engine's learning intake stays empty (the live ``samples_seen: 0``).
    #
    # ``state_links`` is a *callable* rather than a value on purpose: the
    # resolved ids depend on the engine's own device and CT list (a device's CT
    # entity must never become a state source), and that list only exists once
    # this function has read the topology.  Handing the callback the topology it
    # just fetched keeps this to one ``GET`` and keeps the registry read
    # integration-side.
    has_links = callable(state_links)

    # No declaration and no link declaration -> no traffic at all.  A fresh
    # install (or an entry whose wizard never ran) has nothing to replay, and
    # issuing a ``GET /topology`` on every poll for it would be pure cost.  Never
    # fabricate assignments.
    if (not isinstance(assignments, Mapping) or not assignments) and not has_links:
        state.reason = "no_assignments"
        return state

    # Idempotent guard, and the "engine topology_version != CONF_TOPOLOGY_VERSION"
    # re-check the coordinator relies on.
    #
    # This exact engine revision has already been reconciled, so there is
    # nothing to do.  A *different* version means the engine (re)wrote its
    # topology -- a database reset, a discovery bootstrap, a manual edit -- and
    # the declaration is re-examined (read-only when it still matches).  A
    # previous failure, or a PUT still awaiting confirmation, always re-opens
    # the check.  A periodic re-read covers the one case a version comparison
    # cannot see: a reset that recreated the same version number.
    #
    # ``link_signature`` closes the mirror-image blind spot for links: a user who
    # links a device *after* setup changes no engine revision at all, so a
    # version guard alone would never write the newly resolved entities.
    links_seen = link_signature is None or state.checked_links == link_signature
    state.polls_since_check += 1
    if (
        state.error is None
        and state.pending_confirm is None
        and state.verified_version is not None
        and state.verified_version == state.observed_version
        and state.polls_since_check < TOPOLOGY_RECHECK_POLLS
        # Both guards are independent and both must hold before the cached
        # verdict can be reused.
        #
        # A cached verdict from a non-confirming poll cannot answer a
        # confirming request: the poll only proved "the declaration matches",
        # never "the user may confirm now".  Re-examine instead of returning a
        # stale ``engine_matches`` that the confirm path would read as
        # "nothing was written".
        and (state.verified_confirmed_request or not confirmed)
        # A link declaration is a second, independent reason to re-examine: a
        # user who links a device after setup changes no engine revision at
        # all, so a version guard alone could never write the newly resolved
        # state entities.  ``links_seen`` is False until that signature is
        # checked in.
        and links_seen
    ):
        return state

    topology = await _safe_topology(client, state, log)
    if topology is None:
        # The read failed; ``_safe_topology`` recorded why.  Do not plan against
        # a topology we could not read -- that would either fabricate an empty
        # one or rewrite blind.  Retry on the next poll.  A pending confirm does
        # not need the body, so it is retried below regardless.
        state.reason = "topology_unavailable"
        state.attempts += 1
        if state.pending_confirm is None:
            return state
    elif state.error is not None and state.pending_confirm is None:
        # A previous read failed and the engine is reachable again.  The error
        # is stale now; a plan that still needs a write will set its own.
        state.error = None
    state.polls_since_check = 0

    # A previous attempt pushed the declaration but failed to confirm it.  The
    # topology body is already on the engine; re-sending it would create a
    # second revision for the same declaration, so only the confirm is retried.
    if state.pending_confirm is not None:
        state.attempts += 1
        try:
            await client.confirm_topology(state.pending_confirm)
            state.pushed = True
            state.pushed_version = state.pending_confirm
            state.pending_confirm = None
            state.error = None
            state.pushed_at = now or utcnow()
            state.verified_version = state.observed_version
            state.verified_confirmed_request = bool(confirmed)
        except Exception as err:  # noqa: BLE001 - retried on the next poll
            state.error = sanitize_text(err) or "topology confirmation failed"
            log.debug("WattMosaic topology confirm retry failed: %s", state.error)
        return state

    resolved_links = has_links and topology is not None
    links_changed = False
    resolution: Any = None
    if resolved_links:
        resolution = state_links(topology)
        topology, links_changed = apply_state_link_metadata(topology, resolution)
        # A resolved link the engine already carries is not a reason to write;
        # record the inputs so the guard above settles instead of re-reading.
        if not links_changed:
            state.checked_links = link_signature

    plan = plan_topology_push(
        topology,
        assignments,
        confirmed=confirmed,
        stored_version=stored_version,
        links_changed=links_changed,
    )
    state.reason = plan.reason
    if (
        not plan.push
        and plan.reason == "no_assignments"
        and resolution is not None
        and resolution.links_declared
    ):
        # "no_assignments" would blame a wizard that was never needed and hide
        # the link entirely.  Name the link outcome instead; each individual
        # refusal is in ``devices_without_state_entity``.
        state.reason = (
            "state_links_in_sync"
            if resolution.state_entities
            else "state_links_unresolved"
        )
    if not plan.push:
        # Record the revision actually read from the engine (not the snapshot's
        # possibly older reconciliation version), so the guard settles as soon
        # as the two agree.
        state.verified_version = (
            plan.engine_version if plan.engine_version is not None else state.observed_version
        )
        state.verified_confirmed_request = bool(confirmed)
        state.identical_pushes = 0
        state.checked_links = link_signature
        return state

    # Bound identical rewrites.  A declaration the engine accepted but does not
    # reflect (a schema that silently drops ``parent_ct_id``, say) must not be
    # re-sent on every poll: two attempts ride out a restart, then the mismatch
    # is reported as a push error instead of being hidden behind traffic.
    # The fingerprint covers the *whole* body this path writes, links included,
    # so a link-only push is bounded exactly like a declaration push.
    signature = declaration_signature(plan.assignments, plan.confirmed)
    if link_signature:
        signature = f"{signature}|links:{link_signature}"
    if signature == state.last_declaration and state.identical_pushes >= MAX_IDENTICAL_PUSHES:
        already_reported = state.reason == "engine_ignored_declaration"
        state.error = (
            "engine did not reflect the stored topology declaration after "
            f"{state.identical_pushes} attempts"
        )
        state.reason = "engine_ignored_declaration"
        (log.debug if already_reported else log.warning)("WattMosaic %s", state.error)
        return state

    state.attempts += 1
    produced: int | None = None
    notes = [TOPOLOGY_REPLAY_NOTE]
    if links_changed:
        notes.append(TOPOLOGY_LINK_NOTE)
    try:
        updated = build_topology_update(
            topology,
            plan.assignments,
            confirmed=plan.confirmed,
            notes=notes,
        )
        result = await client.put_topology(updated)
        produced = as_int(result.get("version")) if isinstance(result, Mapping) else None
        if plan.confirmed:
            await client.confirm_topology(produced)
        state.pushed = True
        state.pushed_version = produced if produced is not None else plan.engine_version
        state.declared_version = as_int(stored_version)
        state.error = None
        state.pushed_at = now or utcnow()
        state.identical_pushes = (
            state.identical_pushes + 1 if signature == state.last_declaration else 1
        )
        state.last_declaration = signature
        state.checked_links = link_signature
        # ``PUT`` produces a new revision, so the version observed on the next
        # poll is the one to reconcile against (and it will already be in sync).
        state.verified_version = produced if produced is not None else state.observed_version
        state.verified_confirmed_request = bool(confirmed)
        log.info(
            "Re-applied the stored WattMosaic topology declaration (engine v%s)",
            state.pushed_version,
        )
    except Exception as err:  # noqa: BLE001 - a replay must never break setup
        message = sanitize_text(err)
        first_failure = state.error is None
        state.error = message or "topology replay failed"
        if produced is not None and plan.confirmed:
            # The PUT landed; only the confirmation is outstanding.
            state.pending_confirm = produced
            state.pushed_version = produced
            state.declared_version = as_int(stored_version)
        # Log the first failure loudly, later retries quietly: a flapping engine
        # must not fill the log with one warning per poll.
        (log.warning if first_failure else log.debug)(
            "WattMosaic topology replay failed (will retry): %s", state.error
        )
    return state


async def _safe_topology(
    client: Any, state: TopologyPushState, log: logging.Logger
) -> dict[str, Any] | None:
    """``GET /topology`` that degrades to ``None`` instead of raising."""
    try:
        topology = await client.topology()
    except Exception as err:  # noqa: BLE001 - read failure is state, not a crash
        state.error = sanitize_text(err) or "topology read failed"
        log.debug("WattMosaic topology read failed: %s", state.error)
        return None
    return topology if isinstance(topology, Mapping) else None


def mapping_summary(
    channels: Sequence[Mapping[str, Any]],
    assignments: Mapping[str, Mapping[str, Any]],
    *,
    limit: int = 50,
) -> list[str]:
    """Compact human-readable summary used on the confirmation screen."""
    lines: list[str] = []
    for channel in channels[:limit]:
        ct_id = str(channel.get("ct_id") or "")
        raw = assignments.get(ct_id) or assignment_defaults(channel)
        normalised = normalise_assignment(raw, channel)
        pair = f" pair={normalised['paired_ct_id']}" if normalised["paired_ct_id"] else ""
        parent = f" parent={normalised['parent_ct_id']}" if normalised["parent_ct_id"] else ""
        alias = f" alias_of={normalised['alias_of_ct_id']}" if normalised["alias_of_ct_id"] else ""
        if normalised["measurement_multiplier"] != DEFAULT_MULTIPLIER:
            pair += f" x{normalised['measurement_multiplier']:g}"
        device = f" device={normalised['device_id']}" if normalised["device_id"] else ""
        flag = " (uncertain)" if is_uncertain(normalised) else ""
        lines.append(
            f"{ct_id} [{channel.get('name')}]: role={normalised['role']} "
            f"phase={normalised['phase']} polarity={normalised['polarity']}"
            f"{pair}{parent}{alias}{device}{flag}"
        )
    if len(channels) > limit:
        lines.append(f"... and {len(channels) - limit} more channels")
    return lines


# --------------------------------------------------------------------------- #
# Device candidates
# --------------------------------------------------------------------------- #

#: Tokens that carry no device-identifying information in a CT channel name.
#: These are structural words from the wiring vocabulary, never brand names: a
#: monitor's own name is filtered by ``panel_shared_tokens`` instead, which works for
#: any hardware.
_STOPWORDS = frozenset(
    {
        "power",
        "sensor",
        "energy",
        "the",
        "and",
        "ct",
        "ct1",
        "ct2",
        "ct3",
        "channel",
        "panel",
        "main",
        "sub",
    }
)


def _tokens(text: Any, extra: frozenset[str] = frozenset()) -> set[str]:
    words = re.split(r"[^a-z0-9]+", str(text or "").lower())
    return {
        word
        for word in words
        if len(word) > 1 and word not in _STOPWORDS and word not in extra
    }


def panel_shared_tokens(channels: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Tokens that appear in *every* channel name or ct id.

    This is the panel/monitor prefix, whatever the hardware is called.  Without
    it a channel called ``acme_panel_ct3`` would be offered the monitor device
    named ``Acme`` as a candidate: the shared token says nothing about which
    load the circuit serves.  Deriving it from the channels keeps the rule
    brand-agnostic, and it degrades to an empty set for a single channel.
    """
    shared: set[str] | None = None
    for channel in channels:
        tokens = _tokens(channel.get("ct_id")) | _tokens(channel.get("name"))
        shared = tokens if shared is None else (shared & tokens)
    # A single channel shares every token with itself, which says nothing about
    # a panel prefix; subtracting them would leave no candidates at all.
    if shared is None or len(channels) < 2:
        return frozenset()
    return frozenset(shared)


def normalise_device_list(payload: Any) -> list[dict[str, Any]]:
    """Normalise ``GET /devices`` into ``[{device_id, name, has_power}]``.

    When the engine includes a per-device ``configuration`` object it is kept
    verbatim under the same key, because the integration needs it to decide
    whether a device gets an energy entity and which real HA entity may host
    it.  A device without configuration simply has no such key.
    """
    raw: Any = payload
    if isinstance(payload, Mapping):
        raw = payload.get("devices") or payload.get("items") or []
    if not isinstance(raw, list):
        return []

    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            device_id, name, has_power = item, item, False
            configuration = None
        elif isinstance(item, Mapping):
            device_id = item.get("device_id") or item.get("id")
            if not isinstance(device_id, str) or not device_id.strip():
                continue
            name = item.get("name") or item.get("friendly_name") or device_id
            has_power = bool(item.get("has_power") or item.get("power_w") is not None)
            configuration = item.get("configuration")
        else:
            continue
        if device_id in seen:
            continue
        seen.add(device_id)
        entry: dict[str, Any] = {
            "device_id": device_id,
            "name": str(name),
            "has_power": has_power,
        }
        if isinstance(configuration, Mapping):
            entry["configuration"] = dict(configuration)
        devices.append(entry)
    return devices


def device_candidates(
    devices: Sequence[Mapping[str, Any]],
    channel: Mapping[str, Any],
    *,
    limit: int = 5,
    shared_tokens: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Rank devices by token overlap with a channel name.

    Candidates are *suggestions only* and are never written to the topology
    without an explicit user selection (master spec §15.2).  ``shared_tokens``
    is the panel/monitor prefix that every channel carries (``panel_shared_tokens``);
    matching on it would only ever suggest the monitor itself.
    """
    channel_tokens = _tokens(channel.get("name"), shared_tokens) | _tokens(
        channel.get("ct_id"), shared_tokens
    )
    if not channel_tokens:
        return []
    scored: list[tuple[int, bool, str, dict[str, Any]]] = []
    for device in devices:
        device_tokens = _tokens(device.get("name")) | _tokens(device.get("device_id"))
        overlap = channel_tokens & device_tokens
        if not overlap:
            continue
        score = len(overlap)
        scored.append(
            (
                score,
                bool(device.get("has_power")),
                str(device.get("name") or device.get("device_id")),
                {
                    **dict(device),
                    "overlap": sorted(overlap),
                    "confidence": "low" if score < 2 else "medium",
                },
            )
        )
    scored.sort(key=lambda item: (-item[0], not item[1], item[2]))
    return [item[3] for item in scored[:limit]]


def candidate_labels(candidates: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """``(device_id, label)`` pairs for a select field; never auto-selected."""
    labels: list[tuple[str, str]] = []
    for candidate in candidates:
        device_id = str(candidate.get("device_id") or "")
        if not device_id:
            continue
        overlap = ",".join(candidate.get("overlap") or [])
        label = f"{candidate.get('name') or device_id} ({device_id})"
        if overlap:
            label += f" — match: {overlap}"
        labels.append((device_id, label))
    return labels


def coercion_error_keys(errors: Sequence[str]) -> dict[str, str]:
    """Map validation keys to flow error keys for display."""
    mapped: dict[str, str] = {}
    for error in errors:
        key = error.split(":", 1)[0]
        mapped[key] = "invalid_mapping"
    return mapped


def assignment_store(
    assignments: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Serialise assignments into a JSON-friendly dict for the config entry."""
    return {str(ct_id): normalise_assignment(raw) for ct_id, raw in assignments.items()}


def count_confirmed(
    assignments: Mapping[str, Mapping[str, Any]],
) -> tuple[int, int]:
    """``(total, uncertain)`` counts for the flow summary."""
    total = len(assignments)
    uncertain = sum(1 for raw in assignments.values() if is_uncertain(raw))
    return total, uncertain


def format_voltage_hint(channel: Mapping[str, Any]) -> str:
    """Small helper used in flow descriptions."""
    voltage = as_float(channel.get("nominal_voltage"))
    if voltage is None:
        return "voltage unknown"
    return f"{voltage:g} V"


# --------------------------------------------------------------------------- #
# Per-device configuration (options flow)
# --------------------------------------------------------------------------- #
#
# ``DeviceConfiguration`` is nested (``expected`` / ``light`` / ``controller``)
# and validates cross-field rules.  The flow therefore keeps the *editing* shape
# flat — one form, one field per input — and converts it here, where the rules
# are testable without Home Assistant.


def parse_entity_list(value: Any) -> list[str]:
    """Parse a comma-separated entity list into a deduplicated list of ids.

    Accepts a string (the form field), an iterable of strings, or ``None``.
    Whitespace around each entry is trimmed, empty entries are dropped, and the
    first occurrence wins so a user can paste a list twice without duplicates.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw: list[Any] = value.split(",")
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        raw = list(value)
    else:
        return []

    entities: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        entities.append(text)
    return entities


def _clean_choice(value: Any, allowed: Sequence[str]) -> str | None:
    """Coerce one select value; ``None`` when empty, ``unknown`` when invalid."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text if text in allowed else "unknown"


def normalise_device_configuration(
    user_input: Mapping[str, Any] | None, *, stored_cts: Sequence[str] | None = None
) -> dict[str, Any]:
    """Convert one flat form submission into ``DeviceConfiguration`` fields.

    Only stated values are emitted: an absent field means "not stated", never a
    zero.  ``0.0`` *is* kept for ``expected_on_w`` because a user may legitimately
    assert that a device draws nothing while running (a standby-only device).
    ``device_id`` is never included — it travels on the command itself.
    """
    data = user_input or {}
    params: dict[str, Any] = {}

    for field, allowed in (
        ("device_type", DEVICE_TYPES),
        ("connectivity", CONNECTIVITY_LEVELS),
        ("monitoring", MONITORING_LEVELS),
        ("behavior", POWER_BEHAVIORS),
    ):
        choice = _clean_choice(data.get(field), allowed)
        # ``unknown`` is the contract default: sending it would not change the
        # engine's behaviour, so it is omitted like an empty field.
        if choice is not None and choice != "unknown":
            params[field] = choice

    state_entities = parse_entity_list(data.get("state_entity_ids"))
    if state_entities:
        params["state_entity_ids"] = state_entities

    mode_entities = parse_entity_list(data.get("mode_entity_ids"))
    if mode_entities:
        params["mode_entity_ids"] = mode_entities

    #: CTs the user asserts observe this device.  Without this the engine can
    #: only attribute a device from a CT the *channel wizard* assigned to it, and
    #: a channel has exactly one ``device_id`` -- so "these several lamps share
    #: this one circuit" could not be expressed at all.  The engine already reads
    #: it (``DeviceConfiguration.monitored_by_ct_ids``); nothing in the product
    #: sent it, which made the shared-circuit case unreachable through the UI.
    #:
    #: The widget is pre-filled with what is stored, so "submitted value equals
    #: the stored value" means the user expressed no opinion and nothing must be
    #: sent -- otherwise merely editing an unrelated field would create a
    #: configuration row for every device someone opened.  A *different* value is
    #: an instruction, and that includes the empty selection: leaving it empty
    #: must clear a wrong declaration.  Swallowing empty would make a mis-declared
    #: circuit impossible to undo, the same dead end the device link had.
    if "monitored_by_ct_ids" in data:
        submitted_cts = parse_entity_list(data.get("monitored_by_ct_ids"))
        if submitted_cts != list(stored_cts or ()):
            params["monitored_by_ct_ids"] = submitted_cts

    expected: dict[str, float] = {}
    for field, key in (("expected_on_w", "on_w"), ("expected_standby_w", "standby_w")):
        watts = as_float(data.get(field))
        if watts is not None and watts >= 0.0:
            expected[key] = watts
    if expected:
        params["expected"] = expected

    light = _normalise_light(data, params.get("device_type", "unknown"))
    if light:
        params["light"] = light

    controller = _normalise_controller(data)
    if controller:
        params["controller"] = controller

    # Integration-side only: the engine has no concept of a Home Assistant
    # energy entity, so this never travels in the command parameters.  It is
    # returned separately by ``energy_entity_choice``.
    return params


def energy_entity_choice(user_input: Mapping[str, Any] | None) -> bool | None:
    """Per-device decision to create the energy entity.

    ``None`` means the form did not express an opinion, which must be treated
    as "leave the stored value alone" rather than "enable" or "disable".
    """
    data = user_input or {}
    if ENERGY_ENTITY_FIELD not in data:
        return None
    return as_bool(data.get(ENERGY_ENTITY_FIELD), default=True)


def stored_energy_choice(
    stored: Any, device_id: str, *, default: bool = True
) -> bool:
    """The form default for the energy opt-in.

    The wizard form always submits its checkbox, so the *default* is what
    decides the outcome when a user edits any other field on the same device.
    Showing an unconditional ``True`` would silently re-enable an entity the
    user deliberately turned off.  The stored choice therefore wins, and
    ``default`` applies only to a device that has never been configured.
    """
    if not isinstance(stored, Mapping):
        return default
    return as_bool(stored.get(device_id), default=default)


# --------------------------------------------------------------------------- #
# Device attachment (link an engine device to an existing HA device)
# --------------------------------------------------------------------------- #


def device_link_choice(user_input: Mapping[str, Any] | None) -> str | None:
    """The HA device the form links, or ``None`` when it stated nothing.

    An empty string is a real instruction: it *unlinks*.  Distinguishing it from
    "field absent" matters because the form always submits the selector, and
    collapsing both to ``None`` would make a device impossible to unlink.
    """
    data = user_input or {}
    if DEVICE_LINK_FIELD not in data:
        return None
    value = data.get(DEVICE_LINK_FIELD)
    if value is None:
        return ""
    return str(value).strip()


def apply_device_link(
    stored_links: Any,
    device_id: str,
    choice: str | None,
) -> dict[str, str]:
    """The full link map with one device's link set, changed or removed.

    Home Assistant replaces an entry's ``options`` wholesale, so every
    completion path must rebuild the *whole* map.  A partial map would silently
    detach every other device the user had linked.

    ``choice is None`` means "no opinion" and leaves the map untouched; ``""``
    removes the link; anything else sets it.
    """
    links: dict[str, str] = {}
    if isinstance(stored_links, Mapping):
        for key, value in stored_links.items():
            text_id = str(key).strip()
            text_device = str(value or "").strip()
            if text_id and text_device:
                links[text_id] = text_device
    if choice is None:
        return links
    if not choice:
        links.pop(str(device_id), None)
    else:
        links[str(device_id)] = choice
    return links


def linked_device_id(stored_links: Any, device_id: str) -> str:
    """The already-stored link for one device, or ``""`` when unlinked."""
    if not isinstance(stored_links, Mapping):
        return ""
    value = stored_links.get(device_id)
    return value.strip() if isinstance(value, str) else ""


def device_link_established(
    engine_device_id: str,
    ha_device_id: str,
) -> bool:
    """Whether a link names a usable Home Assistant device.

    A blank device id is not a link, and an engine device cannot host its own
    measurement: that would make the inferred load the parent of the entity that
    describes it.
    """
    return bool(engine_device_id.strip()) and bool(ha_device_id.strip())


def _normalise_light(data: Mapping[str, Any], device_type: str) -> dict[str, Any]:
    """Light sub-fields, only for the device types the contract allows."""
    if device_type not in ("light", "light_group", "unknown"):
        return {}

    light_type = _clean_choice(data.get("light_type"), LIGHT_TYPES)
    bulb_count = as_int(data.get("bulb_count"))
    watts = as_float(data.get("watts_per_bulb_on"))

    light: dict[str, Any] = {}
    if light_type is not None and light_type != "unknown":
        light["light_type"] = light_type
    if bulb_count is not None:
        light["bulb_count"] = max(1, min(200, int(bulb_count)))
    if watts is not None and watts >= 0.0:
        light["watts_per_bulb_on"] = watts

    # A default fixture (one unknown bulb, no stated watts) is not worth sending.
    meaningful = (
        light.get("light_type") not in (None, "unknown")
        or "watts_per_bulb_on" in light
        or light.get("bulb_count", 1) != 1
    )
    return light if meaningful else {}


def _normalise_controller(data: Mapping[str, Any]) -> dict[str, Any]:
    controller: dict[str, Any] = {}
    mode_entity = data.get("controller_mode_entity_id")
    if isinstance(mode_entity, str) and mode_entity.strip():
        controller["mode_entity_id"] = mode_entity.strip()
    load_entities = parse_entity_list(data.get("controller_load_entity_ids"))
    if load_entities:
        controller["load_entity_ids"] = load_entities
    return controller


def validate_device_configuration(
    user_input: Mapping[str, Any] | None, *, stored_cts: Sequence[str] | None = None
) -> dict[str, str]:
    """Validate one flat form submission.  Returns ``{field: error_key}``.

    Mirrors the cross-field rules in ``DeviceConfiguration`` so the user sees a
    field error instead of an engine rejection.
    """
    data = user_input or {}
    errors: dict[str, str] = {}

    device_type = _clean_choice(data.get("device_type"), DEVICE_TYPES)
    connectivity = _clean_choice(data.get("connectivity"), CONNECTIVITY_LEVELS)
    behavior = _clean_choice(data.get("behavior"), POWER_BEHAVIORS)

    if device_type is None:
        errors["device_type"] = "invalid_choice"
    if connectivity is None:
        errors["connectivity"] = "invalid_choice"
    if behavior is None:
        errors["behavior"] = "invalid_choice"

    state_entities = parse_entity_list(data.get("state_entity_ids"))
    if connectivity == "not_connected" and state_entities:
        errors["state_entity_ids"] = "not_connected_with_state_entities"

    if state_entities and any(not _looks_like_entity_id(e) for e in state_entities):
        errors["state_entity_ids"] = "invalid_entity_id"

    mode_entities = parse_entity_list(data.get("mode_entity_ids"))
    if mode_entities and any(not _looks_like_entity_id(e) for e in mode_entities):
        errors["mode_entity_ids"] = "invalid_entity_id"

    load_entities = parse_entity_list(data.get("controller_load_entity_ids"))
    if load_entities and any(not _looks_like_entity_id(e) for e in load_entities):
        errors["controller_load_entity_ids"] = "invalid_entity_id"

    mode_entity = data.get("controller_mode_entity_id")
    if (
        isinstance(mode_entity, str)
        and mode_entity.strip()
        and not _looks_like_entity_id(mode_entity.strip())
    ):
        errors["controller_mode_entity_id"] = "invalid_entity_id"

    if behavior == "complex" and not (
        (isinstance(mode_entity, str) and mode_entity.strip()) or load_entities
    ):
        errors["behavior"] = "complex_requires_controller"

    for field in ("expected_on_w", "expected_standby_w", "watts_per_bulb_on"):
        raw = data.get(field)
        if raw in (None, ""):
            continue
        number = as_float(raw)
        if number is None or number < 0.0:
            errors[field] = "invalid_number"

    for ct_id in parse_entity_list(data.get("monitored_by_ct_ids")):
        if not _looks_like_ct_id(ct_id):
            errors["monitored_by_ct_ids"] = "invalid_ct_id"
            break

    raw_bulbs = data.get("bulb_count")
    if raw_bulbs not in (None, ""):
        bulbs = as_int(raw_bulbs)
        if bulbs is None or bulbs < 1:
            errors["bulb_count"] = "invalid_number"

    # An all-default form states nothing.  The engine reads an empty parameter
    # mapping as "remove this device's configuration", so a blank submission must
    # be refused rather than silently deleting what the user has not replaced.
    # The energy opt-in is integration-side, so it alone *is* something to save
    # and must not be rejected as empty.  A *changed* CT selection is also
    # something to save, including clearing it: that is how a wrong declaration
    # is undone.
    if (
        not errors
        and not normalise_device_configuration(data, stored_cts=stored_cts)
        and energy_entity_choice(data) is None
    ):
        errors["base"] = "nothing_to_save"

    return errors


def _looks_like_entity_id(value: str) -> bool:
    """``domain.object_id`` — deliberately permissive about the characters."""
    text = str(value).strip()
    if text.count(".") != 1:
        return False
    domain, object_id = text.split(".", 1)
    return bool(domain) and bool(object_id) and " " not in text


def _looks_like_ct_id(value: str) -> bool:
    """A CT id is an engine identifier: one token, no spaces, no domain dot.

    It is *not* an entity id.  Accepting ``sensor.foo`` here would silently
    declare a channel that does not exist, and the engine drops unknown ids --
    the user would see a saved configuration and no change in behaviour.
    """
    text = str(value).strip()
    return bool(text) and " " not in text and "." not in text


def device_configuration_command(
    device_id: str, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    """The ``ServiceCommand`` body for ``set_device_configuration``."""
    return {
        "command": COMMAND_SET_DEVICE_CONFIGURATION,
        "device_id": str(device_id).strip(),
        "parameters": dict(parameters),
    }

