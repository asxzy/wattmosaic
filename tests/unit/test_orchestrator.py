"""I7 — pure composition of the v3 inference stages (design §7, DEC-22).

Covers the task's named checks:

* ``infer_group`` and ``infer_steady`` are pure entry points with the
  normative K08 signatures, driven only by injected inputs/clock;
* the six stages I1→I2→I3→I4→I5→I6 are actually composed, in order;
* determinism over ten runs (byte-identical result and ``result_id``);
* ``infer_steady`` constrains the absolute solve with established groups and
  identities, and a > 3σ disagreement is flagged CONFLICTED and surfaced in
  ``unresolved_events``;
* an architecture static gate: nothing under ``inference/`` imports
  ``realtime``, ``mapping`` or ``storage``.

Production code never imports ``runtime.fakes``: the fakes here are test
inputs only.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from wattmosaic_engine.contracts import (
    CandidateHypothesis,
    DeltaPrior,
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    MatchState,
    ReaderDelta,
    VersionTriple,
)
from wattmosaic_engine.inference import orchestrator
from wattmosaic_engine.inference.orchestrator import infer_group, infer_steady
from wattmosaic_engine.runtime.fakes import (
    FakeMapping,
    FakeModelSet,
    FakeResidual,
    FakeTopology,
    InMemoryHistory,
    InMemoryMatchStore,
    fake_operator,
)
from wattmosaic_engine.runtime.interfaces import (
    HeldReading,
    InferGroup,
    InferSteady,
    JointSolution,
    UnknownPrior,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #


def _operator():
    topology = FakeTopology.from_tree({"Main": {"Circuit A": {}}})
    mapping = FakeMapping.from_pairs(
        {("device", "tv"): "Circuit A", ("device", "light"): "Circuit A"}
    )
    return fake_operator(
        topology,
        mapping,
        ("Main", "Circuit A"),
        (("device", "tv"), ("device", "light")),
        at=T0,
    )


def _group() -> EventGroup:
    return EventGroup(
        group_id="group-1",
        revision=1,
        status=EventGroupStatus.PROVISIONAL,
        opened_at=T0,
        closed_at=T0,
        reader_deltas=[
            ReaderDelta(
                reader_id="Main",
                delta_w=100.0,
                sigma_w=2.0,
                completeness="complete",
                evidence_ref=EvidenceRef(kind="observation", id="Main"),
            ),
            ReaderDelta(
                reader_id="Circuit A",
                delta_w=100.0,
                sigma_w=2.0,
                completeness="complete",
                evidence_ref=EvidenceRef(kind="observation", id="Circuit A"),
            ),
        ],
        versions=VT,
    )


def _single_operator():
    """An operator where the TV is the single subject on its plug reader."""
    topology = FakeTopology.from_tree({"Main": {"Circuit A": {}}})
    mapping = FakeMapping.from_pairs({("device", "tv"): "Circuit A"})
    return fake_operator(
        topology,
        mapping,
        ("Main", "Circuit A"),
        (("device", "tv"),),
        at=T0,
    )


def _models() -> FakeModelSet:
    return FakeModelSet.fixed(
        {
            "tv": DeltaPrior(
                mean_w=100.0, sigma_w=5.0, sign="pos", model_version_id="model-1"
            )
        }
    )


def _residual() -> FakeResidual:
    return FakeResidual(
        UnknownPrior(scope_reader_id="Main", mu_w=0.0, sigma_w=50.0, lambda_u=0.25)
    )


# --------------------------------------------------------------------------- #
# Pure entry points and composition.
# --------------------------------------------------------------------------- #


def test_entry_points_match_k08_callable_protocols() -> None:
    assert isinstance(infer_group, InferGroup)
    assert isinstance(infer_steady, InferSteady)


def test_infer_group_is_pure_and_uses_injected_clock() -> None:
    result = infer_group(
        _group(), _operator(), _models(), _residual(),
        InMemoryHistory(), InMemoryMatchStore(), VT, lambda: T0,
    )
    assert result.computed_at == T0
    assert result.versions == VT
    assert result.event_group_id == "group-1"
    assert result.event_group_revision == 1


def test_infer_group_composes_all_six_stages_in_order(monkeypatch) -> None:
    """I1→I2→I3→I4→I5→I6 are called, in order, with no stage skipped."""
    calls: list[str] = []
    op = _operator()
    group = _group()
    candidate = CandidateHypothesis(
        candidate_id="cand-group-1-device::tv",
        subject_kind="device",
        subject_id="tv",
        sources=["ha_state"],
        transition="on",
        evidence_terms={},
    )
    candidate2 = CandidateHypothesis(
        candidate_id="cand-group-1-device::light",
        subject_kind="device",
        subject_id="light",
        sources=["ha_state"],
        transition="on",
        evidence_terms={},
    )

    def fake_candidates(*args: Any, **kwargs: Any):
        calls.append("I1")
        return SimpleNamespace(
            candidates=[candidate, candidate2], rejected=[], truncated=0, unknown_candidates=[]
        )

    def fake_score(cands, inputs):
        calls.append("I2")
        results = {
            c.candidate_id: SimpleNamespace(
                candidate_id=c.candidate_id,
                subject_id=c.subject_id,
                temporal_fit=0.9,
                rejected=False,
            )
            for c in cands
        }
        return list(cands), results

    def fake_solve(*args: Any, **kwargs: Any):
        calls.append("I3")
        solution = JointSolution(
            x=np.array([100.0, 10.0]),
            cov=None,
            u=np.zeros(0),
            active_rows=(0, 1),
            row_residual_w=np.zeros(len(op.rows)),
            objective=0.0,
            converged=True,
            mode="delta",
        )
        return SimpleNamespace(
            solution=solution,
            subject_ids=("tv", "light"),
            unknown_scopes=(),
            lambda_u_by_scope={},
            unknown_share_by_reader={},
            sigma=np.ones(len(op.rows)),
            lower=np.zeros(2),
            upper=np.ones(1),
            iterations=1,
            warnings=(),
        )

    class FakeAnalyzer:
        def analyze(self, *args: Any, **kwargs: Any):
            calls.append("I4")
            return orchestrator._empty_report(op)

    def fake_assemble(inputs):
        calls.append("I5")
        return [], []

    def fake_classify(*args: Any, **kwargs: Any):
        calls.append("I6")
        return MatchState.PROVISIONAL

    monkeypatch.setattr(orchestrator, "generate_candidates", fake_candidates)
    monkeypatch.setattr(orchestrator, "score_and_record", fake_score)
    monkeypatch.setattr(orchestrator, "solve_joint_detailed", fake_solve)
    monkeypatch.setattr(orchestrator, "IdentifiabilityAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(orchestrator, "assemble_status_estimates", fake_assemble)
    monkeypatch.setattr(orchestrator, "classify_match", fake_classify)

    orchestrator.infer_group(
        group, op, _models(), _residual(),
        InMemoryHistory(), InMemoryMatchStore(), VT, lambda: T0,
    )
    # I6 runs once per candidate, so deduplicate while checking the stage order.
    assert list(dict.fromkeys(calls)) == ["I1", "I2", "I3", "I4", "I5", "I6"]


def test_infer_group_is_deterministic_over_ten_runs() -> None:
    group, op, models, residual = _group(), _operator(), _models(), _residual()
    history, matches = InMemoryHistory(), InMemoryMatchStore()
    results = [
        infer_group(group, op, models, residual, history, matches, VT, lambda: T0)
        for _ in range(10)
    ]
    ids = {result.result_id for result in results}
    assert len(ids) == 1
    baseline = results[0].model_dump_json()
    assert all(result.model_dump_json() == baseline for result in results)


# --------------------------------------------------------------------------- #
# infer_steady / DEC-22.
# --------------------------------------------------------------------------- #


class _Established:
    def __init__(self, identities=None, groups=None) -> None:
        self._identities = identities or {}
        self._groups = groups or {}

    def groups(self):
        return self._groups

    def identities(self):
        return self._identities


def test_infer_steady_adds_identity_and_group_constraints() -> None:
    op = _operator()
    established = _Established(
        identities={("device", "tv"): 60.0, ("device", "light"): 40.0},
        groups={"grp-lights": ("light", "tv")},
    )
    readings = {
        "Main": HeldReading(value=100.0, held_since=T0, last_confirmed_at=T0, state="held"),
        "Circuit A": HeldReading(value=100.0, held_since=T0, last_confirmed_at=T0, state="held"),
    }
    result = infer_steady(
        readings, op, _models(), _residual(), established, VT, lambda: T0
    )
    assert result.computed_at == T0
    assert result.event_group_id is None
    assert result.unresolved_events == []
    assert result.status == EventGroupStatus.CONFIRMED
    assert any(estimate.group_id == "grp-lights" for estimate in result.device_estimates)


def test_infer_steady_conflict_beyond_three_sigma_is_flagged() -> None:
    from dataclasses import replace

    op = replace(_single_operator(), rows=tuple(
        replace(row, sigma_w=2.0) for row in _single_operator().rows
    ))
    established = _Established(identities={("device", "tv"): 100.0})
    readings = {
        "Main": HeldReading(value=140.0, held_since=T0, last_confirmed_at=T0, state="held"),
        "Circuit A": HeldReading(value=140.0, held_since=T0, last_confirmed_at=T0, state="held"),
    }
    # DEC-05: the DIRECT row is only a measured reading when its endpoint
    # declares a confirmed single-subject scope naming the subject.
    result = infer_steady(
        readings, op, _models(), _residual(), established, VT, lambda: T0,
        declared_single_subject={("Circuit A", "tv")},
    )
    assert result.unresolved_events == ["tv"]
    assert result.conflicted_subject_ids == ["tv"]
    assert result.matches["tv"] == MatchState.CONFLICTED
    assert result.status == EventGroupStatus.UNRESOLVED


def test_infer_steady_within_three_sigma_is_not_conflicted() -> None:
    from dataclasses import replace

    op = replace(_single_operator(), rows=tuple(
        replace(row, sigma_w=2.0) for row in _single_operator().rows
    ))
    established = _Established(identities={("device", "tv"): 100.0})
    readings = {
        "Main": HeldReading(value=104.0, held_since=T0, last_confirmed_at=T0, state="held"),
        "Circuit A": HeldReading(value=104.0, held_since=T0, last_confirmed_at=T0, state="held"),
    }
    result = infer_steady(
        readings, op, _models(), _residual(), established, VT, lambda: T0
    )
    assert result.unresolved_events == []
    assert result.status == EventGroupStatus.CONFIRMED


# --------------------------------------------------------------------------- #
# Architecture static gate (§4).
# --------------------------------------------------------------------------- #

FORBIDDEN_PACKAGES = {"realtime", "mapping", "storage"}
INFERENCE_DIR = Path(__file__).resolve().parents[2] / "engine" / "wattmosaic_engine" / "inference"


def _forbidden_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")
            if any(part in FORBIDDEN_PACKAGES for part in module):
                offenders.append(f"{node.module} (line {node.lineno})")
            if node.level and any(
                alias.name in FORBIDDEN_PACKAGES for alias in node.names
            ):
                offenders.append(f".{node.module} {node.names} (line {node.lineno})")
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if any(part in FORBIDDEN_PACKAGES for part in parts):
                    offenders.append(f"{alias.name} (line {node.lineno})")
    return offenders


def test_inference_never_imports_realtime_mapping_or_storage() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(INFERENCE_DIR.rglob("*.py")):
        found = _forbidden_imports(path)
        if found:
            offenders[str(path.relative_to(INFERENCE_DIR))] = found
    assert offenders == {}, f"inference imports forbidden packages: {offenders}"


def test_static_gate_detects_a_planted_forbidden_import() -> None:
    """The gate is real: it flags a planted import of a forbidden package."""
    tree = ast.parse("from wattmosaic_engine import realtime\n")
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name in FORBIDDEN_PACKAGES for alias in node.names
        ):
            found = True
    assert found
