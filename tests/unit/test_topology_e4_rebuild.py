"""E4 §1/§2: series analysis is computed once per aligned rebuild and cached.

The E3 level gate used to evaluate containment per edge per fold, which made a
10-day replay ~4x slower.  E4 computes all pair analysis once per aligned
rebuild; between rebuilds the very same objects must be returned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _core(tmp_path: object) -> EngineCore:
    now = [T0]
    db = Database(str(tmp_path / "e4.db"))  # type: ignore[attr-defined]
    engine = EngineCore(
        EngineConfig(database_path=str(tmp_path / "e4.db"), ha_token="x"),  # type: ignore[attr-defined]
        db,
        clock=lambda: now[0],
    )
    engine._wire_v3_services()  # noqa: SLF001 - production composition seam
    return engine


def test_containment_and_pair_analysis_are_reused_between_rebuilds(
    tmp_path: object,
) -> None:
    core = _core(tmp_path)
    for i in range(500):
        at = T0 + timedelta(seconds=30 * i)
        core._t5_record("feed", at, 1000.0 + (i % 5) * 20.0)  # noqa: SLF001
        core._t5_record("child", at, 400.0 + (i % 5) * 20.0)  # noqa: SLF001
    core._t5_aligned_at = T0 + timedelta(hours=10)  # noqa: SLF001 - force a fresh rebuild

    aligned = core._t5_aligned_series()  # noqa: SLF001
    assert aligned is not None
    absolute = core._t5_containment_series(aligned, ["feed", "child"])  # noqa: SLF001
    assert absolute is not None
    analysis = core._t5_pair_analysis(absolute, ["feed", "child"])  # noqa: SLF001
    assert analysis is not None

    # Repeated calls inside the same rebuild return the very same objects.
    assert core._t5_aligned_series() is aligned  # noqa: SLF001
    assert core._t5_containment_series(aligned, ["feed", "child"]) is absolute  # noqa: SLF001
    assert core._t5_pair_analysis(absolute, ["feed", "child"]) is analysis  # noqa: SLF001

    # A pair's stats are memoised on the shared analysis object.
    a = analysis.stats_for_or_compute("feed", "child")
    b = analysis.stats_for_or_compute("feed", "child")
    assert a is b
