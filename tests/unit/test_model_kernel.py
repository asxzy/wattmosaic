"""L3: Response-kernel model.

Covers the task's acceptance points:

* the kernel separates a lamp (0 s step), an HVAC compressor (+90 s) and a
  blind (transient -> standby) (AT 5 / WP-F3),
* fitting works from aligned FINAL windows only (P4 evidence),
* ``expected_window`` matches the L1 ``expected_window_for_spec`` handoff,
* the declared delay-step fallback mirrors ``device_profile`` (223-227),
* the spec round-trips through the L1 codec (DEF-02).
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    KernelPhase,
    LevelStat,
    ModelBasis,
    ModelClass,
    PowerModelVersion,
    ResponseKernelSpec,
)
from wattmosaic_engine.models.device_profile import DevicePowerProfile
from wattmosaic_engine.models.kernel import (
    AlignedWindow,
    KernelModel,
    default_delay_kernel,
    expected_window_from_spec,
    fit_kernel_from_windows,
)
from wattmosaic_engine.models.prediction import expected_window_for_spec
from wattmosaic_engine.models.spec import decode_spec, encode_spec

from tools.simulator.hb17 import BLIND_STANDBY_W, BLIND_TRANSIENT_S, HVAC_DELAY_S

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _version(spec: ResponseKernelSpec, n: int = 1) -> PowerModelVersion:
    return PowerModelVersion(
        version_id=f"mdl-kernel-{n}",
        subject_kind="device",
        subject_id="dev",
        number=n,
        model_class=ModelClass.RESPONSE_KERNEL,
        basis=ModelBasis.DELTA,
        spec=spec,
        trainer_version="test-L3",
        created_at=T0,
    )


def test_kernel_separates_lamp_hvac_and_blind() -> None:
    """0 s lamp step, +90 s compressor step, transient-then-standby blind."""
    lamp = KernelModel.declared(0.2, 60.0, delay_s=0.0)
    hvac = KernelModel.declared(5.0, 1500.0, delay_s=HVAC_DELAY_S)
    blind = KernelModel(
        spec=ResponseKernelSpec(
            by_transition={
                "open": [
                    KernelPhase(
                        phase="startup",
                        delay_s=LevelStat(median_w=0.0),
                        amplitude_w=LevelStat(median_w=60.0),
                        duration_s=LevelStat(median_w=BLIND_TRANSIENT_S),
                    ),
                    KernelPhase(
                        phase="standby",
                        delay_s=LevelStat(median_w=0.0),
                        amplitude_w=LevelStat(median_w=BLIND_STANDBY_W),
                        duration_s=LevelStat(median_w=1000.0),
                    ),
                ]
            }
        )
    )

    tau = np.array([1.0, HVAC_DELAY_S - 1, HVAC_DELAY_S + 1, BLIND_TRANSIENT_S + 2])
    lamp_mean, _ = lamp.expected_window("on", {}, tau)
    hvac_mean, _ = hvac.expected_window("on", {}, tau)
    blind_mean, _ = blind.expected_window("open", {}, tau)

    # Lamp responds immediately (~1 s), HVAC not until 90 s.
    assert lamp_mean[0] == pytest.approx(60.0)
    assert hvac_mean[0] == pytest.approx(5.0)
    assert hvac_mean[1] == pytest.approx(5.0)
    assert hvac_mean[2] == pytest.approx(1500.0)
    # Blind's transient gives way to a small standby draw.
    assert blind_mean[0] == pytest.approx(60.0)
    assert blind_mean[3] == pytest.approx(BLIND_STANDBY_W)


def test_fit_kernel_uses_only_final_windows() -> None:
    final = AlignedWindow(
        offsets_s=(0.0, 5.0, 10.0),
        power_w=(150.0, 150.0, 150.0),
        finalized=True,
        transition="on",
    )
    provisional = AlignedWindow(
        offsets_s=(0.0, 5.0, 10.0),
        power_w=(900.0, 900.0, 900.0),
        finalized=False,
        transition="on",
    )
    spec = fit_kernel_from_windows([final, provisional], transition="on")
    phases = spec.by_transition["on"]
    assert len(phases) == 1
    assert phases[0].amplitude_w.median_w == pytest.approx(150.0)


def test_fit_kernel_on_empty_windows_returns_no_phases() -> None:
    spec = fit_kernel_from_windows([], transition="on")
    assert spec.by_transition == {}
    model = KernelModel(spec=spec)
    assert model.expected_window("on", {}, np.array([0.0])) is None


def test_expected_window_matches_l1_handoff() -> None:
    """The kernel's window must equal the registry's L1 expected_window."""
    spec = default_delay_kernel(5.0, 150.0, delay_s=30.0)
    version = _version(spec)
    tau = np.array([0.0, 29.0, 30.0, 60.0])

    from_module = expected_window_from_spec(spec, "on", {}, tau)
    from_l1 = expected_window_for_spec(version, "on", {}, tau)
    assert from_module is not None and from_l1 is not None
    np.testing.assert_array_equal(from_module[0], from_l1[0])
    np.testing.assert_array_almost_equal(from_module[1], from_l1[1])


def test_declared_delay_step_mirrors_device_profile() -> None:
    """The fallback is device_profile's declared delay step (223-227)."""
    profile = DevicePowerProfile.from_mapping(
        "compressor", {"on_w": 1500.0, "standby_w": 5.0}
    ).with_response_delay(HVAC_DELAY_S)
    kernel = KernelModel.declared(
        profile.standby_w, profile.operating_w(), delay_s=profile.response_delay_s
    )
    mean, _ = kernel.expected_window("on", {}, np.array([0.0, 89.0, 90.0, 91.0]))
    assert mean[0] == pytest.approx(5.0)
    assert mean[1] == pytest.approx(5.0)
    assert mean[2] == pytest.approx(1500.0)
    assert mean[3] == pytest.approx(1500.0)


def test_uncovered_tau_is_nan_not_zero() -> None:
    """A τ not covered by any phase is NaN (absent), never 0 W."""
    spec = ResponseKernelSpec(
        by_transition={
            "on": [
                KernelPhase(
                    phase="active",
                    delay_s=LevelStat(median_w=0.0),
                    amplitude_w=LevelStat(median_w=100.0),
                    duration_s=LevelStat(median_w=10.0),
                )
            ]
        }
    )
    mean, sigma = expected_window_from_spec(spec, "on", {}, np.array([5.0, 50.0]))
    assert mean[0] == pytest.approx(100.0)
    assert np.isnan(mean[1])
    assert np.isnan(sigma[1])


def test_kernel_spec_roundtrips_through_l1_codec() -> None:
    spec = fit_kernel_from_windows(
        [AlignedWindow(offsets_s=(0.0, 10.0), power_w=(5.0, 150.0))], transition="on"
    )
    version = _version(spec)
    restored = decode_spec(encode_spec(version.spec))
    assert restored == version.spec

    model = KernelModel.from_version(version)
    assert model.from_encoded(model.encoded()).spec == model.spec
    # The kernel's steady phase starts at the fitted 10 s delay; τ=0 is
    # uncovered (NaN, not a fabricated 0 W).
    mean, _ = model.from_encoded(model.encoded()).expected_window("on", {}, np.array([0.0, 10.0]))
    assert np.isnan(mean[0])
    assert mean[1] == pytest.approx(77.5)


def test_missing_transition_is_unknown_window_not_flat_zero() -> None:
    model = KernelModel.declared(5.0, 60.0, delay_s=0.0)
    assert model.expected_window("off", {}, np.array([0.0])) is None
