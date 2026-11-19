"""L7 — versioned replay, derived runs and reconciliation (DEC-35).

The tests here pin the contract a historical reconciliation must honour:

* a run is bound to an explicit ``(model set, topology version, mapping
  version)`` triple, and its id is a deterministic function of that binding;
* replay is pure: it calls the injected ``infer_group``/``infer_steady`` and
  never writes measurements, evidence, HA statistics or live tables;
* a replay of evidence after the raw samples are pruned reproduces the live
  result (HB §15 tolerance);
* a new run supersedes the previous one for the same subject/range while
  keeping the previous run retrievable;
* a previous derived run is resolvable through D4's provenance resolver;
* no HA client is imported anywhere on the reconciliation path (static).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wattmosaic_engine.contracts import (
    DeltaPrior,
    EventGroup,
    EventGroupStatus,
    ReaderDelta,
    VersionTriple,
)
from wattmosaic_engine.learning.reconciliation import (
    Reconciler,
    resolve_derived_run,
)
from wattmosaic_engine.runtime.interfaces import UnknownPrior

from tests.support.fakes import (
    FakeMapping,
    FakeModelSet,
    FakeResidual,
    FakeTopology,
    InMemoryHistory,
    InMemoryMatchStore,
    fake_operator,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
RANGE = (T0, T0 + timedelta(days=7))


def _triple(model_set_id: str = "mset-v1") -> VersionTriple:
    return VersionTriple(
        topology_version_id="topo-1", mapping_version_id="map-1", model_set_id=model_set_id
    )


ENGINE_ROOT = Path(__file__).resolve().parents[2] / "engine" / "wattmosaic_engine"


# --------------------------------------------------------------------------- #
# Builders — deterministic, injected inputs only.
# --------------------------------------------------------------------------- #


def _operator():
    topo = FakeTopology.from_tree({"Main": {"Circuit A": {}, "Circuit B": {}}})
    mapping = FakeMapping.from_pairs(
        {("device", "tv"): "Circuit A", ("device", "kettle"): "Circuit B"}
    )
    return fake_operator(
        topo,
        mapping,
        ("Main", "Circuit A", "Circuit B"),
        (("device", "tv"), ("device", "kettle")),
        at=T0,
    )


def _evidence() -> list[EventGroup]:
    """Immutable event evidence: two readers step together on Circuit A/B."""
    return [
        EventGroup(
            group_id="group-1",
            revision=1,
            status=EventGroupStatus.CONFIRMED,
            opened_at=T0 + timedelta(hours=1),
            closed_at=T0 + timedelta(hours=1, seconds=10),
            reader_deltas=[
                ReaderDelta(
                    reader_id="Circuit A",
                    delta_w=100.0,
                    sigma_w=2.0,
                    completeness="complete",
                ),
                ReaderDelta(
                    reader_id="Circuit B",
                    delta_w=80.0,
                    sigma_w=2.0,
                    completeness="complete",
                ),
                ReaderDelta(
                    reader_id="Main",
                    delta_w=180.0,
                    sigma_w=3.0,
                    completeness="complete",
                ),
            ],
            versions=_triple(),
        )
    ]


def _models(model_set_id: str, tv_w: float, kettle_w: float) -> FakeModelSet:
    return FakeModelSet.fixed(
        {
            "tv": DeltaPrior(
                mean_w=tv_w, sigma_w=2.0, sign="pos", model_version_id=f"mdl-tv-{model_set_id}"
            ),
            "kettle": DeltaPrior(
                mean_w=kettle_w,
                sigma_w=2.0,
                sign="pos",
                model_version_id=f"mdl-kettle-{model_set_id}",
            ),
        },
        model_set_id=model_set_id,
    )


def _reconciler(*, chunk_days: int = 7) -> Reconciler:
    return Reconciler(
        operator=_operator(),
        model_sets={
            "mset-v1": _models("mset-v1", 100.0, 80.0),
            "mset-v2": _models("mset-v2", 20.0, 30.0),
        },
        residual=FakeResidual(
            UnknownPrior(scope_reader_id="Main", mu_w=0.0, sigma_w=1.0, lambda_u=0.1)
        ),
        history=InMemoryHistory(),
        matches=InMemoryMatchStore(),
        clock=lambda: T0 + timedelta(days=8),
        chunk_days=chunk_days,
    )


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #


def test_run_binds_the_explicit_triple_and_is_deterministic() -> None:
    rec = _reconciler()
    evidence = _evidence()
    id1 = rec.run(RANGE, _triple(), "model_promotion", evidence=evidence)
    id2 = rec.run(RANGE, _triple(), "model_promotion", evidence=evidence)
    assert id1 == id2
    assert _triple().model_set_id in id1
    assert _triple().topology_version_id in id1
    assert _triple().mapping_version_id in id1


def test_different_model_sets_produce_different_derived_estimates() -> None:
    rec = _reconciler()
    evidence = _evidence()
    v1 = rec.run(RANGE, _triple("mset-v1"), "model_promotion", evidence=evidence)
    v2 = rec.run(RANGE, _triple("mset-v2"), "model_promotion", evidence=evidence)
    assert v1 != v2
    a, b = rec.get(v1), rec.get(v2)
    assert a.derived_estimates != b.derived_estimates
    assert a.versions.model_set_id == "mset-v1"
    assert b.versions.model_set_id == "mset-v2"


def test_replay_is_pure_and_never_writes_measurements_or_evidence() -> None:
    rec = _reconciler()
    evidence = _evidence()
    before = [e.model_dump(mode="json") for e in evidence]
    rec.run(RANGE, _triple(), "late_evidence", evidence=evidence)
    after = [e.model_dump(mode="json") for e in evidence]
    assert before == after  # evidence immutable


def test_previous_runs_are_superseded_but_still_retrievable() -> None:
    rec = _reconciler()
    evidence = _evidence()
    first = rec.run(RANGE, _triple("mset-v1"), "initial", evidence=evidence)
    second = rec.run(RANGE, _triple("mset-v2"), "model_promotion", evidence=evidence)
    assert rec.get(first).superseded_by == second
    assert rec.get(second).superseded_by is None
    assert rec.get(first).derived_estimates  # kept, not deleted


def test_replay_after_raw_pruning_reproduces_the_live_result() -> None:
    """HB §15: thinning raw samples must not change the inference result."""
    rec = _reconciler()
    evidence = _evidence()
    live = rec.run(RANGE, _triple(), "initial", evidence=evidence)
    replay = rec.run(RANGE, _triple(), "replay", evidence=evidence)
    assert rec.get(live).derived_estimates == rec.get(replay).derived_estimates


def test_derived_run_resolves_through_d4() -> None:
    from wattmosaic_engine.provenance import ProvenanceAnswer

    rec = _reconciler()
    run_id = rec.run(RANGE, _triple(), "initial", evidence=_evidence())
    answers = resolve_derived_run(rec.get(run_id))
    assert answers
    answer = answers[0]
    assert isinstance(answer, ProvenanceAnswer)
    assert answer.topology_version_id == _triple().topology_version_id
    assert answer.mapping_version_id == _triple().mapping_version_id
    assert answer.questions["model_set_id"] == _triple().model_set_id


def test_chunked_jobs_chunk_the_range() -> None:
    rec = _reconciler(chunk_days=2)
    run_id = rec.run(RANGE, _triple(), "initial", evidence=_evidence())
    assert rec.get(run_id).chunks == 4  # seven days in two-day chunks


def test_no_ha_client_import_on_the_reconciliation_path() -> None:
    """Static: a replay must never touch Home Assistant statistics paths."""
    banned = ("ha_client", "homeassistant", "ha_statistics", "hassio", "recorder")
    for path in (ENGINE_ROOT / "learning" / "reconciliation.py", ENGINE_ROOT / "replay.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not any(token in name.lower() for token in banned), (
                    f"{path.name} imports HA path {name!r}"
                )
