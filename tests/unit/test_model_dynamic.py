"""L3: Dynamic (hidden-state) power model.

Covers the task's acceptance points:

* an X07 fridge cycle yields >= 2 states under one HA state (WP-F3 / AT 5),
* segmentation-initialised states keep robust power/duration stats,
* running transition counts and duration stats are updated incrementally,
* ambiguous segmentation yields an "unknown stage", never a guessed state,
* the spec round-trips through the L1 codec (DEF-02).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.contracts import (
    DynamicSpec,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PowerModelVersion,
    PredictionStatus,
)
from wattmosaic_engine.learning.stages import (
    StateRecognition,
    recognize_dynamic_states,
)
from wattmosaic_engine.models.dynamic import (
    DynamicModel,
    build_dynamic_spec_from_series,
    fit_dynamic_spec,
)
from wattmosaic_engine.models.segmentation import group_segments_into_states, segment_power_series
from wattmosaic_engine.models.spec import decode_spec, encode_spec
from wattmosaic_engine.runtime.interfaces import PredictionContext

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _series(levels: list[tuple[float, int]]) -> list[tuple[datetime, float]]:
    """Build a deterministic stepped series from (watts, sample_count) pairs."""
    points: list[tuple[datetime, float]] = []
    t = T0
    for power, count in levels:
        for _ in range(count):
            points.append((t, power))
            t += timedelta(seconds=10)
    return points


def _version(spec: DynamicSpec, n: int = 1) -> PowerModelVersion:
    return PowerModelVersion(
        version_id=f"mdl-fridge-{n}",
        subject_kind="device",
        subject_id="fridge",
        number=n,
        model_class=ModelClass.DYNAMIC,
        basis=ModelBasis.DELTA,
        spec=spec,
        trainer_version="test-L3",
        created_at=T0,
    )


def test_x07_fridge_cycle_yields_two_states_under_one_ha_state() -> None:
    """A simulator fridge duty cycle gives >= 2 hidden states for one HA state."""
    from tools.simulator.behavior import RefrigeratorProfile
    from tools.simulator.topology import Phase, SimDevice, SimDeviceType

    device = SimDevice(
        device_id="fridge",
        name="Fridge",
        sim_type=SimDeviceType.REFRIGERATOR,
        phase=Phase.L1,
        circuits={},
        nominal_power_w=150.0,
        ha_entity_id="switch.fridge",
        seed_key="load_000",
    )
    profile = RefrigeratorProfile(device=device, seed=42, horizon_s=4 * 3600)
    points = [
        (T0 + timedelta(seconds=t), profile.power(t)) for t in range(0, 4 * 3600, 10)
    ]

    result = segment_power_series(points, min_change_w=8.0, noise_k=4.0, min_segment_s=20.0)
    assert result.ambiguous is False
    spec, states = build_dynamic_spec_from_series(result, ha_state="cooling")

    # At least two distinct hidden states (standby ~1 W and running ~150 W)
    # are mapped to the single HA state "cooling".
    assert len(states) >= 2
    assert len(set(states)) == len(states)
    assert spec.ha_state_to_states["cooling"] == states or set(
        spec.ha_state_to_states["cooling"]
    ) == set(states)
    powers = [spec.states[s].median_w for s in states]
    assert min(powers) < 10.0  # standby
    assert max(powers) >= 150.0  # compressor


def test_states_keep_robust_power_and_duration_stats() -> None:
    points = _series([(5.0, 12), (150.0, 12), (5.0, 12)])
    spec = fit_dynamic_spec(points, ha_state="cool", min_change_w=15.0)

    assert len(spec.states) == 2
    for stat in spec.states.values():
        assert stat.median_w is not None
        assert stat.n > 0
    # Durations are recorded per state, never forced to 0.
    assert all(s.median_w is None or s.median_w > 0 for s in spec.duration_s.values())


def test_ha_state_maps_to_a_set_of_states_and_prediction_prioritises() -> None:
    points = _series([(30.0, 12), (150.0, 12)])
    spec = fit_dynamic_spec(points, ha_state="cool", min_change_w=15.0)
    model = DynamicModel(spec=spec)

    assert len(spec.ha_state_to_states["cool"]) >= 2
    # prediction picks the highest-priority state for that HA state.
    prediction = model.predict({"state": "cool"}, PowerLevelV3.ACTIVE)
    assert prediction.status == PredictionStatus.KNOWN
    assert prediction.mean_w == pytest.approx(150.0)

    # Unknown HA state is UNKNOWN with mean_w=None, never a guessed number.
    unknown = model.predict({"state": "unknown-mode"})
    assert unknown.status == PredictionStatus.UNKNOWN
    assert unknown.mean_w is None


def test_running_transition_counts_and_durations() -> None:
    points = _series([(5.0, 12), (150.0, 12)])
    spec = fit_dynamic_spec(points, ha_state="cool", min_change_w=15.0)
    model = DynamicModel(spec=spec, clock=lambda: T0)

    original = model.to_spec()
    model.observe(from_state="s0", to_state="s1", duration_s=120.0)
    model.observe(from_state="s0", to_state="s1", duration_s=180.0)
    updated = model.to_spec()

    assert updated.transition_counts["s0"]["s1"] == 2
    # Durations are accumulated: sample count grows and the running stat gets a
    # finite (never zero) median and an estimable spread after two observations.
    stat = updated.duration_s["s1"]
    assert stat.median_w is not None and stat.median_w > 0
    assert stat.n == original.duration_s["s1"].n + 2
    # Original spec is immutable: observations are append-only.
    assert original.transition_counts == {}
    assert original.duration_s["s1"].n < stat.n


def test_ambiguous_segmentation_yields_unknown_stage() -> None:
    """Noisy, indistinguishable levels -> UNKNOWN stage, not a guessed state."""
    import numpy as np

    rng = np.random.default_rng(20261118)
    points = [(T0 + timedelta(seconds=10 * i), 100.0 + rng.normal(0, 20)) for i in range(60)]

    recognition = recognize_dynamic_states(points, ha_state="cool", min_change_w=15.0)
    assert isinstance(recognition, StateRecognition)
    assert recognition.status.value == "unknown"
    assert recognition.state is None
    assert recognition.ambiguous is True


def test_dynamic_spec_roundtrips_through_l1_codec() -> None:
    points = _series([(5.0, 12), (150.0, 12)])
    spec = fit_dynamic_spec(points, ha_state="cool", min_change_w=15.0)
    version = _version(spec)

    encoded = encode_spec(version.spec)
    restored = decode_spec(encoded)
    assert restored == version.spec

    model = DynamicModel.from_version(version)
    assert model.from_encoded(model.encoded()).spec == model.spec

    ctx = PredictionContext(at=T0, level=PowerLevelV3.ACTIVE, attributes={"state": "cool"})
    assert model.predict(ctx.attributes, ctx.level) == DynamicModel.from_encoded(
        model.encoded()
    ).predict(ctx.attributes, ctx.level)


def test_group_segments_into_states_collapses_noisy_duplicates() -> None:
    points = _series([(5.0, 12), (150.0, 12)])
    result = segment_power_series(points, min_change_w=15.0)
    groups = group_segments_into_states(result, tolerance_w=5.0)
    assert len(groups) == 2
    assert groups[1].power_w == pytest.approx(150.0)
    assert groups[1].mad_w is None  # a single-state group has no spread claim
