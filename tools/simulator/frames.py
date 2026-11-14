"""The observation/ground-truth seam, and the leak detector that enforces it.

Why a seam at all
-----------------
A simulator that hands an algorithm its own ground truth proves nothing.  The
existing ``HouseholdScenario`` record mixes both frames on purpose — a test
harness needs them side by side — so a consumer must be able to *split* a record
into two disjoint views:

``observations(record)``
    exactly what an integration could learn from Home Assistant and its CTs:
    delivered sample values, the three timestamps, quality flags, declared
    factor metadata, HA device states and declared events.
``ground_truth(record)``
    the physical truth an algorithm must never see: per-device watts, the true
    CT aggregation, panel totals, unmetered power, integrated energy, and the
    conductor/appliance split behind a factor channel.

``find_ground_truth_keys`` walks the observation view recursively and names
every surviving truth key.  It is deliberately *structural* (key names), so it
cannot be satisfied by a value happening to look plausible; and it is
deliberately recursive, because the leaks that matter hide two levels down —
``measurements[ct]["true_channel_power_w"]`` and
``ha_states[entity]["attributable"]`` are exactly that kind of leak.

Nothing in this module knows about any particular household: it operates on the
record dict shape documented in :mod:`tools.simulator.scenario`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# --------------------------------------------------------------------------- #
# Ground-truth vocabulary
# --------------------------------------------------------------------------- #

#: Keys that name physical truth and must never reach an algorithm.
TRUTH_KEYS: frozenset[str] = frozenset(
    {
        "ground_truth",
        "ct_powers_w",
        "ct_energy_kwh",
        "device_energy_kwh",
        "device_power_w",
        "devices_on",
        "main_total_w",
        "panel_totals_w",
        "unmetered_w",
        "unmetered_truth_w",
        "power_w_truth",
        # simulator-diagnostic fields that are truth, not observation
        "true_channel_power_w",
        "injected_noise_w",
        "noise_w",
        "conductor_truth_w",
        "appliance_leg_truth_w",
        "appliance_total_truth_w",
        # truth-shaped judgements the simulator attaches to a HA state
        "attributable",
        "sim_type",
        "nominal_power_w",
        "seed_key",
    }
)

#: Prefixes that mark a truth key without being in the explicit set.
TRUTH_KEY_PREFIXES: tuple[str, ...] = ("true_", "ground_", "gt_", "truth_")

#: Suffixes that mark a truth key without being in the explicit set.
TRUTH_KEY_SUFFIXES: tuple[str, ...] = ("_truth", "_truth_w", "_truth_kwh")


def is_truth_key(key: str) -> bool:
    """Whether a key name denotes ground truth rather than observation."""
    if key in TRUTH_KEYS:
        return True
    if key.startswith(TRUTH_KEY_PREFIXES):
        return True
    return key.endswith(TRUTH_KEY_SUFFIXES)


def find_ground_truth_keys(view: Any, *, _path: str = "") -> list[str]:
    """Recursively list every dotted path whose key names ground truth.

    Returns an empty list for a clean observation view.  The dotted path (e.g.
    ``measurements.ct_main_l1.true_channel_power_w``) is the point: a bare
    boolean tells you the seam is broken but not what leaked.
    """
    found: list[str] = []
    if isinstance(view, Mapping):
        for key, value in view.items():
            name = str(key)
            path = f"{_path}.{name}" if _path else name
            if is_truth_key(name):
                found.append(path)
            found.extend(find_ground_truth_keys(value, _path=path))
    elif isinstance(view, (list, tuple)):
        for index, item in enumerate(view):
            found.extend(find_ground_truth_keys(item, _path=f"{_path}[{index}]"))
    return found


def assert_no_ground_truth_keys(view: Any, *, where: str = "observation view") -> None:
    """Raise ``AssertionError`` naming every leaked truth path."""
    leaked = find_ground_truth_keys(view)
    if leaked:
        preview = "\n".join(f"  - {path}" for path in leaked[:20])
        more = "" if len(leaked) <= 20 else f"\n  ... and {len(leaked) - 20} more"
        raise AssertionError(
            f"{where} leaks {len(leaked)} ground-truth key(s):\n{preview}{more}"
        )


# --------------------------------------------------------------------------- #
# Measurement shape
# --------------------------------------------------------------------------- #

#: Fields of a delivered sample that are observation, not truth.
OBSERVABLE_MEASUREMENT_FIELDS: tuple[str, ...] = (
    "entity_id",
    "ct_id",
    "value",
    "value_w",
    "unit",
    "state_timestamp",
    "received_at",
    "physical_timestamp",
    "quality",
    "repeated",
    "polarization",
    "sequence",
    "sample_time_s",
    "delivered",
    "samples_in_tick",
    "factor",
)

#: Fields kept in the as-of-tick-end snapshot (no per-tick counters).
OBSERVABLE_LATEST_FIELDS: tuple[str, ...] = (
    "entity_id",
    "value_w",
    "state_timestamp",
    "received_at",
    "physical_timestamp",
    "sample_time_s",
    "quality",
    "repeated",
    "age_s",
    "factor",
)

#: HA-state fields that are observation.  ``attributable`` is intentionally
#: absent: whether a load is "attributable" is a property of the household, not
#: of the state message.
OBSERVABLE_STATE_FIELDS: tuple[str, ...] = (
    "entity_id",
    "state",
    "attributes",
    "last_changed",
    "last_updated",
    "received_at",
    "device_id",
)

#: Scenario metadata that is safe to show an algorithm.
OBSERVABLE_SCENARIO_FIELDS: tuple[str, ...] = (
    "seed",
    "minutes",
    "interval_s",
    "start_time",
    "horizon_s",
    "quality",
    "simulator_version",
)


def _pick(source: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {name: source[name] for name in fields if name in source}


def observation_view(
    record: Mapping[str, Any],
    *,
    include_events: bool = True,
    include_scenario: bool = True,
) -> dict[str, Any]:
    """Strip a simulator record down to what an algorithm may see.

    The output is a *new* dict; the input record is never mutated, so a test can
    hold both views of the same tick.
    """
    view: dict[str, Any] = {
        "timestamp": record.get("timestamp"),
        "t_s": record.get("t_s"),
        "sequence": record.get("sequence"),
        "measurements": {
            ct_id: _pick(sample, OBSERVABLE_MEASUREMENT_FIELDS)
            for ct_id, sample in (record.get("measurements") or {}).items()
        },
        "latest_measurements": {
            ct_id: _pick(sample, OBSERVABLE_LATEST_FIELDS)
            for ct_id, sample in (record.get("latest_measurements") or {}).items()
        },
        "ha_states": {
            entity_id: _pick(state, OBSERVABLE_STATE_FIELDS)
            for entity_id, state in (record.get("ha_states") or {}).items()
        },
    }
    if include_events:
        view["events"] = [dict(event) for event in record.get("events") or ()]
    if include_scenario:
        view["scenario"] = _pick(
            record.get("scenario") or {}, OBSERVABLE_SCENARIO_FIELDS
        )
        factors = (record.get("scenario") or {}).get("factor_channels") or {}
        if factors:
            view["factors"] = {ct_id: dict(meta) for ct_id, meta in factors.items()}
    return view


def truth_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """The complementary frame: physical truth for one tick."""
    truth = dict(record.get("ground_truth") or {})
    truth["t_s"] = record.get("t_s")
    truth["timestamp"] = record.get("timestamp")
    truth["sequence"] = record.get("sequence")
    return truth


def observation_stream(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """``observation_view`` over a whole record stream."""
    return [observation_view(record) for record in records]


def truth_stream(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``truth_view`` over a whole record stream."""
    return [truth_view(record) for record in records]


__all__ = [
    "OBSERVABLE_LATEST_FIELDS",
    "OBSERVABLE_MEASUREMENT_FIELDS",
    "OBSERVABLE_SCENARIO_FIELDS",
    "OBSERVABLE_STATE_FIELDS",
    "TRUTH_KEYS",
    "TRUTH_KEY_PREFIXES",
    "TRUTH_KEY_SUFFIXES",
    "assert_no_ground_truth_keys",
    "find_ground_truth_keys",
    "is_truth_key",
    "observation_stream",
    "observation_view",
    "truth_stream",
    "truth_view",
]
