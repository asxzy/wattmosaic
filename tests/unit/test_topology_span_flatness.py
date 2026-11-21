"""P8: "the parent did not follow" needs the parent flat over the whole anchored span.

Regression (real-history replay, 2026-11-20): 17 % of the verdicts on true
feeder/child pairs were conflict.  Classified against the raw samples, most
were a child pulse shorter than the parent's post window (61 %), a concurrent
change inside the unmeasured gap between the pre and post windows (19 %) or
the parent showing the step a sample early/late (14 %).  In all of them the
parent's net change was ~0 with flat windows, yet the parent was not flat.
Names are arbitrary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from wattmosaic_engine.electrical.topology_inference import _classify_step

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_conflict_needs_a_flat_span() -> None:
    kwargs = dict(sigma_p=5.0, sigma_c=2.0, parent_spread=3.0)
    assert _classify_step(0.0, 200.0, **kwargs) == "conflict"
    assert _classify_step(0.0, 200.0, span_deviation=4.0, **kwargs) == "conflict"
    # The parent moved by 200 W somewhere in the span and came back: neutral.
    assert _classify_step(0.0, 200.0, span_deviation=200.0, **kwargs) is None


def test_span_deviation_never_blocks_support() -> None:
    assert (
        _classify_step(198.0, 200.0, sigma_p=5.0, sigma_c=2.0, parent_spread=3.0, span_deviation=200.0)
        == "support"
    )


def _core(series: dict[str, list[tuple[float, float]]]) -> SimpleNamespace:
    from wattmosaic_engine.core.runtime import RuntimeMixin

    def window(reader: str, since: datetime, until: datetime) -> tuple[object, ...]:
        return tuple(
            SimpleNamespace(observed_at=T0 + timedelta(seconds=t), active_power_w=v)
            for t, v in series.get(reader, [])
            if since <= T0 + timedelta(seconds=t) <= until
        )

    return SimpleNamespace(
        reader_state=SimpleNamespace(window=window),
        _T5_ANCHOR_S=RuntimeMixin._T5_ANCHOR_S,
        _T5_HOLD_LOOKBACK_S=RuntimeMixin._T5_HOLD_LOOKBACK_S,
    )


def test_anchor_reports_a_concurrent_change_inside_the_gap() -> None:
    from wattmosaic_engine.core.runtime import RuntimeMixin

    # Child steps between t=60 (lo) and t=120 (hi).  The parent is flat in both
    # windows but carries a +300 W / -300 W excursion inside the gap.
    parent = [(float(t), 1000.0) for t in range(0, 61, 15)]
    parent += [(75.0, 1300.0), (90.0, 1300.0), (105.0, 1000.0)]
    parent += [(float(t), 1000.0) for t in range(120, 200, 15)]
    core = _core({"parent": parent})
    change, spread, span_deviation = RuntimeMixin._anchored_level_change(  # type: ignore[arg-type]
        core, "parent", T0 + timedelta(seconds=60), T0 + timedelta(seconds=120), lag_s=15.0
    )
    assert abs(change) < 1.0 and spread < 1.0
    assert span_deviation >= 300.0


def test_anchor_on_a_truly_flat_parent_has_no_span_deviation() -> None:
    from wattmosaic_engine.core.runtime import RuntimeMixin

    core = _core({"parent": [(float(t), 1000.0) for t in range(0, 200, 15)]})
    change, spread, span_deviation = RuntimeMixin._anchored_level_change(  # type: ignore[arg-type]
        core, "parent", T0 + timedelta(seconds=60), T0 + timedelta(seconds=120), lag_s=15.0
    )
    assert change == 0.0 and spread == 0.0 and span_deviation == 0.0
