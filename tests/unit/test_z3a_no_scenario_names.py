"""Z3a — production engine paths carry no HB §17 scenario knowledge.

One regression per CLASS A defect of the Z3 audit.  Every test uses reader and
subject names that share nothing with the HB §17 house (no ``Main``/``ct_main``,
no ``tv``, no ``plug``-substring trick for the declared endpoint), so a
name-based heuristic cannot pass them:

* A1 — a MEASURED/ESTIMATED level comes from the subject's power-model levels,
  or is ``None`` when there is no model; never a 3 W constant.
* A2 — the T7 relocation association is fed by *declared* single-subject
  endpoints (DEC-05), not by a ``"plug"`` substring; its pairing window is the
  configured ``stale_after_s``.
* A3 — the snapshot's ``main_w`` is the topology roots' *reading* (parallel
  mains / verified legs summed) and ``residual_w`` the roots' residual; no
  root -> ``None`` (HB §18).
* A4 — the M1 physical total adds the *root* residual, from the operator's own
  ancestry or an explicit root list, never the first reader in a dict.
* A5 — ``reconciliation_from_estimates`` takes the root explicitly.

The last test is a static guard over ``engine/wattmosaic_engine``.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    DataQualityReport,
    DeviceEstimate,
    EdgeLifecycleV3,
    EstimateStatus,
    EventGroupStatus,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    MeasurementBasis,
    MeasurementEvent,
    PowerLevelV3,
    PowerPrediction,
    PredictionStatus,
    ReaderResidualPoint,
    RelationType,
    StepClass,
    TopologyEdgeV3,
    VersionTriple,
)
from wattmosaic_engine.contracts.foundation import Bounds, ConfidenceVectorV3
from wattmosaic_engine.contracts.topology import EdgeEvidence
from wattmosaic_engine.core.runtime import RuntimeMixin
from wattmosaic_engine.core.snapshot import AssembleInputs, SnapshotAssembler
from wattmosaic_engine.electrical.topology_change import ENDPOINT_SUBJECT_PREFIX
from wattmosaic_engine.electrical.topology_graph import topology_root_readers
from wattmosaic_engine.inference.estimates import (
    level_from_model,
    reconciliation_from_estimates,
)
from wattmosaic_engine.inference.orchestrator import infer_steady
from wattmosaic_engine.mapping.projection import materialise_cells, operator_root_readers
from wattmosaic_engine.residual.reader_residual import UnknownPrior
from wattmosaic_engine.runtime.interfaces import HeldReading, PredictionContext

from tests.support.fakes import (
    FakeMapping,
    FakeResidual,
    FakeTopology,
    fake_operator,
)

T0 = datetime(2026, 3, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-z3a", mapping_version_id="map-z3a", model_set_id="mset-z3a"
)

FEED = "feed_l1"
FEED_LEG = "feed_l2"
KITCHEN = "kitchen_ckt"
LAUNDRY = "laundry_ckt"
MONITOR = "appliance_mon_3"  # a declared single-subject endpoint, no "plug" in its id
DECOY = "smartplug_7"  # "plug" in its id, but never declared single-subject
DISHWASHER = "dishwasher"


# --------------------------------------------------------------------------- #
# Shared helpers.
# --------------------------------------------------------------------------- #


def _edge(relation: RelationType, source: str, target: str) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"z3a::{relation.value}::{source}->{target}",
        relation=relation,
        source_endpoint_id=source,
        target_endpoint_id=target,
        lifecycle=EdgeLifecycleV3.CONFIRMED,
        declared_by="user",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(),
        valid_from=T0,
        pinned=True,
    )


class _LevelModels:
    """A ModelSetView whose subjects have per-level FIXED-model statistics."""

    model_set_id = "mset-z3a"

    def __init__(self, levels: dict[str, dict[PowerLevelV3, tuple[float, float]]]) -> None:
        self._levels = levels

    def predict(self, subject_id: str, ctx: PredictionContext) -> PowerPrediction:
        level = ctx.level if ctx.level is not None else PowerLevelV3.UNKNOWN
        stat = self._levels.get(subject_id, {}).get(level)
        if stat is None:
            return PowerPrediction(level=level, status=PredictionStatus.UNKNOWN)
        return PowerPrediction(
            level=level, mean_w=stat[0], sigma_w=stat[1],
            status=PredictionStatus.KNOWN, model_version_id=f"pm-{subject_id}",
        )

    def transition_prior(self, *_args: Any) -> None:
        return None

    def expected_window(self, *_args: Any) -> None:
        return None


class _NoStructure:
    def identities(self) -> dict:
        return {}

    def groups(self) -> dict:
        return {}

    def group_sums(self) -> dict:
        return {}


def _steady_dishwasher(reading_w: float, models: Any):
    """infer_steady with a dishwasher on a declared single-subject monitor."""
    topology = FakeTopology.from_tree({FEED: {KITCHEN: {}, MONITOR: {}}})
    mapping = FakeMapping.from_pairs({("device", DISHWASHER): MONITOR})
    operator = fake_operator(
        topology, mapping, (FEED, KITCHEN, MONITOR), (("device", DISHWASHER),), at=T0
    )
    held = {
        reader: HeldReading(value=value, held_since=T0, last_confirmed_at=T0, state="held")
        for reader, value in ((FEED, 900.0 + reading_w), (KITCHEN, 900.0), (MONITOR, reading_w))
    }
    result = infer_steady(
        held, operator, models,
        FakeResidual(UnknownPrior(scope_reader_id=FEED, mu_w=0.0, sigma_w=50.0, lambda_u=0.25)),
        _NoStructure(), VT, lambda: T0,
        declared_single_subject={(MONITOR, DISHWASHER)},
    )
    return next(e for e in result.device_estimates if e.subject_id == DISHWASHER)


# --------------------------------------------------------------------------- #
# A1 — the published level comes from the subject's power model.
# --------------------------------------------------------------------------- #


def test_a1_level_is_none_without_a_power_model_even_at_three_watts() -> None:
    estimate = _steady_dishwasher(3.0, _LevelModels({}))
    assert estimate.status == EstimateStatus.MEASURED
    assert estimate.point_w == pytest.approx(3.0)
    # The old code labelled any ~3 W direct reading STANDBY (an HB §17 TV value).
    assert estimate.level is None


def test_a1_level_is_the_model_level_the_reading_sits_at() -> None:
    models = _LevelModels(
        {DISHWASHER: {PowerLevelV3.STANDBY: (1.2, 0.3), PowerLevelV3.ACTIVE: (1800.0, 60.0)}}
    )
    active = _steady_dishwasher(1775.0, models)
    assert active.status == EstimateStatus.MEASURED
    assert active.level == PowerLevelV3.ACTIVE
    standby = _steady_dishwasher(1.4, models)
    assert standby.level == PowerLevelV3.STANDBY
    # 3 W is outside this model's standby (1.2 ± 3·0.3) and far from ACTIVE.
    assert _steady_dishwasher(3.0, models).level is None


def test_a1_level_from_model_uses_only_the_given_levels() -> None:
    levels = {PowerLevelV3.STANDBY: (8.0, 0.5), PowerLevelV3.IDLE: (40.0, 2.0)}
    assert level_from_model(3.0, levels) is None
    assert level_from_model(8.6, levels) == PowerLevelV3.STANDBY
    assert level_from_model(41.0, levels) == PowerLevelV3.IDLE
    assert level_from_model(3.0, None) is None
    assert level_from_model(None, levels) is None
    # A reading sigma widens the gate in quadrature.
    assert level_from_model(10.0, levels) is None
    assert level_from_model(10.0, levels, reading_sigma_w=1.0) == PowerLevelV3.STANDBY


# --------------------------------------------------------------------------- #
# A2 — relocation association from declared single-subject endpoints.
# --------------------------------------------------------------------------- #


def _association_core(*, stale_after_s: float = 120.0) -> Any:
    core = object.__new__(type("Core", (RuntimeMixin,), {}))
    endpoints = [
        {"endpoint_id": MONITOR, "kind": "appliance_monitor", "scope": "single_subject",
         "scope_status": "confirmed", "scope_subject_id": DISHWASHER},
        {"endpoint_id": DECOY, "kind": "ct", "scope": "circuit",
         "scope_status": "candidate", "scope_subject_id": None},
    ]
    core.db = SimpleNamespace(repos=SimpleNamespace(entities=SimpleNamespace(endpoints=lambda: endpoints)))
    contains = [
        {"source_endpoint_id": FEED, "target_endpoint_id": child, "relation": "contains"}
        for child in (KITCHEN, LAUNDRY, MONITOR, DECOY)
    ]
    core.version_service = SimpleNamespace(_topo=SimpleNamespace(edges=lambda: contains))
    core.config = SimpleNamespace(stale_after_s=stale_after_s)
    core._recent_step_deltas = {}
    core._association_history = {}
    return core


def _step(reader: str, at: datetime, delta_w: float) -> MeasurementEvent:
    return MeasurementEvent(
        event_id=f"mev-{reader}-{at.isoformat()}", reader_id=reader, delta_w=delta_w,
        sigma_w=1.0, z_score=9.0, step_class=StepClass.SUSTAINED, onset_lo=at,
        onset_hi=at, pre_level_w=0.0, post_level_w=delta_w, quality_score=0.9,
        detected_at=at + timedelta(seconds=30),
    )


def test_a2_declared_endpoint_feeds_the_association_history() -> None:
    core = _association_core()
    core._record_association_from_step(_step(LAUNDRY, T0, 400.0))
    core._record_association_from_step(_step(KITCHEN, T0, 1790.0))
    core._record_association_from_step(_step(MONITOR, T0 + timedelta(seconds=10), 1800.0))
    assert core._association_history == {
        f"{ENDPOINT_SUBJECT_PREFIX}{MONITOR}": deque([KITCHEN], maxlen=6)
    }


def test_a2_a_plug_named_reader_that_is_not_declared_feeds_nothing() -> None:
    core = _association_core()
    core._record_association_from_step(_step(KITCHEN, T0, 1790.0))
    core._record_association_from_step(_step(DECOY, T0 + timedelta(seconds=5), 1800.0))
    assert core._association_history == {}


def test_a2_pairing_window_is_the_configured_staleness_horizon() -> None:
    gap = timedelta(seconds=90)
    wide = _association_core(stale_after_s=120.0)
    wide._record_association_from_step(_step(KITCHEN, T0, 1790.0))
    wide._record_association_from_step(_step(MONITOR, T0 + gap, 1800.0))
    assert list(wide._association_history[f"{ENDPOINT_SUBJECT_PREFIX}{MONITOR}"]) == [KITCHEN]
    narrow = _association_core(stale_after_s=60.0)
    narrow._record_association_from_step(_step(KITCHEN, T0, 1790.0))
    narrow._record_association_from_step(_step(MONITOR, T0 + gap, 1800.0))
    assert narrow._association_history == {}


# --------------------------------------------------------------------------- #
# A3 — snapshot main reading and root residual come from topology roots.
# --------------------------------------------------------------------------- #


def _legs_topology() -> FakeTopology:
    return FakeTopology.from_edges(
        [
            _edge(RelationType.CONTAINS, FEED, KITCHEN),
            _edge(RelationType.CONTAINS, FEED, LAUNDRY),
            _edge(RelationType.PAIRED_LEG, FEED, FEED_LEG),
            _edge(RelationType.PAIRED_LEG, FEED_LEG, FEED),
        ]
    )


def test_a3_roots_come_from_the_topology_not_names() -> None:
    assert topology_root_readers(_legs_topology()) == (FEED, FEED_LEG)
    parallel = FakeTopology.from_tree({"service_a": {KITCHEN: {}}, "service_b": {LAUNDRY: {}}})
    assert topology_root_readers(parallel) == ("service_a", "service_b")
    # A reader called "Main" with no CONTAINS edge is not a root.
    flat = FakeTopology.from_edges([_edge(RelationType.CORRELATED, "Main", KITCHEN)])
    assert topology_root_readers(flat) == ()
    # A SAME_CONDUCTOR alias of a root is not counted again.
    aliased = FakeTopology.from_edges(
        [
            _edge(RelationType.CONTAINS, FEED, KITCHEN),
            _edge(RelationType.CONTAINS, "feed_alias", LAUNDRY),
            _edge(RelationType.SAME_CONDUCTOR, FEED, "feed_alias"),
        ]
    )
    (only,) = topology_root_readers(aliased)
    assert only in (FEED, "feed_alias")


def _held(**values: float) -> dict[str, HeldReading]:
    return {
        reader: HeldReading(value=value, held_since=T0, last_confirmed_at=T0, state="held")
        for reader, value in values.items()
    }


def _residual_point(reader: str, value: float) -> ReaderResidualPoint:
    return ReaderResidualPoint(
        reader_id=reader, at=T0, residual_w=value, topology_version_id=VT.topology_version_id,
        basis=MeasurementBasis.CT_MEASURED,
    )


def _empty_result() -> JointInferenceResult:
    return JointInferenceResult(
        status=EventGroupStatus.CONFIRMED, device_estimates=[], group_estimates=[],
        unknown_estimates=[], candidates=[], matches={}, residual_w_by_reader={},
        versions=VT, computed_at=T0,
    )


def _assemble(roots: tuple[str, ...]) -> Any:
    result = _empty_result()
    assembler = SnapshotAssembler(SimpleNamespace(put_snapshot=lambda _s: True), lambda: T0)
    return assembler.on_result(
        result,
        inputs=AssembleInputs(
            result=result,
            held_readings=_held(**{FEED: 610.0, FEED_LEG: 640.0, KITCHEN: 420.0, LAUNDRY: 300.0}),
            quality=DataQualityReport(timestamp=T0),
            residual_points=[
                _residual_point(KITCHEN, 35.0),
                _residual_point(FEED, 90.0),
                _residual_point(FEED_LEG, 110.0),
            ],
            root_reader_ids=roots,
        ),
    ).reconciliation


def test_a3_main_is_the_summed_root_reading_and_residual_the_root_residual() -> None:
    recon = _assemble(topology_root_readers(_legs_topology()))
    assert recon.main_w == pytest.approx(610.0 + 640.0)  # both legs' readings
    assert recon.residual_w == pytest.approx(90.0 + 110.0)  # the roots' residual


def test_a3_no_root_means_no_main_and_no_residual() -> None:
    recon = _assemble(())
    assert recon.main_w is None
    assert recon.residual_w is None


def test_a3_runtime_resolves_roots_from_the_active_topology() -> None:
    core = object.__new__(type("Core", (RuntimeMixin,), {}))
    core.version_service = SimpleNamespace(topology_view=lambda _vid: _legs_topology())
    assert core._root_readers(VT) == (FEED, FEED_LEG)
    core.version_service = None
    assert core._root_readers(VT) == ()


# --------------------------------------------------------------------------- #
# A4 — the M1 physical total adds the root residual.
# --------------------------------------------------------------------------- #


def _projection(residuals: dict[str, float], **kwargs: Any) -> Any:
    topology = FakeTopology.from_tree({FEED: {KITCHEN: {MONITOR: {}}}})
    mapping = FakeMapping.from_pairs({("device", DISHWASHER): MONITOR})
    operator = fake_operator(
        topology, mapping, (FEED, KITCHEN, MONITOR), (("device", DISHWASHER),), at=T0
    )
    estimate = DeviceEstimate(
        subject_kind="device", subject_id=DISHWASHER, point_w=1800.0,
        bounds=Bounds(lower_w=1700.0, upper_w=1900.0), status=EstimateStatus.ESTIMATED,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE, method=InferenceMethod.JOINT_DELTA,
        confidence=ConfidenceVectorV3(), source_reader_ids=[MONITOR], evidence_refs=[],
        versions=VT, updated_at=T0,
    )
    result = _empty_result()
    result.device_estimates = [estimate]
    result.residual_w_by_reader = residuals
    _cells, _totals, physical = materialise_cells(operator, result, mapping, {}, None, **kwargs)
    return operator, physical


def test_a4_physical_total_uses_the_operator_root_residual() -> None:
    # The circuit's residual is listed first; the old code took the first key.
    operator, physical = _projection({KITCHEN: 40.0, FEED: 300.0})
    assert operator_root_readers(operator) == (FEED,)
    assert physical.residual_w == pytest.approx(300.0)
    assert physical.total_w == pytest.approx(2100.0)


def test_a4_explicit_roots_override_and_no_root_is_none() -> None:
    _op, explicit = _projection({KITCHEN: 40.0, FEED: 300.0}, root_reader_ids=(KITCHEN,))
    assert explicit.residual_w == pytest.approx(40.0)
    _op, rootless = _projection({KITCHEN: 40.0, FEED: 300.0}, root_reader_ids=())
    assert rootless.residual_w is None
    assert rootless.total_w == pytest.approx(1800.0)


# --------------------------------------------------------------------------- #
# A5 — reconciliation_from_estimates takes the root explicitly.
# --------------------------------------------------------------------------- #


def test_a5_reconciliation_residual_is_the_explicit_root_residual() -> None:
    residuals = {KITCHEN: 40.0, FEED: 300.0, FEED_LEG: 120.0}
    legs = reconciliation_from_estimates(
        devices=[], residual_by_reader=residuals, main_w=2400.0, root_reader_ids=(FEED, FEED_LEG)
    )
    assert legs.residual_w == pytest.approx(420.0)
    assert legs.main_w == pytest.approx(2400.0)
    rootless = reconciliation_from_estimates(devices=[], residual_by_reader=residuals)
    assert rootless.residual_w is None  # never the first reader in the mapping
    missing = reconciliation_from_estimates(
        devices=[], residual_by_reader={KITCHEN: 40.0}, root_reader_ids=(FEED,)
    )
    assert missing.residual_w is None


# --------------------------------------------------------------------------- #
# Static guard: scenario literals stay out of non-comment engine code.
# --------------------------------------------------------------------------- #

ENGINE = Path(__file__).resolve().parents[2] / "engine" / "wattmosaic_engine"
FORBIDDEN = ("ct_main", '"Main"', '"plug" not in', "_TV_STANDBY_W")
#: Any spelling of the HB §17 reference house's name (``hb17``, ``HB17``,
#: ``topo-hb17``, ``from_hb17``...) in code is scenario knowledge; Z3b moved
#: every such seed/stand-in to ``tests/support`` or ``tools/simulator``.
FORBIDDEN_CASEFOLD = ("hb17", "hb_17")

#: Z3b emptied this allowlist (it held the CLASS B scenario stand-ins); nothing
#: may be added back.  ``relative path -> (start marker, end marker)``.
Z3B_ALLOWLIST: dict[str, tuple[str | None, str | None]] = {}


def _code_lines(source: str) -> list[str]:
    """``source`` lines with comments and docstrings blanked out."""
    lines = source.splitlines()
    blank: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                blank.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    out = [("" if number in blank else line) for number, line in enumerate(lines, start=1)]
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            row, col = token.start
            if row not in blank:
                out[row - 1] = out[row - 1][:col]
    return out


def _allowed_range(rel: str, lines: list[str]) -> range:
    if rel not in Z3B_ALLOWLIST:
        return range(0)
    start, end = Z3B_ALLOWLIST[rel]
    first = 0
    last = len(lines)
    if start is not None:
        hits = [i for i, line in enumerate(lines) if line.startswith(start)]
        if not hits:
            return range(0)  # the stand-in is gone: nothing is allowed any more
        first = hits[0]
    if end is not None:
        tails = [i for i, line in enumerate(lines) if i > first and line.startswith(end)]
        last = tails[0] if tails else last
    return range(first, last)


def test_no_scenario_literals_in_engine_code() -> None:
    offenders: list[str] = []
    for path in sorted(ENGINE.rglob("*.py")):
        rel = path.relative_to(ENGINE).as_posix()
        source = path.read_text(encoding="utf-8")
        raw = source.splitlines()
        allowed = _allowed_range(rel, raw)
        for index, line in enumerate(_code_lines(source)):
            if index in allowed:
                continue
            for literal in FORBIDDEN:
                if literal in line:
                    offenders.append(f"{rel}:{index + 1}: {literal!r}: {raw[index].strip()}")
            folded = line.casefold()
            for literal in FORBIDDEN_CASEFOLD:
                if literal in folded:
                    offenders.append(f"{rel}:{index + 1}: {literal!r}: {raw[index].strip()}")
    assert not offenders, "scenario literals in engine code:\n" + "\n".join(offenders)


def test_z3b_allowlist_is_empty() -> None:
    assert Z3B_ALLOWLIST == {}


def test_class_b_stand_ins_are_gone_from_the_engine() -> None:
    for rel in ("core/at17.py", "models/learn.py", "runtime/fakes.py"):
        assert not (ENGINE / rel).exists(), rel
    from wattmosaic_engine.inference import estimates
    from wattmosaic_engine.versioning.service import VersionService

    assert not hasattr(estimates, "build_snapshot")
    for name in ("from_hb17", "snapshot_value_at", "set_observation_snapshot", "origin"):
        assert not hasattr(VersionService, name), name


def test_static_guard_sees_code_but_not_comments_or_docstrings() -> None:
    source = 'def f():\n    """ct_main in a docstring"""\n    x = 1  # ct_main in a comment\n    y = "ct_main"\n'
    lines = _code_lines(source)
    assert [i for i, line in enumerate(lines) if "ct_main" in line] == [3]
    mixed = 'def g():\n    """HB17 in a docstring"""\n    return "topo-HB17"  # hb17\n'
    folded = [i for i, line in enumerate(_code_lines(mixed)) if "hb17" in line.casefold()]
    assert folded == [2]
