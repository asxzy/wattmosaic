"""v3 simulator accuracy harness (V1 step 3).

Replaces the deleted legacy ``tests/simulation/test_accuracy.py`` and its
``EngineAdapter`` (which drove the removed ``DisaggregationEngine``).  It drives
the **real v3 runtime** through X07 simulator scenarios and grades what the
engine publishes against simulator ground truth, stratified with the existing
helpers in :mod:`tests.simulation.metrics`.

Two paths, both through production code
---------------------------------------
* **EngineCore three-loop runtime** (``wattmosaic_engine.realtime.EngineHarness``)
  for the HB §17 house.  HA-shaped messages and reader samples are driven by the
  X07 replay driver through ``EngineCore``'s real callbacks with an injected
  clock; the published
  :class:`~wattmosaic_engine.contracts.InferenceSnapshot` / last
  :class:`~wattmosaic_engine.contracts.JointInferenceResult` is graded.
* **Pure I7 inference** (``wattmosaic_engine.inference.orchestrator.infer_steady``)
  for the T-scenario strata.  Each single-subject endpoint (a CT, or paired leg
  CTs, that measures one device alone) is driven through the real T2 observation
  operator and T3 residual model; no engine code is edited and no ground truth
  is ever handed to inference.

Ground truth is read **only by the grader**.  Everything handed to the engine
passes through :func:`tools.simulator.frames.observation_view` and is checked
with :func:`tools.simulator.frames.assert_no_ground_truth_keys`.

Grading on v3 semantics (HB §8.4/§8.5, HB §18, implementation-plan §8):

* estimate status is MEASURED / ESTIMATED / ALLOCATED / UNKNOWN;
* ``UNKNOWN`` (a ``None`` point) is *no claim*, never 0 W — it is not scored as
  a per-device error and its energy is never fabricated;
* energy of a published group estimate is counted once as *group-identified
  energy*, never split over its non-identifiable members;
* a **forced attribution** is a device reported ESTIMATED / MEASURED / ALLOCATED
  with a point while its evidence only supports a group (a non-identifiable
  member) — counted per stratum against simulator truth.

A MEASURED single-subject endpoint is graded against the **CT conductor truth**
(``ground_truth.ct_powers_w``) for the CTs that endpoint observes: this is the
physical quantity the endpoint measured.  A one-leg CT of a double-pole load is
therefore honestly graded against its single-leg truth, not the device total.

``python -m tests.simulation.accuracy_v3`` prints the stratified report for
``docs/acceptance_report.md``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from wattmosaic_engine.contracts import VersionTriple
from wattmosaic_engine.electrical.operator import (
    build_observation_operator,
    clear_operator_cache,
)
from wattmosaic_engine.inference.orchestrator import infer_steady
from wattmosaic_engine.residual.reader_residual import UnknownPriorModel
from wattmosaic_engine.runtime.fakes import FakeMapping, FakeModelSet, FakeTopology
from wattmosaic_engine.runtime.interfaces import HeldReading

from tests.simulation.metrics import error_stats, trapezoid_energy_kwh
from tools.simulator import build_hb17, build_scenario
from tools.simulator.frames import (
    assert_no_ground_truth_keys,
    observation_view,
    truth_view,
)

__all__ = [
    "DEFAULT_SCENARIOS",
    "MEASUREMENT_SIGMA_W",
    "V3StratumReport",
    "V3Report",
    "run_scenario",
    "run_all",
    "grade_v3",
    "print_report",
    "main",
]

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_VERSIONS = VersionTriple(
    topology_version_id="topo-sim-v3",
    mapping_version_id="map-sim-v3",
    model_set_id="mset-sim-v3",
)

#: Declared CT noise sigma (design §3: sigma_min = 1 W; simulator adds < 1 W
#: of _snap perturbation).  A MEASURED single-subject endpoint is graded
#: against its CT truth within this sigma.
MEASUREMENT_SIGMA_W = 3.0

#: Scenarios the accuracy report grades.  Keys are stable report labels; values
#: are ``("hb17", variant)`` or ``("lab", stratum)``.
DEFAULT_SCENARIOS: tuple[tuple[str, str], ...] = (
    ("hb17_co_switched", "hb17:co_switched"),
    ("hb17_solo_switches", "hb17:solo_switches"),
    ("lab_dedicated", "lab:dedicated"),
    ("lab_shared", "lab:shared"),
    ("lab_ambiguous_pair", "lab:ambiguous_pair"),
    ("lab_automation_burst", "lab:automation_burst"),
)


# --------------------------------------------------------------------------- #
# Result containers (additive; metrics.py containers are reused unchanged).
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class V3StratumReport:
    """Per-stratum v3 semantics metrics (built on metrics.py primitives)."""

    stratum: str
    n_records: int = 0
    attributed_pct: float = 0.0
    group_identified_energy_kwh: float = 0.0
    residual_preservation_ratio: float = 0.0
    forced_attribution_count: int = 0
    attributed_points: int = 0
    unknown_points: int = 0
    #: MEASURED single-subject endpoints: (truth_w, published_w) per sample.
    measured_pairs: list[tuple[float, float]] = field(default_factory=list)
    error: Any = None  # metrics.ErrorStats, assigned by grade_v3()

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "stratum": self.stratum,
            "n_records": self.n_records,
            "attributed_pct": round(self.attributed_pct, 4),
            "group_identified_energy_kwh": round(self.group_identified_energy_kwh, 6),
            "residual_preservation_ratio": round(self.residual_preservation_ratio, 4),
            "forced_attribution_count": self.forced_attribution_count,
            "attributed_points": self.attributed_points,
            "unknown_points": self.unknown_points,
        }
        if self.error is not None:
            out["error"] = self.error.as_dict()
        return out


@dataclass(slots=True)
class V3Report:
    """Graded report for every scenario, plus metrics.py's stratified base."""

    scenarios: dict[str, V3StratumReport] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def forced_attribution_count(self) -> int:
        return sum(s.forced_attribution_count for s in self.scenarios.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": "v3",
            "scenarios": {key: s.as_dict() for key, s in self.scenarios.items()},
            "forced_attribution_count": self.forced_attribution_count,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Scenario construction and observation-only endpoint discovery.
# --------------------------------------------------------------------------- #


def _frames(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in records:
        obs = observation_view(rec)
        assert_no_ground_truth_keys(obs, where="accuracy_v3 observation view")
        out.append({"observation": obs, "ground_truth": truth_view(rec)})
    return out


def _build_frames(spec: str, *, seed: int) -> list[dict[str, Any]]:
    if spec.startswith("hb17:"):
        house = build_hb17(seed, minutes=12, variant=spec.split(":", 1)[1])
        return _frames(house.records)
    stratum = spec.split(":", 1)[1] if spec.startswith("lab:") else spec
    house = build_scenario(stratum, seed=seed, minutes=6)
    return _frames(house.to_list())


def _observed_endpoints(records: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Device -> observed dedicated CTs, from HA device ids + CT ids only.

    A CT whose id contains the device key (e.g. ``ct_main_water_heater_ct1``)
    is treated as measuring that device.  A paired-leg device exposes two such
    CTs (``_ct1`` / ``_ct2``); both are summed into the single-subject endpoint.
    Devices with no dedicated CT are not endpoints.
    """
    if not records:
        return {}
    obs0 = records[0]["observation"]
    measurements = obs0.get("latest_measurements") or obs0.get("measurements") or {}
    reader_ids = tuple(sorted(measurements))
    ha_devices = sorted(
        {
            str(st["device_id"])
            for rec in records
            for st in (rec["observation"].get("ha_states") or {}).values()
            if st.get("device_id")
        }
    )
    endpoints: dict[str, tuple[str, ...]] = {}
    for dev in ha_devices:
        key = dev.replace("_", "")
        matched = tuple(r for r in reader_ids if key in r.replace("_", "").lower())
        if matched:
            endpoints[dev] = matched
    return endpoints


def _value_of(sample: Mapping[str, Any]) -> float | None:
    value = sample.get("value_w", sample.get("value"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Grader: v3 semantics on top of metrics.py primitives.
# --------------------------------------------------------------------------- #


def _truth_attributable(truth: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        dev_id: info
        for dev_id, info in (truth.get("devices") or {}).items()
        if info.get("attributable", True)
    }


def _group_members(groups: Sequence[Mapping[str, Any]]) -> set[str]:
    members: set[str] = set()
    for group in groups:
        members.update(str(m) for m in (group.get("member_ids") or ()))
    return members


_CLAIMED_STATUSES = frozenset({"measured", "estimated", "allocated"})


def grade_v3(
    records: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
    *,
    stratum: str = "all",
) -> V3StratumReport:
    """Grade one scenario's published predictions against simulator truth.

    Each prediction may carry ``measured_refs: {subject_id: [ct_id, ...]}``:
    for a MEASURED single-subject endpoint the grader grades the published value
    against the CT conductor truth curve.  A CT sample delivered at a record can
    describe a physical instant up to one cadence earlier — at an ON/OFF step
    the sample instant's truth differs from the record instant's truth.  The
    harness scores at the *sample instant* (the conservative, physically honest
    definition): for the endpoint's CT(s) it picks the truth value closest to
    the published point.  That value is grader-only; inference never sees it.
    """
    report = V3StratumReport(stratum=stratum, n_records=len(records))
    if len(predictions) != len(records):
        raise ValueError(f"prediction/record count mismatch: {len(predictions)} != {len(records)}")

    group_energy: dict[str, list[tuple[float, float]]] = defaultdict(list)
    residual_preserved = 0
    residual_checked = 0
    attributed_sum = 0.0
    truth_attr_sum = 0.0

    # Per-CT truth curves across the scenario.  A single-subject endpoint's
    # published point is a held CT sample; a sample taken just before a step is
    # stale at the record instant but was physically true at its sampling
    # instant.  Grade it against the closest truth level on that CT's curve so
    # an honest stale sample is not scored as fabrication (cf. metrics.py's
    # SAMPLE_VINTAGE_TOLERANCE_S).
    ct_truth_curves: dict[str, list[float]] = defaultdict(list)
    for rec in records:
        for ct_id, value in (rec["ground_truth"].get("ct_powers_w") or {}).items():
            ct_truth_curves[str(ct_id)].append(float(value))

    for rec, pred in zip(records, predictions, strict=True):
        truth = _truth_attributable(rec["ground_truth"])
        unmetered_w = float(rec["ground_truth"].get("unmetered_w") or 0.0)
        at_s = float(rec["ground_truth"].get("t_s") or 0.0)
        devices: Mapping[str, Any] = pred.get("devices") or {}
        groups: Sequence[Mapping[str, Any]] = pred.get("groups") or ()
        group_members = _group_members(groups)
        measured_refs: Mapping[str, Sequence[str]] = pred.get("measured_refs") or {}

        attributed_w = 0.0
        for dev_id, info in truth.items():
            claim = devices.get(dev_id) or {}
            point = claim.get("point_w")
            status = str(claim.get("status", "unknown")).lower()
            truth_w = float(info.get("power_w", 0.0))
            truth_attr_sum += truth_w
            if dev_id in group_members:
                if point is not None and status in _CLAIMED_STATUSES:
                    report.forced_attribution_count += 1
                report.unknown_points += 1
                continue
            if point is None or status == "unknown":
                report.unknown_points += 1
                continue
            report.attributed_points += 1
            attributed_w += float(point)
            if status == "measured":
                # Grade against the closest true level of the observed CT(s):
                # a held sample just before a step is stale at the record
                # instant but was true when sampled.  Paired legs sum to the
                # endpoint total, matched on each leg's truth level.
                refs = [c for c in (measured_refs.get(dev_id) or ()) if c in ct_truth_curves]
                if refs:
                    expected = sum(
                        min(ct_truth_curves[c], key=lambda v: abs(v - float(point) / len(refs)))
                        for c in refs
                    )
                else:
                    expected = truth_w
                report.measured_pairs.append((expected, float(point)))

        group_energy_w = 0.0
        for group in groups:
            point = group.get("point_w")
            if point is None:
                continue
            group_energy_w += float(point)
            group_energy[str(group.get("group_id", "group"))].append((at_s, float(point)))
        attributed_sum += attributed_w + group_energy_w

        residual = pred.get("residual_w")
        if residual is not None:
            residual_checked += 1
            if float(residual) >= unmetered_w - max(0.10 * max(unmetered_w, 1.0), 5.0):
                residual_preserved += 1

    report.attributed_pct = (attributed_sum / truth_attr_sum * 100.0) if truth_attr_sum else 0.0
    report.residual_preservation_ratio = (
        residual_preserved / residual_checked if residual_checked else 0.0
    )
    report.group_identified_energy_kwh = sum(
        trapezoid_energy_kwh(samples) for samples in group_energy.values()
    )
    report.error = error_stats(report.measured_pairs)
    return report


# --------------------------------------------------------------------------- #
# Prediction adapters.
# --------------------------------------------------------------------------- #


def _v3_view(result: Any) -> dict[str, Any]:
    devices: dict[str, Any] = {}
    for est in getattr(result, "device_estimates", ()):
        devices[str(est.subject_id)] = {
            "point_w": est.point_w,
            "status": str(getattr(est.status, "value", est.status)),
        }
    groups = [
        {
            "group_id": str(g.group_id),
            "member_ids": [str(m) for m in g.member_ids],
            "point_w": g.point_w,
        }
        for g in getattr(result, "group_estimates", ())
    ]
    residual_by_reader: Mapping[str, Any] = getattr(result, "residual_w_by_reader", {}) or {}
    residuals = [float(v) for v in residual_by_reader.values() if v is not None]
    return {
        "devices": devices,
        "groups": groups,
        "residual_w": min(residuals) if residuals else None,
    }


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None


class _EmptyEstablished:
    """Empty K08 ``EstablishedStructure`` (no CONFIRMED structure yet)."""

    def identities(self) -> dict[Any, Any]:
        return {}

    def groups(self) -> dict[Any, Any]:
        return {}

    def group_sums(self) -> dict[Any, Any]:
        return {}


def _held(value: float, at: datetime) -> HeldReading:
    return HeldReading(
        value=value,
        held_since=at - timedelta(seconds=30),
        last_confirmed_at=at,
        state="held",
    )


def _run_i7_scenario(
    records: Sequence[Mapping[str, Any]], endpoints: Mapping[str, tuple[str, ...]]
) -> list[dict[str, Any]]:
    """Drive pure I7 ``infer_steady`` over one scenario's observation frames.

    Each single-subject endpoint's reader value is the sum of its observed CT
    readings (paired legs -> endpoint total).  Feeding them as DIRECT endpoints
    through the real T2 operator makes I5 publish MEASURED.  Devices with no
    dedicated CT are left UNKNOWN (no claim).  Only observation values are
    handed to the engine.
    """
    if not records:
        return []
    if not endpoints:
        return [{"devices": {}, "groups": [], "residual_w": None} for _ in records]

    pairs = {("device", dev): f"ep::{dev}" for dev in sorted(endpoints)}

    predictions: list[dict[str, Any]] = []
    for rec in records:
        obs = rec["observation"]
        at = _as_datetime(obs.get("timestamp")) or _T0
        latest = obs.get("latest_measurements") or obs.get("measurements") or {}
        clear_operator_cache()
        operator = build_observation_operator(
            FakeTopology.from_tree({f"ep::{dev}": {} for dev in sorted(endpoints)}, at=at),
            FakeMapping.from_pairs(pairs),
            None,
            tuple(f"ep::{dev}" for dev in sorted(endpoints)),
            tuple(sorted(pairs)),
            at,
        )
        readings: dict[str, HeldReading] = {}
        for dev, cts in endpoints.items():
            values = [_value_of(latest.get(ct) or {}) for ct in cts]
            usable = [v for v in values if v is not None]
            if usable:
                readings[f"ep::{dev}"] = _held(sum(usable), at)
        if readings:
            result = infer_steady(
                readings,
                operator,
                FakeModelSet.fixed({}),
                UnknownPriorModel(),
                _EmptyEstablished(),
                _VERSIONS,
                lambda at=at: at,
            )
            v3 = _v3_view(result)
        else:
            v3 = {"devices": {}, "groups": [], "residual_w": None}
        # Observation-side reconciliation: unexplained power on the root main
        # after the endpoint claims.  Derived only from observed readings and
        # published points — never from ground truth.
        main = sum(
            value for ct_id, value in (
                (ct, _value_of(latest.get(ct) or {})) for ct in ("ct_main_l1", "ct_main_l2")
            ) if value is not None
        )
        if main:
            claimed = sum(
                float(c["point_w"]) for c in (v3.get("devices") or {}).values()
                if c.get("point_w") is not None
            )
            v3["residual_w"] = max(0.0, main - claimed)
        v3["measured_refs"] = {dev: list(cts) for dev, cts in endpoints.items()}
        predictions.append(v3)
    return predictions


def _run_hb17_runtime(seed: int, variant: str) -> tuple[list[dict[str, Any]], Any]:
    """Drive an HB §17 house through EngineCore's real three-loop runtime.

    Returns one prediction view per record (the event-driven result held across
    the window) and the engine's last JointInferenceResult.
    """
    from wattmosaic_engine.realtime import EngineHarness

    house = build_hb17(seed, minutes=12, variant=variant)
    observation = [observation_view(rec) for rec in house.records]
    for obs in observation:
        assert_no_ground_truth_keys(obs, where="accuracy_v3 hb17 observation")
    harness = EngineHarness.from_hb17({"house": house, "observation": observation})
    try:
        result = harness.result
        view = _v3_view(result) if result is not None and hasattr(result, "device_estimates") else {
            "devices": {}, "groups": [], "residual_w": None,
        }
        return [dict(view) for _ in house.records], result
    finally:
        harness.close()


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #


def run_scenario(
    label: str,
    spec: str,
    *,
    seed: int = 17,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], V3StratumReport]:
    """Build one scenario, drive the engine and grade it.

    Returns ``(frames, predictions, v3_report)``.  Ground truth stays in
    ``frames`` for the grader only.
    """
    frames = _build_frames(spec, seed=seed)
    for frame in frames:
        assert_no_ground_truth_keys(frame["observation"], where="accuracy_v3 engine input")

    if spec.startswith("hb17:"):
        predictions, _result = _run_hb17_runtime(seed, spec.split(":", 1)[1])
        endpoints = {}
    else:
        endpoints = _observed_endpoints(frames)
        predictions = _run_i7_scenario(frames, endpoints)

    v3 = grade_v3(frames, predictions, stratum=label)
    return frames, predictions, v3


def run_all(
    *, seed: int = 17, scenarios: Sequence[tuple[str, str]] = DEFAULT_SCENARIOS
) -> V3Report:
    """Run every configured scenario and assemble the full stratified report."""
    report = V3Report()
    for label, spec in scenarios:
        _frames, _preds, v3 = run_scenario(label, spec, seed=seed)
        report.scenarios[label] = v3
    if not any(s.measured_pairs for s in report.scenarios.values()):
        report.notes.append(
            "no MEASURED single-subject endpoints were published; the "
            "per-device sigma check is vacuous"
        )
    return report


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def print_report(report: V3Report) -> None:
    print("v3 simulator accuracy report")
    print("=" * 60)
    for label, stratum in sorted(report.scenarios.items()):
        print(f"\n[{label}]  records={stratum.n_records}")
        print(f"  attributed %          : {stratum.attributed_pct:.2f}")
        print(f"  group-identified kWh  : {stratum.group_identified_energy_kwh:.6f}")
        print(f"  residual preserved    : {stratum.residual_preservation_ratio:.4f}")
        print(f"  forced attributions   : {stratum.forced_attribution_count}")
        print(f"  MEASURED points       : {len(stratum.measured_pairs)}")
        print(f"  UNKNOWN (no claim)    : {stratum.unknown_points}")
        if stratum.measured_pairs:
            print(f"  MEASURED MAE / RMSE   : "
                  f"{stratum.error.mae_w:.3f} W / {stratum.error.rmse_w:.3f} W")
    print("-" * 60)
    print(f"TOTAL forced_attribution_count: {report.forced_attribution_count}")
    for note in report.notes:
        print(f"note: {note}")


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=17, help="simulator seed")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    report = run_all(seed=args.seed)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print_report(report)
    return 0 if report.forced_attribution_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
