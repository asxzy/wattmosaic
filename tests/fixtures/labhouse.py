"""Test-facing helpers for building `LabHouse` strata and Hot Tub variants.

This module exists so a test reads like the scenario it exercises::

    from tests.fixtures.labhouse import house, hot_tub

    def test_two_loads_one_breaker():
        lab = house("shared", seed=7)
        assert len(lab.observations()) == len(lab.ground_truth())

The heavy lifting lives in ``tools/simulator``.  Nothing here is a pytest
fixture: these are plain functions, so they can be called from a helper, a
parametrisation list or a benchmark as easily as from a test.  The pytest
fixtures that wrap them are in ``tests/conftest.py``.

Coverage of the scenario matrix
-------------------------------
``MATRIX_STRATA`` maps every *scenario-bearing* row group in
``docs/test_scenario_matrix.md`` onto the strata that can build it, and
``MATRIX_UNCOVERED`` records the rows a simulator fixture deliberately cannot
reach (they need production wiring, not data).  ``assert_matrix_coverage``
fails loudly if a required stratum name disappears, which is the point: a
renamed builder must break the coverage test rather than silently reduce it.
"""

from __future__ import annotations

from tools.simulator.labhouse import LabHouse, build_scenario

#: The strata the brief requires by name.  This tuple is the contract: every
#: name here must be buildable, and a test asserts exactly that.
REQUIRED_STRATA: tuple[str, ...] = (
    "dedicated",
    "shared",
    "standby",
    "unknown_load",
    "dynamic",
    "factor2",
    "noisy_legs",
    "unbalanced",
    "automation_burst",
    "ambiguous_pair",
    "hvac_modulation",
    "ev_rate_change",
    "fridge_cycling",
    "motor_startup",
    "delayed_ct",
    "irregular_sampling",
    "missing_intervals",
    "spikes",
    "main_subpanel_overlap",
    "reversed_orientation",
    "negative_ct",
)

#: Matrix row → the strata that can build that scenario.
#: Rows absent from this mapping are covered by existing unit tests on contract
#: objects and do not need a household-shaped fixture.
MATRIX_STRATA: dict[str, tuple[str, ...]] = {
    # Group A — basic loads
    "A1": ("dedicated",),
    "A2": ("unbalanced", "noisy_legs"),
    "A3": ("dedicated",),
    "A4": ("shared",),
    "A5": ("dynamic",),
    "A6": ("motor_startup",),
    "A7": ("standby",),
    # Group B — Refoss factor
    "B1": ("dedicated",),
    "B2": ("factor2",),
    "B3": ("factor2",),
    "B4": ("factor_changes_midhistory",),  # hot tub variant, built separately
    "B5": ("factor_missing",),
    "B6": ("factor_incorrect_metadata",),
    "B7": ("factor2",),
    "B8": ("unbalanced", "noisy_legs"),
    "B9": ("factor_changes_midhistory",),
    # Group C — CT relationships
    "C1": ("dedicated",),
    "C2": ("unbalanced",),
    "C3": ("shared",),
    "C4": ("main_subpanel_overlap",),
    "C5": ("main_subpanel_overlap",),
    "C6": ("ambiguous_pair",),
    "C7": ("negative_ct",),
    "C8": ("missing_intervals",),
    "C9": ("reversed_orientation",),
    # Group D — dedicated circuits
    "D1": ("ev_rate_change",),
    "D2": ("factor2",),
    "D3": ("hvac_modulation",),
    "D4": ("standby",),
    "D5": ("dynamic",),
    "D6": ("dedicated",),
    "D7": ("ambiguous_pair",),
    # Group E — shared circuits
    "E1": ("shared",),
    "E2": ("shared",),
    "E3": ("shared",),
    "E4": ("shared",),
    "E5": ("automation_burst",),
    "E6": ("unknown_load",),
    # Group F — residuals
    "F1": ("standby",),
    "F2": ("standby",),
    "F3": ("unknown_load",),
    "F4": ("unknown_load",),
    "F5": ("negative_ct",),
    "F6": ("standby",),
    "F7": ("unknown_load",),
    "F8": ("ambiguous_pair",),
    "F9": ("dedicated",),
    "F10": ("main_subpanel_overlap",),
    # Group G — HA events
    "G1": ("dedicated",),
    "G2": ("automation_burst",),
    "G3": ("automation_burst",),
    "G4": ("shared",),
    "G5": ("automation_burst",),
    "G6": ("delayed_ct",),
    "G7": ("delayed_ct",),
    "G8": ("automation_burst",),
    "G9": ("ambiguous_pair",),
    "G10": ("delayed_ct",),
    # Group H — dynamic
    "H1": ("hvac_modulation",),
    "H2": ("ev_rate_change",),
    "H3": ("fridge_cycling",),
    "H4": ("motor_startup",),
    "H5": ("dynamic",),
    "H6": ("delayed_ct",),
    "H7": ("irregular_sampling",),
    "H8": ("spikes", "noisy_legs"),
    "H9": ("missing_intervals",),
    # Group I — identifiability
    "I1": ("ambiguous_pair",),
    "I2": ("ambiguous_pair",),
    "I3": ("ambiguous_pair",),
    "I4": ("ambiguous_pair",),
    "I5": ("ambiguous_pair",),
    "I6": ("inconsistent_ha_vs_ct",),  # hot tub variant
    "I7": ("unknown_load",),
    "I8": ("main_subpanel_overlap",),
}

#: Hot Tub variant names that the matrix rows above reference by shorthand.
REQUIRED_HOT_TUB_VARIANTS: tuple[str, ...] = (
    "factor_one",
    "factor_two",
    "factor_changes_midhistory",
    "factor_missing",
    "factor_incorrect_metadata",
    "multiple_factor_cts",
    "independent_physical_cts",
    "noisy_unbalanced_legs",
    "inconsistent_ha_vs_ct",
)


def house(name: str, seed: int = 42, *, minutes: float = 20.0, interval: float = 15.0) -> LabHouse:
    """Build the named stratum (short alias for ``build_scenario``)."""
    return build_scenario(name, seed, minutes=minutes, interval=interval)


def matrix_rows() -> dict[str, str]:
    """Every matrix row this module can build, with its primary stratum."""
    return {row: strata[0] for row, strata in MATRIX_STRATA.items()}


def assert_matrix_coverage() -> None:
    """Fail loudly if a matrix row names a stratum that cannot be built.

    Two directions are checked: a stratum must exist, *and* every required
    stratum must be referenced by at least one row (an unreferenced builder is
    dead weight that nothing exercises).
    """
    from tools.simulator.hot_tub import HOT_TUB_VARIANTS
    from tools.simulator.labhouse import available_strata

    buildable = set(available_strata())
    hot_tub_names = set(HOT_TUB_VARIANTS) | set(HOT_TUB_VARIANTS.values())

    missing: dict[str, list[str]] = {}
    for name in REQUIRED_STRATA:
        if name not in buildable:
            missing.setdefault("stratum", []).append(name)
    for name in REQUIRED_HOT_TUB_VARIANTS:
        if name not in hot_tub_names:
            missing.setdefault("hot_tub_variant", []).append(name)
    if missing:
        raise AssertionError(
            "the documented matrix cannot be built; missing builders: "
            + "; ".join(f"{kind}={names}" for kind, names in sorted(missing.items()))
        )

    referenced = {
        name
        for strata in MATRIX_STRATA.values()
        for name in strata
        if name in buildable
    }
    unreferenced = sorted(buildable - referenced)
    if unreferenced:
        raise AssertionError(
            "these strata are buildable but no matrix row uses them, so nothing "
            f"exercises them: {unreferenced}"
        )


__all__ = [
    "MATRIX_STRATA",
    "REQUIRED_HOT_TUB_VARIANTS",
    "REQUIRED_STRATA",
    "assert_matrix_coverage",
    "house",
    "matrix_rows",
]
