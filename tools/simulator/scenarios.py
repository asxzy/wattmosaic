"""Deterministic builders for the canonical T01–T15 simulation scenarios.

The scenario identifier set is fixed by ``01-requirements.md`` §4.1 (the only
T-catalogue; ``REQ-TEST-003``/``REQ-TEST-004``).  This module mirrors that set
in :data:`SCENARIO_IDS` and exposes one deterministic builder per id through
:data:`SCENARIO_BUILDERS`.

Builders deliberately *reuse* the seeded :mod:`tools.simulator.labhouse`
strata rather than inventing a second generator: every T scenario maps onto an
existing household situation, and each builder adds only a stable per-id seed
offset.  Identical inputs therefore produce byte-identical records, which the
coverage test pins.
"""

from __future__ import annotations

from collections.abc import Callable

from .labhouse import DEFAULT_INTERVAL, DEFAULT_MINUTES, DEFAULT_SEED, LabHouse
from .labhouse import build_scenario as _build_stratum

#: The canonical scenario ids, verbatim from ``01-requirements.md`` §4.1.
#: This tuple is the source of truth for the coverage assertion.
SCENARIO_IDS: tuple[str, ...] = (
    "T01",
    "T02",
    "T03",
    "T04",
    "T05",
    "T06",
    "T07",
    "T08",
    "T09",
    "T10",
    "T11",
    "T12",
    "T13",
    "T14",
    "T15",
)

#: The human-readable canonical titles from §4.1, kept for diagnosis only.
SCENARIO_TITLES: dict[str, str] = {
    "T01": "Single Child Containment",
    "T02": "Multiple Child Contributions",
    "T03": "Unobserved Parent Load",
    "T04": "Baseline Standby Consumption",
    "T05": "Noise",
    "T06": "Multiple Simultaneous Devices",
    "T07": "Non-identifiable Devices",
    "T08": "Variable Power Device",
    "T09": "Delayed Device Response",
    "T10": "Unknown Device Discovery",
    "T11": "Smart Plug Relocation",
    "T12": "Reader Relocation or Replacement",
    "T13": "240 V Split-Phase Load",
    "T14": "Missing and Delayed Data",
    "T15": "Bidirectional Power",
}

#: Each T scenario maps onto a documented, reusable LabHouse stratum.  The
#: mapping is intentionally many-to-one where scenarios share a household
#: situation; the per-id seed offset keeps each built scenario distinct.
_SCENARIO_STRATA: dict[str, str] = {
    "T01": "dedicated",
    "T02": "shared",
    "T03": "unknown_load",
    "T04": "standby",
    "T05": "spikes",
    "T06": "automation_burst",
    "T07": "ambiguous_pair",
    "T08": "dynamic",
    "T09": "delayed_ct",
    "T10": "unknown_load",
    "T11": "reversed_orientation",
    "T12": "negative_ct",
    "T13": "unbalanced",
    "T14": "missing_intervals",
    "T15": "reversed_orientation",
}

#: Stable seed offset derived from the numeric part of each id, so a builder's
#: default seed is reproducible and distinct across ids without hand-picking.
def _seed_for(scenario_id: str, seed: int) -> int:
    return seed + int(scenario_id[1:])


ScenarioBuilder = Callable[..., LabHouse]


def _make_builder(scenario_id: str) -> ScenarioBuilder:
    """Return a deterministic builder bound to one canonical scenario id."""

    def build(
        seed: int = DEFAULT_SEED,
        *,
        minutes: float = DEFAULT_MINUTES,
        interval: float = DEFAULT_INTERVAL,
    ) -> LabHouse:
        return _build_stratum(
            _SCENARIO_STRATA[scenario_id],
            _seed_for(scenario_id, seed),
            minutes=minutes,
            interval=interval,
        )

    build.__name__ = f"build_{scenario_id.lower()}"
    build.__qualname__ = f"scenarios.{build.__name__}"
    return build


#: Every canonical scenario id has exactly one deterministic builder.
SCENARIO_BUILDERS: dict[str, ScenarioBuilder] = {
    scenario_id: _make_builder(scenario_id) for scenario_id in SCENARIO_IDS
}


def build_scenario(
    scenario_id: str,
    seed: int = DEFAULT_SEED,
    *,
    minutes: float = DEFAULT_MINUTES,
    interval: float = DEFAULT_INTERVAL,
) -> LabHouse:
    """Build the named T scenario, raising ``KeyError`` for an unknown id."""
    try:
        builder = SCENARIO_BUILDERS[scenario_id]
    except KeyError:
        raise KeyError(
            f"unknown scenario {scenario_id!r}; available: {list(SCENARIO_IDS)}"
        ) from None
    return builder(seed, minutes=minutes, interval=interval)


__all__ = [
    "SCENARIO_BUILDERS",
    "SCENARIO_IDS",
    "SCENARIO_TITLES",
    "build_scenario",
]
