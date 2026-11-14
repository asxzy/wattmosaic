"""Refoss factor semantics and measurement basis (WP3).

The rule this file enforces is arithmetic, not lexical: ``applied_by`` says
**who applied the multiplier**, and that decides whether the engine may apply it.

* ``monitor`` -- the Refoss monitor already published the whole load.  The
  engine must apply **nothing** (basis ``MONITOR_REPORTED``).
* ``user`` -- the multiplier is a declaration the engine still owes the raw
  reading.  It applies it **exactly once** (basis ``FACTOR_ADJUSTED``).
* ``unknown`` -- the semantics cannot be established.  The basis is ``UNKNOWN``,
  confidence is lowered, and neither multiplying nor "not multiplying as if we
  knew" is allowed.

Everything else here is a consequence: a factor is resolved against the interval
that covers the *sample's* timestamp (never today's factor retroactively); a gap
is "no metadata" and not an implicit x1; and two CTs on one appliance are an
overlap, never two independent loads.

The Hot Tub fixtures come from WP2 (``tools.simulator.hot_tub``) unchanged.  The
tests translate the fixture's *observable* declaration frame into contract
``MeasurementFactor`` rows; nothing reads the fixture's ground truth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    MeasurementBasis,
    MeasurementFactor,
    Phase,
    RelationKind,
    Topology,
)
from wattmosaic_engine.electrical.factors import (
    UNKNOWN_CONFIDENCE,
    FactorRegistry,
    classify_basis,
    resolve_factor_ownership,
    scale_reading,
)
from wattmosaic_engine.electrical.topology import build_graph
from wattmosaic_engine.storage.database import Database

from tools.simulator.hot_tub import (
    CT_L1,
    CT_L2,
    DEVICE_ID,
    HOT_TUB_VARIANTS,
    HotTubFixture,
    hot_tub,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _t(seconds: float, base: datetime = T0) -> datetime:
    return base + timedelta(seconds=seconds)


def _factor(
    *,
    ct_id: str = "ct_a",
    multiplier: float = 2.0,
    effective_from: datetime = T0,
    effective_to: datetime | None = None,
    applied_by: str = "user",
    declared_by: str = "user",
    note: str | None = None,
) -> MeasurementFactor:
    return MeasurementFactor(
        ct_id=ct_id,
        multiplier=multiplier,
        effective_from=effective_from,
        effective_to=effective_to,
        applied_by=applied_by,
        declared_by=declared_by,
        note=note,
    )


# --------------------------------------------------------------------------- #
# Fixture adapters: observable declaration -> contract rows
# --------------------------------------------------------------------------- #
#
# `tools.simulator.hot_tub` exposes the factor declarations as part of the
# *observation* view (`factor_declarations`), never the ground truth.  The
# translation below is the only evidence this file uses: the declared role
# ("this CT is clamped on one leg of the appliance and therefore needs a factor
# to become an appliance total") and the declared intervals.  Nothing is read
# from `phase`, `name` or `nominal_voltage`, which is the whole point of
# `classify_basis`.


def _ci_registry(fixture: HotTubFixture) -> tuple[FactorRegistry, dict[str, str]]:
    declarations = fixture.observations()["factor_declarations"]
    rows: list[MeasurementFactor] = []
    conflicts: dict[str, str] = {}
    requires: list[str] = []
    for ct_id, decl in declarations.items():
        if decl["role"] == "leg_conductor":
            requires.append(str(ct_id))
        for interval in decl["intervals"]:
            if interval["published"] and interval["applied_by"] == "user":
                # The monitor published the value *and* a user still claims to
                # owe the multiplication.  Exactly one can be true, so the
                # ownership is not established.
                conflicts[str(ct_id)] = "factor_declared_user_applied_and_already_published"
                continue
            if interval["applied_by"] == "none":
                continue
            rows.append(
                MeasurementFactor(
                    ct_id=str(ct_id),
                    multiplier=float(interval["multiplier"]),
                    effective_from=_t(float(interval["start_s"]), fixture.start_time),
                    effective_to=(
                        None
                        if interval["end_s"] is None
                        else _t(float(interval["end_s"]), fixture.start_time)
                    ),
                    applied_by=str(interval["applied_by"]),
                    declared_by="user",
                    note=interval["note"] or None,
                )
            )
    registry = FactorRegistry(rows, requires_factor=requires, conflicts=conflicts)
    return registry, conflicts


def _ci_channels(fixture: HotTubFixture) -> list[CTChannel]:
    """Contract channels mirroring the fixture's *declared* wiring.

    The legacy ``measurement_multiplier`` scalar is set the way production would
    hold it: the user's factor when the user owns the multiplication, and 1.0
    when the monitor already applied it.  That is what makes the
    "scalar would re-apply a monitor factor" conflict detectable.
    """
    pairs = _ci_pairs(fixture)
    partner = {a: b for a, b in pairs} | {b: a for a, b in pairs}
    declarations = fixture.observations()["factor_declarations"]
    channels: list[CTChannel] = []
    for item in fixture.cts:
        intervals = declarations[item.ct_id]["intervals"]
        scalar = 1.0
        if intervals:
            first = intervals[0]
            if first["applied_by"] == "user" and not first["published"]:
                scalar = float(first["multiplier"])
        channels.append(
            CTChannel(
                ct_id=item.ct_id,
                entity_id=item.entity_id,
                ct_type=CTType.DEDICATED_DEVICE,
                phase=item.leg,
                device_id=DEVICE_ID,
                paired_ct_id=partner.get(item.ct_id),
                measurement_multiplier=scalar,
            )
        )
    return channels


def _ci_pairs(fixture: HotTubFixture) -> list[tuple[str, str]]:
    """Double-pole pairs, from the fixture's declared role, never from phase."""
    declarations = fixture.observations()["factor_declarations"]
    legs = [
        str(ct_id)
        for ct_id, decl in declarations.items()
        if decl["role"] == "leg_conductor"
    ]
    if len(legs) == 2:
        return [(legs[0], legs[1])]
    return []


def _ci_ownership(
    fixture: HotTubFixture,
    *,
    ct_id: str | None = None,
    at_s: float,
    evidence_conflict: str | None = None,
) -> Any:
    registry, _ = _ci_registry(fixture)
    channels = _ci_channels(fixture)
    first = fixture.cts[0].ct_id if ct_id is None else ct_id
    return resolve_factor_ownership(
        first,
        registry.factors,
        channels,
        _ci_pairs(fixture),
        timestamp=_t(at_s, fixture.start_time),
        requires_factor=[c.ct_id for c in channels if _declares_leg(fixture, c.ct_id)],
        evidence_conflict=evidence_conflict,
    )


def _declares_leg(fixture: HotTubFixture, ct_id: str) -> bool:
    return fixture.observations()["factor_declarations"][ct_id]["role"] == "leg_conductor"


# --------------------------------------------------------------------------- #
# Interval resolution
# --------------------------------------------------------------------------- #


def test_factor_at_resolves_only_the_covering_interval() -> None:
    reg = FactorRegistry(
        [
            _factor(multiplier=1.0, effective_from=T0, effective_to=_t(600)),
            _factor(multiplier=2.0, effective_from=_t(600)),
        ]
    )
    assert reg.factor_at("ct_a", _t(599)).multiplier == 1.0
    assert reg.factor_at("ct_a", _t(600)).multiplier == 2.0
    assert reg.factor_at("ct_a", _t(10_000)).multiplier == 2.0


def test_factor_at_returns_none_before_the_first_interval() -> None:
    """A reading from before any declaration has no metadata, not the future one."""
    reg = FactorRegistry([_factor(multiplier=2.0, effective_from=_t(600))])
    assert reg.factor_at("ct_a", _t(0)) is None
    assert reg.factor_at("ct_a", _t(599)) is None
    assert reg.describe("ct_a", _t(0))["has_metadata"] is False


def test_a_gap_between_intervals_is_no_metadata_not_one() -> None:
    reg = FactorRegistry(
        [
            _factor(multiplier=2.0, effective_from=T0, effective_to=_t(600)),
            _factor(multiplier=2.0, effective_from=_t(1200)),
        ]
    )
    gap = reg.resolve("ct_a", _t(900))
    assert reg.factor_at("ct_a", _t(900)) is None
    assert gap.has_metadata is False
    assert gap.reason_code == "factor_gap"
    # The existence of a history proves the channel is factor-managed, so a gap
    # cannot be read as a deliberate x1.
    assert gap.basis is MeasurementBasis.UNKNOWN
    assert gap.confidence < 1.0
    assert gap.applied_multiplier == 1.0
    assert gap.reported_multiplier == 1.0


def test_overlapping_intervals_are_refused_at_construction() -> None:
    """A registry can never answer with a later interval for an earlier sample."""
    with pytest.raises(ValueError):
        FactorRegistry(
            [
                _factor(effective_from=T0),
                _factor(effective_from=_t(60)),
            ]
        )


def test_a_ct_with_no_factor_history_at_all_is_ct_measured() -> None:
    reg = FactorRegistry([])
    res = reg.resolve("ct_a", _t(0))
    assert res.basis is MeasurementBasis.CT_MEASURED
    assert res.reason_code == "no_factor_declared"
    assert res.has_metadata is False
    assert res.confidence == 1.0


# --------------------------------------------------------------------------- #
# classify_basis: explicit evidence only
# --------------------------------------------------------------------------- #


def test_classify_basis_never_derives_from_phase_ct_type_or_name() -> None:
    """A 240 V double-pole channel with a suggestive name is still plain measured.

    Inferring a x2 here is exactly the reported double-count: the monitor
    "commonly" applies the multiplier itself and already publishes the load.
    """
    channel = CTChannel(
        ct_id="ct_hot_tub_240v",
        entity_id="sensor.hot_tub_power",
        name="Hot Tub 240 V Double Pole",
        ct_type=CTType.DEDICATED_DEVICE,
        phase=Phase.BOTH,
        nominal_voltage=240.0,
        device_id="hot_tub",
    )
    reg = FactorRegistry([])
    assert classify_basis(None) is MeasurementBasis.CT_MEASURED
    assert reg.basis_at(channel.ct_id, _t(0)) is MeasurementBasis.CT_MEASURED
    assert reg.describe(channel.ct_id, _t(0))["why"]
    # The channel declaration alone never turns into a factor.
    assert channel.measurement_multiplier == 1.0


def test_classify_basis_cannot_be_given_an_inference_hint() -> None:
    """The signature is the guard: there is no way to pass phase/type/name.

    A behavioural test cannot prove the *absence* of a code path, so this
    inspects the signature too: adding ``phase`` (or ``ct_type``, ``name``,
    ``voltage``) as a parameter would fail here.
    """
    import inspect

    params = set(inspect.signature(classify_basis).parameters)
    for forbidden in ("phase", "ct_type", "cttype", "name", "voltage", "nominal_voltage"):
        assert forbidden not in params, f"classify_basis must not accept {forbidden!r}"


def test_classify_basis_monitor_applied_is_monitor_reported() -> None:
    assert classify_basis(_factor(applied_by="monitor")) is MeasurementBasis.MONITOR_REPORTED


def test_classify_basis_user_factor_is_factor_adjusted() -> None:
    assert classify_basis(_factor(applied_by="user", multiplier=2.0)) is MeasurementBasis.FACTOR_ADJUSTED


def test_classify_basis_a_declared_x1_is_ct_measured() -> None:
    """A factor row exists but it is a deliberate x1: measured, not adjusted."""
    assert classify_basis(_factor(applied_by="user", multiplier=1.0)) is MeasurementBasis.CT_MEASURED


def test_classify_basis_unknown_ownership_is_unknown() -> None:
    assert classify_basis(_factor(applied_by="unknown")) is MeasurementBasis.UNKNOWN


def test_classify_basis_missing_metadata_is_unknown_only_when_expected() -> None:
    assert classify_basis(None) is MeasurementBasis.CT_MEASURED
    assert classify_basis(None, requires_factor=True) is MeasurementBasis.UNKNOWN


def test_classify_basis_estimated_appliance_total_is_explicit_only() -> None:
    assert (
        classify_basis(None, estimated_appliance_total=True)
        is MeasurementBasis.ESTIMATED_APPLIANCE_TOTAL
    )


def test_classify_basis_conflict_is_unknown() -> None:
    assert (
        classify_basis(_factor(applied_by="monitor"), conflict="two_owners")
        is MeasurementBasis.UNKNOWN
    )


def test_registry_classification_is_derived_from_the_same_facts_as_resolution() -> None:
    """D1: a caller must not be able to restate the facts and disagree.

    ``classify_basis(None)`` alone means "no factor was ever declared", i.e. a
    plain conductor reading.  Inside a factor-managed history the *same* ``None``
    means "gap" or "before the first declaration" and must be ``UNKNOWN``.  The
    registry therefore exposes ``facts_at``/``classify`` so the classification is
    a projection of ``resolve`` instead of a second, hand-written opinion.
    """
    reg = FactorRegistry(
        [
            _factor(ct_id="covered_user", multiplier=2.0, applied_by="user"),
            _factor(ct_id="covered_monitor", multiplier=2.0, applied_by="monitor"),
            _factor(ct_id="covered_one", multiplier=1.0, applied_by="user"),
            _factor(ct_id="unknown_owner", multiplier=2.0, applied_by="unknown"),
            _factor(
                ct_id="gap",
                multiplier=2.0,
                applied_by="user",
                effective_from=T0,
                effective_to=_t(600),
            ),
            _factor(ct_id="gap", multiplier=2.0, applied_by="user", effective_from=_t(1200)),
            _factor(ct_id="future", multiplier=2.0, applied_by="user", effective_from=_t(600)),
        ],
        requires_factor=frozenset({"needs_factor"}),
        conflicts={"conflicted": "two_owners"},
    )
    cases: dict[str, tuple[datetime, MeasurementBasis]] = {
        "covered_user": (_t(0), MeasurementBasis.FACTOR_ADJUSTED),
        "covered_monitor": (_t(0), MeasurementBasis.MONITOR_REPORTED),
        "covered_one": (_t(0), MeasurementBasis.CT_MEASURED),
        "unknown_owner": (_t(0), MeasurementBasis.UNKNOWN),
        "gap": (_t(900), MeasurementBasis.UNKNOWN),
        "future": (_t(0), MeasurementBasis.UNKNOWN),
        "needs_factor": (_t(0), MeasurementBasis.UNKNOWN),
        "conflicted": (_t(0), MeasurementBasis.UNKNOWN),
        "never_declared": (_t(0), MeasurementBasis.CT_MEASURED),
    }
    for ct_id, (when, expected) in cases.items():
        resolution = reg.resolve(ct_id, when)
        assert resolution.basis is expected, ct_id
        # Both accessors must equal ``resolve`` -- that is the whole point.
        assert reg.classify(ct_id, when) is expected, ct_id
        assert classify_basis(**reg.facts_at(ct_id, when)) is expected, ct_id

    # The gap is the case that used to disagree: a bare ``None`` says "measured",
    # the registry's own facts say "no metadata at this instant".
    assert classify_basis(None) is MeasurementBasis.CT_MEASURED
    assert reg.describe("gap", _t(900))["has_metadata"] is False
    assert reg.classify("gap", _t(900)) is MeasurementBasis.UNKNOWN
    assert reg.classify("gap", _t(900)) is not classify_basis(None)
    # Gaps and pre-history are distinguishable, not one anonymous "unknown".
    assert reg.resolve("gap", _t(900)).reason_code == "factor_gap"
    assert reg.resolve("future", _t(0)).reason_code == "factor_not_yet_declared"


def test_resolve_factor_ownership_requires_an_explicit_timestamp() -> None:
    """D2: "the factor in force now" may never be the hidden default.

    Resolving a historical sample against the wall clock is the retroactive
    application this module forbids.  Making ``timestamp`` required and
    keyword-only moves that decision into every call site.
    """
    import inspect

    parameters = inspect.signature(resolve_factor_ownership).parameters
    timestamp = parameters["timestamp"]
    assert timestamp.kind is inspect.Parameter.KEYWORD_ONLY
    assert timestamp.default is inspect.Parameter.empty

    with pytest.raises(TypeError):
        resolve_factor_ownership("ct_a", [], [], ())  # type: ignore[call-arg]


def test_the_module_docstring_names_every_scaling_site() -> None:
    """The "applied once" rule is only true if every site is listed.

    Rule 4 used to name one site and say the factor is applied in one place,
    which hid two real multiplications.  The docstring must name all three so a
    reader auditing double application is not sent to a false location.
    """
    import wattmosaic_engine.electrical.factors as factors_module

    text = factors_module.__doc__ or ""
    for site in (
        "scale_reading",
        "AssociationFinder._classify",
        "ResidualAnalyzer._counted_contributions",
    ):
        assert site in text, f"scaling site {site!r} is missing from the docstring"


def test_applied_and_reported_multipliers_depend_on_who_applied_the_factor() -> None:
    user = FactorRegistry([_factor(multiplier=2.0, applied_by="user")])
    monitor = FactorRegistry([_factor(multiplier=2.0, applied_by="monitor")])
    assert user.applied_multiplier_at("ct_a", _t(0)) == 2.0
    assert user.reported_multiplier_at("ct_a", _t(0)) == 1.0
    assert monitor.applied_multiplier_at("ct_a", _t(0)) == 1.0
    assert monitor.reported_multiplier_at("ct_a", _t(0)) == 2.0


def test_confidence_factor_is_one_when_semantics_are_established() -> None:
    for applied_by in ("monitor", "user"):
        reg = FactorRegistry([_factor(applied_by=applied_by)])
        assert reg.confidence_factor_at("ct_a", _t(0)) == 1.0
    unknown = FactorRegistry([_factor(applied_by="unknown")])
    conf = unknown.confidence_factor_at("ct_a", _t(0))
    assert 0.0 < conf < 1.0
    assert conf == UNKNOWN_CONFIDENCE


def test_basis_for_returns_the_frozen_wp5_wp7_tuple() -> None:
    reg = FactorRegistry([_factor(multiplier=2.0, applied_by="user")])
    basis, multiplier, confidence = reg.basis_for("ct_a", _t(0))
    assert basis is MeasurementBasis.FACTOR_ADJUSTED
    assert multiplier == 2.0
    assert confidence == 1.0


def test_describe_is_a_complete_explainable_record() -> None:
    reg = FactorRegistry(
        [_factor(multiplier=2.0, applied_by="monitor", declared_by="import", note="Refoss")]
    )
    record = reg.describe("ct_a", _t(600))
    assert record["ct_id"] == "ct_a"
    assert record["basis"] == MeasurementBasis.MONITOR_REPORTED.value
    assert record["multiplier"] == 2.0
    assert record["applied_multiplier"] == 1.0
    assert record["reported_multiplier"] == 2.0
    assert record["applied_by"] == "monitor"
    assert record["declared_by"] == "import"
    assert record["note"] == "Refoss"
    assert record["interval"]["from"] == T0.isoformat()
    assert record["interval"]["to"] is None
    assert record["confidence"] == 1.0
    assert record["why"]


def test_registry_accepts_rows_from_the_database_helper(tmp_path) -> None:
    db = Database(tmp_path / "factors.db")
    try:
        db.insert_measurement_factor(_factor(ct_id="ct_a", multiplier=2.0, applied_by="user"))
        db.insert_measurement_factor(
            _factor(
                ct_id="ct_b",
                multiplier=2.0,
                applied_by="monitor",
                effective_from=_t(0),
            )
        )
        from_rows = FactorRegistry(db.factors_for_ct("ct_a"))
        assert from_rows.applied_multiplier_at("ct_a", _t(0)) == 2.0

        from_db = FactorRegistry.from_database(db, ["ct_a", "ct_b"])
        assert from_db.basis_at("ct_a", _t(0)) is MeasurementBasis.FACTOR_ADJUSTED
        assert from_db.basis_at("ct_b", _t(0)) is MeasurementBasis.MONITOR_REPORTED
        assert from_db.applied_multiplier_at("ct_b", _t(0)) == 1.0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# scale_reading: the single place scaling happens
# --------------------------------------------------------------------------- #


def test_scale_reading_applies_a_user_factor_exactly_once() -> None:
    reg = FactorRegistry([_factor(multiplier=2.0, applied_by="user")])
    scaled, applied, basis = scale_reading(1100.0, "ct_a", _t(0), reg)
    assert scaled == pytest.approx(2200.0)
    assert applied == 2.0
    assert basis is MeasurementBasis.FACTOR_ADJUSTED
    # Pure function of the *raw* input: calling it again with the same raw
    # reading cannot accumulate, because the caller never feeds the result back.
    again, _, _ = scale_reading(1100.0, "ct_a", _t(0), reg)
    assert again == pytest.approx(2200.0)


def test_scale_reading_never_reapplies_a_monitor_factor() -> None:
    """The double-application regression: a monitor-published total is final."""
    reg = FactorRegistry([_factor(multiplier=2.0, applied_by="monitor")])
    scaled, applied, basis = scale_reading(1250.0, "ct_a", _t(0), reg)
    assert scaled == 1250.0
    assert applied == 1.0
    assert basis is MeasurementBasis.MONITOR_REPORTED
    # Even scaling the already-published value again changes nothing.
    twice, _, _ = scale_reading(scaled, "ct_a", _t(0), reg)
    assert twice == 1250.0


def test_scale_reading_does_not_multiply_when_semantics_are_unknown() -> None:
    reg = FactorRegistry([_factor(multiplier=2.0, applied_by="unknown")])
    scaled, applied, basis = scale_reading(1250.0, "ct_a", _t(0), reg)
    assert basis is MeasurementBasis.UNKNOWN
    assert applied == 1.0
    assert scaled == 1250.0
    assert reg.confidence_factor_at("ct_a", _t(0)) < 1.0


def test_scale_reading_passes_none_through() -> None:
    reg = FactorRegistry([_factor(multiplier=2.0, applied_by="user")])
    scaled, applied, _ = scale_reading(None, "ct_a", _t(0), reg)
    assert scaled is None
    assert applied == 1.0


# --------------------------------------------------------------------------- #
# Historical reconstruction
# --------------------------------------------------------------------------- #


def test_basis_history_reports_the_basis_in_force_per_sample() -> None:
    reg = FactorRegistry(
        [
            _factor(multiplier=1.0, applied_by="user", effective_from=T0, effective_to=_t(600)),
            _factor(multiplier=2.0, applied_by="user", effective_from=_t(600)),
        ]
    )
    timestamps = [_t(0), _t(300), _t(599), _t(600), _t(900)]
    history = reg.basis_history("ct_a", timestamps)
    assert [item.timestamp for item in history] == timestamps
    assert [item.basis for item in history] == [
        MeasurementBasis.CT_MEASURED,
        MeasurementBasis.CT_MEASURED,
        MeasurementBasis.CT_MEASURED,
        MeasurementBasis.FACTOR_ADJUSTED,
        MeasurementBasis.FACTOR_ADJUSTED,
    ]
    assert [item.applied_multiplier for item in history] == [1.0, 1.0, 1.0, 2.0, 2.0]


def test_a_later_factor_change_never_rewrites_an_earlier_sample() -> None:
    """Today's factor is not retroactive, even after a second change is recorded."""
    reg = FactorRegistry(
        [
            _factor(multiplier=1.0, applied_by="user", effective_from=T0, effective_to=_t(600)),
            _factor(multiplier=2.0, applied_by="user", effective_from=_t(600), effective_to=_t(1200)),
            _factor(multiplier=3.0, applied_by="user", effective_from=_t(1200)),
        ]
    )
    old_sample = _t(100)
    new_sample = _t(1300)
    assert reg.applied_multiplier_at("ct_a", old_sample) == 1.0
    assert reg.applied_multiplier_at("ct_a", new_sample) == 3.0
    # Reconstructing the old reading uses the interval that covered it.
    scaled, applied, basis = scale_reading(1000.0, "ct_a", old_sample, reg)
    assert (scaled, applied, basis) == (1000.0, 1.0, MeasurementBasis.CT_MEASURED)


# --------------------------------------------------------------------------- #
# Factor ownership: the double-application hazard
# --------------------------------------------------------------------------- #


def _pair_channels(scalar: float = 1.0) -> list[CTChannel]:
    return [
        CTChannel(
            ct_id="ct_l1",
            entity_id="sensor.l1",
            phase=Phase.L1,
            device_id="tub",
            paired_ct_id="ct_l2",
            measurement_multiplier=scalar,
        ),
        CTChannel(
            ct_id="ct_l2",
            entity_id="sensor.l2",
            phase=Phase.L2,
            device_id="tub",
            paired_ct_id="ct_l1",
        ),
    ]


def test_two_factor_adjusted_legs_are_never_summed_as_independent_loads() -> None:
    result = resolve_factor_ownership(
        "ct_l1",
        [_factor(ct_id="ct_l1", multiplier=2.0, applied_by="user")],
        _pair_channels(),
        [("ct_l1", "ct_l2")],
        timestamp=T0,
    )
    assert result.summing_allowed is False
    assert result.appliance_basis is MeasurementBasis.FACTOR_ADJUSTED
    assert result.relation is RelationKind.CT_MEASUREMENT_OVERLAP
    assert result.relation is not RelationKind.ELECTRICAL_PARENT_CHILD
    assert "factor" in result.reason_code


def test_two_plain_legs_may_be_summed() -> None:
    result = resolve_factor_ownership(
        "ct_l1",
        [
            _factor(ct_id="ct_l1", multiplier=1.0, applied_by="user"),
            _factor(ct_id="ct_l2", multiplier=1.0, applied_by="user"),
        ],
        _pair_channels(),
        [("ct_l1", "ct_l2")],
        timestamp=T0,
    )
    assert result.summing_allowed is True
    assert result.appliance_basis is MeasurementBasis.CT_MEASURED
    assert result.explainable is True


def test_a_monitor_factor_plus_the_legacy_scalar_is_a_double_application_conflict() -> None:
    """Production-representable version of "incorrect metadata".

    The factor row says the monitor already applied x2, while the channel's
    legacy ``measurement_multiplier`` would apply x2 again on the read path.
    Exactly one can be true, so the ownership is unverifiable.
    """
    result = resolve_factor_ownership(
        "ct_l1",
        [_factor(ct_id="ct_l1", multiplier=2.0, applied_by="monitor")],
        _pair_channels(scalar=2.0),
        [("ct_l1", "ct_l2")],
        timestamp=T0,
    )
    assert result.double_application_risk is True
    assert result.appliance_basis is MeasurementBasis.UNKNOWN
    assert result.summing_allowed is False
    assert result.explainable is False
    assert result.reason_code


def test_unknown_ownership_propagates_to_the_appliance_basis() -> None:
    result = resolve_factor_ownership(
        "ct_l1",
        [_factor(ct_id="ct_l1", multiplier=2.0, applied_by="unknown")],
        _pair_channels(),
        [("ct_l1", "ct_l2")],
        timestamp=T0,
    )
    assert result.basis is MeasurementBasis.UNKNOWN
    assert result.appliance_basis is MeasurementBasis.UNKNOWN
    assert result.summing_allowed is False
    assert result.confidence < 1.0


def test_ownership_exposes_the_peer_set_and_the_factor_owner() -> None:
    result = resolve_factor_ownership(
        "ct_l1",
        [_factor(ct_id="ct_l1", multiplier=2.0, applied_by="user", declared_by="import")],
        _pair_channels(),
        [("ct_l1", "ct_l2")],
        timestamp=T0,
    )
    assert result.ct_id == "ct_l1"
    assert result.device_id == "tub"
    # Peers are the *other* CTs of the same appliance, never the CT itself.
    assert set(result.peer_ct_ids) == {"ct_l2"}
    assert result.applied_by == "user"
    assert result.declared_by == "import"
    assert "ct_l2" in result.as_dict()["peer_ct_ids"]


# --------------------------------------------------------------------------- #
# The Hot Tub table (Group B, mandatory regression)
# --------------------------------------------------------------------------- #


def test_hot_tub_table_leg_deltas_and_the_x2_estimate() -> None:
    """HA 0->500 W, CT1 1000->1250 W, CT2 900->1150 W, x2."""
    fixture = hot_tub("factor_two")
    published = fixture.published_table()
    assert published[CT_L1]["delta_w"] == pytest.approx(250.0)
    assert published[CT_L2]["delta_w"] == pytest.approx(250.0)

    registry, _ = _ci_registry(fixture)
    after = _t(600 + 120, fixture.start_time)
    before = _t(300, fixture.start_time)

    # CT1's raw leg delta, scaled by the user factor, is the 500 W appliance step.
    scaled_before, _, basis = scale_reading(published[CT_L1]["before_w"], CT_L1, before, registry)
    scaled_after, _, _ = scale_reading(published[CT_L1]["after_w"], CT_L1, after, registry)
    assert basis is MeasurementBasis.FACTOR_ADJUSTED
    assert scaled_after - scaled_before == pytest.approx(500.0)
    assert scaled_after - scaled_before == pytest.approx(
        fixture.ha_after_w - fixture.ha_before_w
    )

    # The naive "add every CT's best total" is the documented 250 W double count.
    ct2_delta = published[CT_L2]["delta_w"]
    naive = (scaled_after - scaled_before) + ct2_delta
    assert naive == pytest.approx(750.0)
    assert naive != pytest.approx(fixture.ha_after_w - fixture.ha_before_w)


def test_hot_tub_factor_two_legs_are_not_summed_as_independent_consumption() -> None:
    fixture = hot_tub("factor_two")
    result = _ci_ownership(fixture, at_s=720)
    assert result.summing_allowed is False
    assert result.explainable is True
    assert result.appliance_basis is MeasurementBasis.FACTOR_ADJUSTED


def test_hot_tub_equal_deltas_do_not_claim_a_parent_child_relation() -> None:
    """250 W + 250 W does not make one CT the parent of the other."""
    fixture = hot_tub("factor_two")
    result = _ci_ownership(fixture, at_s=720)
    assert result.relation is RelationKind.CT_MEASUREMENT_OVERLAP
    assert result.relation is not RelationKind.ELECTRICAL_PARENT_CHILD
    assert result.as_dict()["relation"] == RelationKind.CT_MEASUREMENT_OVERLAP.value


def test_hot_tub_factor_one_allows_summing_and_invents_no_factor() -> None:
    fixture = hot_tub("factor_one")
    registry, _ = _ci_registry(fixture)
    assert registry.basis_at(CT_L1, _t(720, fixture.start_time)) is MeasurementBasis.CT_MEASURED
    assert registry.applied_multiplier_at(CT_L1, _t(720, fixture.start_time)) == 1.0
    result = _ci_ownership(fixture, at_s=720)
    assert result.summing_allowed is True
    assert result.appliance_basis is MeasurementBasis.CT_MEASURED


def test_hot_tub_monitor_published_is_not_multiplied_a_second_time() -> None:
    """The fixture publishes 2000->2500 W; multiplying again would report 1000 W."""
    fixture = hot_tub("monitor_published")
    registry, _ = _ci_registry(fixture)
    at = _t(720, fixture.start_time)
    scaled, applied, basis = scale_reading(2500.0, CT_L1, at, registry)
    assert (scaled, applied) == (2500.0, 1.0)
    assert basis is MeasurementBasis.MONITOR_REPORTED
    # And the published delta already equals the 500 W appliance step.
    assert fixture.published_table()[CT_L1]["delta_w"] == pytest.approx(500.0)
    result = _ci_ownership(fixture, at_s=720)
    assert result.summing_allowed is False
    assert result.appliance_basis is MeasurementBasis.MONITOR_REPORTED


def test_hot_tub_factor_missing_is_unknown_with_lower_confidence() -> None:
    fixture = hot_tub("factor_missing")
    registry, _ = _ci_registry(fixture)
    at = _t(720, fixture.start_time)
    assert registry.factor_at(CT_L1, at) is None
    assert registry.basis_at(CT_L1, at) is MeasurementBasis.UNKNOWN
    assert registry.confidence_factor_at(CT_L1, at) < 1.0
    # No multiplication and no invented total: the arithmetic coincidence of
    # 250 + 250 = 500 does not establish what either CT is clamped on.
    scaled, applied, _ = scale_reading(1250.0, CT_L1, at, registry)
    assert (scaled, applied) == (1250.0, 1.0)
    result = _ci_ownership(fixture, at_s=720)
    assert result.summing_allowed is False
    assert result.explainable is False


def test_hot_tub_incorrect_metadata_is_unknown_and_never_a_wrong_number() -> None:
    fixture = hot_tub("factor_incorrect_metadata")
    registry, conflicts = _ci_registry(fixture)
    at = _t(720, fixture.start_time)
    assert conflicts
    res = registry.resolve(CT_L1, at)
    assert res.basis is MeasurementBasis.UNKNOWN
    assert res.conflict
    assert res.applied_multiplier == 1.0
    assert res.reported_multiplier == 1.0
    scaled, applied, _ = scale_reading(2500.0, CT_L1, at, registry)
    # Neither 1000 W (applied) nor 250 W (skipped) is published as a total.
    assert (scaled, applied) == (2500.0, 1.0)
    result = _ci_ownership(fixture, at_s=720)
    assert result.explainable is False
    assert result.summing_allowed is False


def test_hot_tub_multiple_factor_cts_are_one_appliance_not_two_loads() -> None:
    fixture = hot_tub("multiple_factor_cts")
    registry, _ = _ci_registry(fixture)
    at = _t(720, fixture.start_time)
    # Each CT independently estimates the same 500 W appliance.
    for ct_id in (CT_L1, CT_L2):
        assert registry.basis_at(ct_id, at) is MeasurementBasis.FACTOR_ADJUSTED
        scaled, _, _ = scale_reading(250.0, ct_id, at, registry)
        assert scaled == pytest.approx(500.0)
    result = _ci_ownership(fixture, at_s=720)
    assert result.summing_allowed is False
    assert result.appliance_basis is MeasurementBasis.FACTOR_ADJUSTED


def test_hot_tub_independent_physical_cts_may_be_summed() -> None:
    fixture = hot_tub("independent_physical_cts")
    result = _ci_ownership(fixture, at_s=720)
    assert result.summing_allowed is True
    assert result.appliance_basis is MeasurementBasis.CT_MEASURED
    # Exactly the other leg: noise never fabricates a third member.
    assert result.peer_ct_ids == (CT_L2,)


def test_hot_tub_noisy_unbalanced_legs_sum_without_inventing_a_leg() -> None:
    fixture = hot_tub("noisy_unbalanced_legs")
    result = _ci_ownership(fixture, at_s=720)
    assert result.summing_allowed is True
    assert result.appliance_basis is MeasurementBasis.CT_MEASURED

    registry, _ = _ci_registry(fixture)
    start = fixture.start_time
    samples = fixture.samples()
    by_ct: dict[str, dict[float, float]] = {}
    for sample in samples:
        by_ct.setdefault(sample["ct_id"], {})[float(sample["sample_time_s"])] = float(
            sample["value_w"]
        )
    for ct_id in (CT_L1, CT_L2):
        observed = by_ct[ct_id]
        delta = observed[720.0] - observed[300.0]
        # Noise moves the observed delta, but the basis stays measured and the
        # engine never "corrects" the split or fabricates a missing leg.
        assert delta == pytest.approx(250.0, abs=150.0)
        assert registry.basis_at(ct_id, _t(720, start)) is MeasurementBasis.CT_MEASURED
    # The pair has exactly one peer each way: no leg is invented from noise.
    assert result.peer_ct_ids == (CT_L2,)
    reverse = _ci_ownership(fixture, ct_id=CT_L2, at_s=720)
    assert reverse.peer_ct_ids == (CT_L1,)
    assert reverse.summing_allowed is True


def test_hot_tub_inconsistent_ha_vs_ct_refuses_a_total() -> None:
    fixture = hot_tub("inconsistent_ha_vs_ct")
    result = _ci_ownership(
        fixture,
        at_s=720,
        evidence_conflict="ha_state_off_while_power_rises",
    )
    # The factor metadata alone would support x2; the conflicting state evidence
    # forbids publishing a total on top of it.
    assert result.appliance_basis is MeasurementBasis.UNKNOWN
    assert result.summing_allowed is False
    assert result.explainable is False
    assert result.conflict


def test_hot_tub_factor_change_midhistory_resolves_each_sample() -> None:
    fixture = hot_tub("factor_changes_midhistory")
    registry, _ = _ci_registry(fixture)
    start = fixture.start_time
    history = registry.basis_history(CT_L1, [_t(300, start), _t(599, start), _t(600, start), _t(900, start)])
    assert [item.basis for item in history] == [
        MeasurementBasis.CT_MEASURED,
        MeasurementBasis.CT_MEASURED,
        MeasurementBasis.FACTOR_ADJUSTED,
        MeasurementBasis.FACTOR_ADJUSTED,
    ]
    assert [item.applied_multiplier for item in history] == [1.0, 1.0, 2.0, 2.0]
    # The pre-change sample keeps the factor that was in force when it was taken.
    assert registry.applied_multiplier_at(CT_L1, _t(300, start)) == 1.0
    assert registry.applied_multiplier_at(CT_L1, _t(900, start)) == 2.0
    # CT2 carries an unconditional x1 and stays measured throughout.
    assert registry.basis_at(CT_L2, _t(900, start)) is MeasurementBasis.CT_MEASURED


@pytest.mark.parametrize("variant", sorted(HOT_TUB_VARIANTS))
def test_every_hot_tub_variant_matches_its_expected_interpretation(variant: str) -> None:
    """The whole Group B matrix, against the fixture's own stated expectation."""
    fixture = hot_tub(variant)
    conflict = None
    if fixture.state_after == "off" and fixture.ha_after_w > 0.0:
        conflict = "ha_state_off_while_power_rises"
    result = _ci_ownership(fixture, at_s=720, evidence_conflict=conflict)
    assert result.appliance_basis.value == fixture.expected.basis, variant
    assert result.summing_allowed is fixture.expected.summing_allowed, variant
    assert result.explainable is fixture.expected.explainable, variant


def test_every_hot_tub_sample_resolves_without_raising() -> None:
    for variant in sorted(HOT_TUB_VARIANTS):
        fixture = hot_tub(variant)
        registry, _ = _ci_registry(fixture)
        for sample in fixture.samples():
            ct_id = sample["ct_id"]
            timestamp = datetime.fromisoformat(sample["state_timestamp"])
            resolution = registry.resolve(ct_id, timestamp)
            assert resolution.basis in set(MeasurementBasis)


# --------------------------------------------------------------------------- #
# Topology delegation (minimal wiring)
# --------------------------------------------------------------------------- #


def _delegating_topology() -> Topology:
    return Topology(
        channels=[
            CTChannel(
                ct_id="ct_tub",
                entity_id="sensor.tub",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                device_id="tub",
                measurement_multiplier=2.0,
            )
        ],
        devices=[
            DeviceTopology(
                device_id="tub",
                dedicated_ct_ids=["ct_tub"],
                phase=Phase.L1,
                double_pole=True,
            )
        ],
    )


def test_topology_without_a_registry_keeps_the_legacy_scalar() -> None:
    graph = build_graph(_delegating_topology())
    assert graph.multiplier_for("ct_tub") == 2.0
    assert graph.scale_w("ct_tub", 1100.0) == pytest.approx(2200.0)


def test_topology_delegates_to_a_user_factor_registry() -> None:
    registry = FactorRegistry([_factor(ct_id="ct_tub", multiplier=2.0, applied_by="user")])
    graph = build_graph(_delegating_topology(), factors=registry)
    assert graph.multiplier_for("ct_tub", _t(0)) == 2.0
    assert graph.scale_w("ct_tub", 1100.0, _t(0)) == pytest.approx(2200.0)
    # Scaling is applied on the way out only: the raw value is unchanged.
    assert graph.scale_w("ct_tub", 1100.0, _t(0)) == pytest.approx(2200.0)


def test_topology_does_not_scale_a_monitor_reported_value_again() -> None:
    registry = FactorRegistry([_factor(ct_id="ct_tub", multiplier=2.0, applied_by="monitor")])
    graph = build_graph(_delegating_topology(), factors=registry)
    assert graph.multiplier_for("ct_tub", _t(0)) == 1.0
    assert graph.scale_w("ct_tub", 1250.0, _t(0)) == 1250.0
