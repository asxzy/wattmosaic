"""A child's own noise around its step floors its sigma for T5.

Regression (real-history replay, 2026-11-20): a plug feeding a fluctuating
appliance swings by 100+ W sample to sample while its long-run MAD sigma reads
~1 W, so its 40-100 W "steps" were resolvable and matched a busy cross-leg
feed by coincidence until that edge became STRUCTURAL.  Names are arbitrary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from wattmosaic_engine.electrical.topology_inference import DeltaObservation, TopologyInferenceView

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _support(**kwargs: object) -> int:
    view = TopologyInferenceView(at=T0)
    for i in range(10):
        view = view.observe_group(
            DeltaObservation(
                group_id=f"g{i}",
                deltas={"noisy_plug": (60.0, 1.0)},
                at=T0 + timedelta(minutes=i),
                cause_id=f"c{i}",
            ),
            observed={"busy_feed": (10.0, 62.0)},
            **kwargs,
        )
    edge = view.edge("busy_feed", "noisy_plug")
    return 0 if edge is None else edge.evidence.support


def test_a_step_inside_the_childs_local_noise_is_not_support() -> None:
    assert _support() == 10  # the long-run sigma alone: coincidences count
    assert _support(local_sigmas={"noisy_plug": 80.0}) == 0


def test_a_quiet_child_keeps_its_support() -> None:
    assert _support(local_sigmas={"noisy_plug": 1.5}) == 10


def test_runtime_local_sigma_is_the_spread_around_the_step() -> None:
    from wattmosaic_engine.core.runtime import RuntimeMixin

    lo, hi = T0 + timedelta(seconds=60), T0 + timedelta(seconds=75)
    values = {0: 300, 15: 180, 30: 421, 45: 288, 60: 383, 75: 222, 90: 499, 105: 413}

    def window(reader: str, since: datetime, until: datetime) -> tuple[object, ...]:
        return tuple(
            SimpleNamespace(observed_at=T0 + timedelta(seconds=t), active_power_w=float(v))
            for t, v in values.items()
            if since <= T0 + timedelta(seconds=t) <= until
        )

    core = SimpleNamespace(
        reader_state=SimpleNamespace(window=window),
        _T5_ANCHOR_S=RuntimeMixin._T5_ANCHOR_S,
        _member_step=lambda group, delta: (lo, hi),
    )
    group = SimpleNamespace(reader_deltas=[SimpleNamespace(reader_id="noisy_plug", delta_w=60.0)])
    sigmas = RuntimeMixin._local_step_sigmas(core, group)  # type: ignore[arg-type]
    assert sigmas["noisy_plug"] >= 50.0
