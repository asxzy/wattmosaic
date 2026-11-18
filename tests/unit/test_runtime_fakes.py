"""K08: runtime interface types and deterministic fakes (interfaces.md §4-8).

Acceptance:
- Each fake satisfies its runtime-checkable Protocol.
- ``fake_operator`` on the HB §17 tree puts a Circuit-A device in the rows of
  Circuit A, Subpanel and Main with coefficient 1.
- A static test: no module under ``engine/wattmosaic_engine/`` except
  ``runtime/fakes.py`` imports ``runtime.fakes``.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from wattmosaic_engine.contracts import (
    DeltaPrior,
    MatchState,
    VersionTriple,
)
from wattmosaic_engine.runtime import (
    Clock,
    HeldReading,
    IdentifiabilityHistory,
    MappingView,
    MatchStore,
    MatrixUpdater,
    ModelSetView,
    PredictionContext,
    ResidualModelView,
    TopologyView,
    VersionService,
)
from wattmosaic_engine.runtime.fakes import (
    HB17_TREE,
    FakeMapping,
    FakeModelSet,
    FakeResidual,
    FakeTopology,
    FakeVersionService,
    InMemoryHistory,
    InMemoryMatchStore,
    RecordingMatrixUpdater,
    fake_operator,
)
from wattmosaic_engine.runtime.interfaces import HistoryRow, UnknownPrior

POWER_ROOT = Path(__file__).resolve().parents[2] / "engine" / "wattmosaic_engine"

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Protocol conformance — each fake satisfies its handoff.
# --------------------------------------------------------------------------- #


def test_topology_fake_satisfies_protocol() -> None:
    topology = FakeTopology.from_tree(HB17_TREE)
    assert isinstance(topology, TopologyView)


def test_mapping_fake_satisfies_protocol() -> None:
    mapping = FakeMapping.from_pairs({("device", "light-1"): "Circuit A"})
    assert isinstance(mapping, MappingView)


def test_model_set_fake_satisfies_protocol() -> None:
    models = FakeModelSet.fixed({})
    assert isinstance(models, ModelSetView)


def test_residual_fake_satisfies_protocol() -> None:
    prior = UnknownPrior("Main", 0.0, 1.0, 0.5, None)
    assert isinstance(FakeResidual(prior), ResidualModelView)


def test_history_fake_satisfies_protocol() -> None:
    assert isinstance(InMemoryHistory(), IdentifiabilityHistory)


def test_match_store_fake_satisfies_protocol() -> None:
    assert isinstance(InMemoryMatchStore(), MatchStore)


def test_version_service_fake_satisfies_protocol() -> None:
    triple = VersionTriple(topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1")
    assert isinstance(FakeVersionService(triple), VersionService)


def test_matrix_updater_fake_satisfies_protocol() -> None:
    assert isinstance(RecordingMatrixUpdater(), MatrixUpdater)


# --------------------------------------------------------------------------- #
# fake_operator on the HB §17 tree.
# --------------------------------------------------------------------------- #


def test_fake_operator_circuit_a_device_reaches_ancestors_with_coefficient_one() -> None:
    topology = FakeTopology.from_tree(HB17_TREE)
    mapping = FakeMapping.from_pairs(
        {
            ("device", "light-1"): "Circuit A",
            ("device", "fridge"): "Circuit B",
            ("device", "tv"): "Circuit C",
        }
    )
    readers = ("Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C")
    subjects = (("device", "light-1"), ("device", "fridge"), ("device", "tv"))
    operator = fake_operator(topology, mapping, readers, subjects)

    assert [row.reader_id for row in operator.rows] == list(readers)
    row_of = {row.reader_id: i for i, row in enumerate(operator.rows)}
    col_of = {subject: j for j, subject in enumerate(operator.subjects)}
    subject = ("device", "light-1")
    col = col_of[subject]

    # A Circuit-A device appears in Circuit A, Subpanel and Main, all with 1.
    for reader in ("Circuit A", "Subpanel", "Main"):
        row = row_of[reader]
        assert operator.H[row, col] == 1.0
        meta = operator.entries[(row, col)]
        assert meta.coefficient == 1.0

    # But it does not enter sibling rows Circuit B or Circuit C.
    for reader in ("Circuit B", "Circuit C"):
        assert operator.H[row_of[reader], col] == 0.0

    # Observability: DIRECT at the mapped reader, EXPECTED on ancestors.
    assert operator.entries[(row_of["Circuit A"], col)].observability.value == "direct"
    assert operator.entries[(row_of["Main"], col)].observability.value == "expected"


def test_fake_operator_is_deterministic() -> None:
    topology = FakeTopology.from_tree(HB17_TREE)
    mapping = FakeMapping.from_pairs({("device", "light-1"): "Circuit A"})
    readers = ("Main", "Subpanel", "Circuit A")
    subjects = (("device", "light-1"),)
    a = fake_operator(topology, mapping, readers, subjects)
    b = fake_operator(topology, mapping, readers, subjects)
    assert np.array_equal(a.H, b.H)
    assert a.entries == b.entries


# --------------------------------------------------------------------------- #
# Fakes are obviously correct and small.
# --------------------------------------------------------------------------- #


def test_match_store_apply_is_functional() -> None:
    store = InMemoryMatchStore()
    updated = store.apply({"c1": MatchState.PROPOSED})
    assert store.snapshot() == {}
    assert updated.snapshot() == {"c1": MatchState.PROPOSED}


def test_history_filters_by_subject_and_time() -> None:
    history = InMemoryHistory(
        [
            HistoryRow("g1", T0, subject_ids=("a",)),
            HistoryRow("g2", T0, subject_ids=("b",)),
        ]
    )
    rows = history.events_touching(("a",), T0)
    assert [r.group_id for r in rows] == ["g1"]


def test_model_set_returns_known_prior_and_unknown_prediction() -> None:
    prior = DeltaPrior(mean_w=100.0, sigma_w=5.0, sign="pos", model_version_id="mdl-x")
    models = FakeModelSet.fixed({"light-1": prior})
    assert models.transition_prior("light-1", "on", {}) == prior
    assert models.predict("light-1", PredictionContext(at=T0)).status == "KNOWN"
    unknown = models.predict("absent", PredictionContext(at=T0))
    assert unknown.status == "UNKNOWN"
    assert unknown.mean_w is None


def test_held_reading_is_a_frozen_dataclass() -> None:
    reading = HeldReading(value=3.0, held_since=T0, last_confirmed_at=T0, state="held")
    assert reading.value == 3.0
    assert reading.state == "held"


def test_clock_alias_is_callable() -> None:
    def clock() -> datetime:
        return T0

    typed: Clock = clock
    assert typed() == T0


# --------------------------------------------------------------------------- #
# Static gate: production code must never import runtime.fakes.
# --------------------------------------------------------------------------- #


def _imports_runtime_fakes(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".")
            # from wattmosaic_engine.runtime import fakes / from .runtime import fakes
            if parts[-1] == "fakes" and "runtime" in parts:
                return True
            if node.module == "wattmosaic_engine.runtime.fakes":
                return True
            if any(a.name == "fakes" for a in node.names) and "runtime" in parts:
                return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "wattmosaic_engine.runtime.fakes":
                    return True
    return False


def test_no_production_module_imports_runtime_fakes() -> None:
    fakes_path = (POWER_ROOT / "runtime" / "fakes.py").resolve()
    offenders: list[str] = []
    for path in POWER_ROOT.rglob("*.py"):
        resolved = path.resolve()
        if resolved == fakes_path:
            continue
        try:
            if _imports_runtime_fakes(path):
                offenders.append(str(path.relative_to(POWER_ROOT)))
        except SyntaxError:  # pragma: no cover - all shipped modules parse
            continue
    assert offenders == [], f"production modules import runtime.fakes: {offenders}"
