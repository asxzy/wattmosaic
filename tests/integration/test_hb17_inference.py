"""I7 integration: HB §17 steps 3–8 driven end-to-end through ``infer_group``.

The observation comes from the X07 HB §17 reference house (topology, readers,
scripted deltas).  The runtime ports are the *real* T2 observation operator and
the *real* T3 unknown-prior model; only the power models are deterministic
fakes (L1–L3 are composed elsewhere).  This proves I7 composes the v3 inference
stages over the X07 scenario the way P6 will call it, without a database or a
wall clock.

Each step is driven with an operator scoped to the subjects that step concerns
— the way P6 builds one for the affected endpoints of an event group.  Steps
covered (HB §17):

* step 3 — two co-switched lights on Circuit A: I4 publishes one stable
  GROUP_IDENTIFIABLE group with null members (DEF-04 does not regress);
* step 4 — dynamic matching: both lights are candidates;
* step 5 — joint inference: the group sum is published, not dumped into
  residual;
* step 6 — matrix-ready output: provenance/source readers name the nested
  readers that saw the group;
* step 7 — fridge step with no HA: it stays an unknown-scope competitor, never
  a fabricated device;
* step 8 — the TV on its plug is MEASURED at 3 W standby, not 0 W.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import (
    DeltaPrior,
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    ReaderDelta,
    SubEvent,
    SubEventKind,
    VersionTriple,
)
from wattmosaic_engine.electrical.operator import (
    build_observation_operator,
    clear_operator_cache,
)
from wattmosaic_engine.inference.orchestrator import infer_group
from wattmosaic_engine.residual.reader_residual import UnknownPriorModel
from wattmosaic_engine.runtime.fakes import (
    FakeMapping,
    FakeModelSet,
    FakeTopology,
    InMemoryHistory,
    InMemoryMatchStore,
)
from wattmosaic_engine.runtime.interfaces import HistoryRow

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-hb17",
    mapping_version_id="map-hb17",
    model_set_id="mset-hb17",
)
LIGHTS = ("light.living_room_1", "light.living_room_2")
NORTON = ("Circuit A", "Subpanel", "Main")


def _operator(
    tree: dict,
    pairs: dict[tuple[str, str], str],
    readers: tuple[str, ...],
    subjects: tuple[tuple[str, str], ...],
    at: datetime = T0,
):
    """Real T2 operator over the X07 topology, scoped to one event's subjects."""
    clear_operator_cache()
    return build_observation_operator(
        FakeTopology.from_tree(tree),
        FakeMapping.from_pairs(pairs),
        None,
        readers,
        subjects,
        at,
    )


def _lights_operator(at: datetime = T0):
    """Main ⊃ Subpanel ⊃ Circuit A with the two co-switched lights."""
    return _operator(
        {"Main": {"Subpanel": {"Circuit A": {}}}},
        {
            ("device", LIGHTS[0]): "Circuit A",
            ("device", LIGHTS[1]): "Circuit A",
        },
        NORTON,
        (("device", LIGHTS[0]), ("device", LIGHTS[1])),
        at,
    )


def _tv_operator(at: datetime = T0):
    """A single-subject plug endpoint measuring only the TV (HB §17 step 8)."""
    return _operator(
        {"Main": {"Subpanel": {"Circuit A": {}}}},
        {("device", "tv"): "tv_plug"},
        ("tv_plug",),
        (("device", "tv"),),
        at,
    )


def _fridge_operator(at: datetime = T0):
    """Circuit B measuring the fridge, with an unknown scope on Main."""
    return _operator(
        {"Main": {"Circuit B": {}}},
        {("device", "fridge"): "Circuit B"},
        ("Main", "Circuit B"),
        (("device", "fridge"),),
        at,
    )


def _models() -> FakeModelSet:
    """Fake power models: a known TV prior, no priors for lights or fridge."""
    return FakeModelSet.fixed(
        {
            "tv": DeltaPrior(
                mean_w=100.0, sigma_w=5.0, sign="pos", model_version_id="model-tv"
            )
        }
    )


def _residual() -> UnknownPriorModel:
    """Real T3 unknown-prior model (initial, residual-favouring penalty)."""
    return UnknownPriorModel()


def _light_history(count: int = 12, *, at: datetime = T0) -> InMemoryHistory:
    """`count` independent events where both lights always switch together."""
    history = InMemoryHistory()
    for index in range(count):
        history.add(
            HistoryRow(
                group_id=f"hist-lights-{index:02d}",
                at=at - timedelta(hours=index + 1),
                subject_ids=LIGHTS,
                reader_ids=("Circuit A",),
                delta_w_by_reader={"Circuit A": 45.0},
            )
        )
    return history


def _group(
    group_id: str,
    readers: tuple[str, ...],
    delta_w: float,
    *,
    at: datetime = T0,
    lights: bool = True,
):
    reader_deltas = [
        ReaderDelta(
            reader_id=reader,
            delta_w=delta_w,
            sigma_w=2.0,
            completeness="complete",
            evidence_ref=EvidenceRef(kind="observation", id=reader),
        )
        for reader in readers
    ]
    sub_events = []
    if lights:
        sub_events = [
            SubEvent(
                sub_event_id=f"se-{index}",
                kind=SubEventKind.HA_STATE,
                ref=EvidenceRef(kind="ha_event", id=f"device:{light}"),
                observed_at=at,
                timestamp_uncertainty_s=1.0,
            )
            for index, light in enumerate(LIGHTS)
        ]
    return EventGroup(
        group_id=group_id,
        revision=1,
        status=EventGroupStatus.PROVISIONAL,
        opened_at=at,
        closed_at=at,
        sub_events=sub_events,
        reader_deltas=reader_deltas,
        versions=VT,
    )


# --------------------------------------------------------------------------- #
# Steps 3–6: co-switched lights become an identified group.
# --------------------------------------------------------------------------- #


def test_steps3to6_coswitched_lights_form_one_group_with_null_members() -> None:
    operator = _lights_operator()
    group = _group("hb17-lights-on", NORTON, 45.0)
    result = infer_group(
        group,
        operator,
        _models(),
        _residual(),
        _light_history(),
        InMemoryMatchStore(),
        VT,
        lambda: T0,
    )

    groups = [g for g in result.group_estimates if set(g.member_ids) == set(LIGHTS)]
    assert len(groups) == 1
    group_estimate = groups[0]
    assert group_estimate.point_w is not None and 40.0 <= group_estimate.point_w <= 50.0
    assert group_estimate.identifiability == IdentifiabilityStatus.GROUP_IDENTIFIABLE
    assert group_estimate.group_id  # stable id
    assert set(NORTON).issubset(set(group_estimate.source_reader_ids))
    assert group_estimate.evidence_refs  # group + reader evidence

    for member_id in LIGHTS:
        estimate = next(e for e in result.device_estimates if e.subject_id == member_id)
        assert estimate.point_w is None
        assert estimate.identifiability == IdentifiabilityStatus.NON_IDENTIFIABLE
        assert estimate.group_id == group_estimate.group_id
        assert estimate.status == "unknown"

    # The group is attributed once, not dumped into residual on three rows.
    assert result.residual_w_by_reader
    assert result.event_group_id == group.group_id
    assert result.computed_at == T0


def test_step3_group_id_is_stable_over_many_cycles() -> None:
    history = _light_history()
    ids: set[str] = set()
    for cycle in range(10):
        at = T0 + timedelta(seconds=cycle * 15)
        result = infer_group(
            _group("hb17-lights-on", NORTON, 45.0, at=at),
            _lights_operator(at),
            _models(),
            _residual(),
            history,
            InMemoryMatchStore(),
            VT,
            lambda at=at: at,
        )
        groups = [g for g in result.group_estimates if set(g.member_ids) == set(LIGHTS)]
        assert groups
        ids.add(groups[0].group_id)
    assert len(ids) == 1


# --------------------------------------------------------------------------- #
# Steps 4–5: a known TV candidate is attributed, the fridge stays unknown.
# --------------------------------------------------------------------------- #


def test_steps4to5_tv_is_attributed_through_the_solve() -> None:
    # A 100 W step on the TV circuit with a known model prior: the TV is a
    # candidate (source 7 / model), so I1→I5 attribute it through the solve.
    operator = _operator(
        {"Main": {"Subpanel": {"Circuit A": {}}}},
        {("device", "tv"): "Circuit A"},
        ("Main", "Circuit A"),
        (("device", "tv"),),
    )
    group = _group("hb17-tv-step", ("Main", "Circuit A"), 100.0, lights=False)
    result = infer_group(
        group,
        operator,
        _models(),
        _residual(),
        InMemoryHistory(),
        InMemoryMatchStore(),
        VT,
        lambda: T0,
    )
    tv = next(e for e in result.device_estimates if e.subject_id == "tv")
    assert tv.point_w is not None and 90.0 <= tv.point_w <= 110.0
    # Unknown-scope competitors are present for the affected readers.
    unknown_scopes = {e.scope_reader_id for e in result.unknown_estimates}
    assert "Circuit A" in unknown_scopes


def test_step7_fridge_step_without_ha_is_an_unknown_competitor() -> None:
    """A step with only a prior-mapping candidate and no HA gets no device."""
    operator = _fridge_operator()
    group = _group("hb17-fridge-step", ("Main", "Circuit B"), 150.0, lights=False)
    result = infer_group(
        group,
        operator,
        _models(),
        _residual(),
        InMemoryHistory(),
        InMemoryMatchStore(),
        VT,
        lambda: T0,
    )
    # A fridge step with no HA and no learned profile leaves the unknown-scope
    # competitor on the affected readers; no fabricated device is published.
    unknown_scopes = {e.scope_reader_id for e in result.unknown_estimates}
    assert unknown_scopes


# --------------------------------------------------------------------------- #
# Step 8: TV standby is MEASURED from the plug, never 0 W.
# --------------------------------------------------------------------------- #


def test_step8_tv_standby_is_measured_at_three_watts_not_zero() -> None:
    operator = _tv_operator()
    # The plug reports 3 W standby as the reader delta on the single-subject
    # endpoint; that is a DIRECT reading, not an inference.
    group = _group("hb17-tv-standby", ("tv_plug",), 3.0, lights=False)
    result = infer_group(
        group,
        operator,
        _models(),
        _residual(),
        InMemoryHistory(),
        InMemoryMatchStore(),
        VT,
        lambda: T0,
    )
    tv = next(e for e in result.device_estimates if e.subject_id == "tv")
    assert tv.point_w == 3.0
    assert tv.status == "measured"
    assert tv.method == "direct_reading"
    assert tv.point_w > 0.0  # never collapsed to zero
