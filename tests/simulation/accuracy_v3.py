"""v3 simulator accuracy harness (V1 step 3).

Replaces the deleted legacy ``tests/simulation/test_accuracy.py`` and its
``EngineAdapter`` (which drove the removed ``DisaggregationEngine``).  It drives
production v3 code over X07 simulator scenarios and grades what the engine
*publishes* against simulator ground truth, per stratum.

Two paths, both through production code
---------------------------------------
* **EngineCore three-loop runtime** for the HB §17 house
  (``wattmosaic_engine.realtime.EngineHarness``).  HA-shaped messages and reader
  samples are replayed record by record through ``EngineCore``'s fast loop; the
  event and steady loops run after every record on an injected clock; the
  published :class:`~wattmosaic_engine.contracts.InferenceSnapshot` is graded
  *at every record*.  The house gets a 15-minute quiet prelude
  (``lead_in_minutes``) so the P3 detector's 64-sample noise warm-up has passed
  before the first scripted step (a cold start never detects a step at
  sample 30).
* **Pure I7 inference** (``infer_steady``) for the lab strata, over the real T2
  observation operator built from the *declared* circuit wiring.

Declared configuration (never truth): HB §17 — one subject per HA light on
Circuit A and the TV on its plug, plus the HA entity links; lab strata — each
HA-visible device's declared branch circuit(s) (``SimDevice.circuits`` on
branch CTs; devices declared only on a main leg are the unmetered loads and are
not declared).  A user supplies exactly this wiring when configuring circuits.
The simulator's per-device watts, conductor truth and unmetered power are read
**only by the grader**; everything handed to the engine passes through
:func:`tools.simulator.frames.observation_view` and the leak detector.

Grading on v3 semantics (HB §8.4/§8.5, HB §18, implementation-plan §8):

* ``UNKNOWN`` (a ``None`` point) is *no claim*, never 0 W;
* a **forced attribution** is a published point for a subject the simulator's
  own evidence cannot separate (a member of a truth non-identifiable group:
  the HB §17 co-switched lights; devices sharing exactly one lab branch CT), or
  a point for a member of a published group;
* a published group's sum is counted once as **group-identified energy**;
* a published unknown-load estimate is counted once as **unknown energy**;
* **residual preservation**: the observed main reading minus everything the
  engine claimed (device points + group sums) must not fall below the true
  unmetered load by more than ``max(10 %, 5 W)`` — claims never eat the residual;
* a MEASURED single-subject endpoint is graded against the CT conductor truth
  of the CTs it observes.

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
    "HB17_DEVICES",
    "HB17_LEAD_IN_MIN",
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

#: HB §17 declared configuration (see the module docstring).
HB17_LIGHTS = ("light.living_room_1", "light.living_room_2")
HB17_DEVICES: dict[str, str] = {HB17_LIGHTS[0]: "ct_a", HB17_LIGHTS[1]: "ct_a", "tv": "ct_plug"}
HB17_ENTITY_LINKS = (
    (HB17_LIGHTS[0], HB17_LIGHTS[0]),
    (HB17_LIGHTS[1], HB17_LIGHTS[1]),
    ("switch.tv_plug", "tv"),
)
HB17_LEAD_IN_MIN = 15.0
HB17_SCRIPT_MIN = 12.0

#: Scenarios the accuracy report grades.
DEFAULT_SCENARIOS: tuple[tuple[str, str], ...] = (
    ("hb17_co_switched", "hb17:co_switched"),
    ("hb17_solo_switches", "hb17:solo_switches"),
    ("lab_dedicated", "lab:dedicated"),
    ("lab_shared", "lab:shared"),
    ("lab_ambiguous_pair", "lab:ambiguous_pair"),
    ("lab_automation_burst", "lab:automation_burst"),
)

_CLAIMED_STATUSES = frozenset({"measured", "estimated", "allocated"})


# --------------------------------------------------------------------------- #
# Result containers.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class V3StratumReport:
    """Per-stratum v3 metrics (built on metrics.py primitives)."""

    stratum: str
    n_records: int = 0
    attributed_pct: float = 0.0
    group_identified_energy_kwh: float = 0.0
    unknown_energy_kwh: float = 0.0
    truth_energy_kwh: float = 0.0
    residual_preservation_ratio: float | None = None
    residual_checked: int = 0
    forced_attribution_count: int = 0
    attributed_points: int = 0
    published_points: int = 0
    group_points: int = 0
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
            "unknown_energy_kwh": round(self.unknown_energy_kwh, 6),
            "truth_energy_kwh": round(self.truth_energy_kwh, 6),
            "residual_preservation_ratio": (
                None
                if self.residual_preservation_ratio is None
                else round(self.residual_preservation_ratio, 4)
            ),
            "residual_checked": self.residual_checked,
            "forced_attribution_count": self.forced_attribution_count,
            "attributed_points": self.attributed_points,
            "published_points": self.published_points,
            "group_points": self.group_points,
            "unknown_points": self.unknown_points,
            "measured_points": len(self.measured_pairs),
        }
        if self.error is not None and self.measured_pairs:
            out["error"] = self.error.as_dict()
        return out


@dataclass(slots=True)
class V3Report:
    """Graded report for every scenario."""

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
# Frames: observation for the engine, truth for the grader.
# --------------------------------------------------------------------------- #


def _hb17_truth(record: Mapping[str, Any]) -> dict[str, Any]:
    truth = record.get("truth") or {}
    return {
        "t_s": float(record.get("t_s") or 0.0),
        "devices": {
            dev: {"power_w": float(watts), "attributable": True}
            for dev, watts in (truth.get("device_power_w") or {}).items()
        },
        "ct_powers_w": dict(truth.get("ct_powers_w") or {}),
        "unmetered_w": float(truth.get("unmetered_w") or 0.0),
        "non_identifiable_groups": [list(g) for g in truth.get("non_identifiable_groups") or ()],
    }


def _frames(observations: Sequence[Mapping[str, Any]], truths: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for obs, truth in zip(observations, truths, strict=True):
        assert_no_ground_truth_keys(obs, where="accuracy_v3 observation view")
        out.append({"observation": obs, "ground_truth": truth})
    return out


def _lab_declared(house: Any) -> dict[str, tuple[str, ...]]:
    """Declared branch wiring: ``device -> sorted branch CT ids`` (no main legs)."""
    main = {c.ct_id for c in house.topology.channels if str(getattr(c.ct_type, "value", c.ct_type)) == "main"}
    declared: dict[str, tuple[str, ...]] = {}
    for device in house.scenario.devices:
        cts = tuple(sorted(ct for ct in device.circuits if ct not in main))
        if cts:
            declared[str(device.device_id)] = cts
    return declared


def _lab_non_identifiable(declared: Mapping[str, tuple[str, ...]]) -> list[list[str]]:
    """Grader-only: devices whose declared CT set is shared exactly (inseparable)."""
    by_cts: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for device, cts in declared.items():
        by_cts[cts].append(device)
    return [sorted(devs) for devs in by_cts.values() if len(devs) >= 2]


def _build(spec: str, *, seed: int) -> tuple[list[dict[str, Any]], Any]:
    if spec.startswith("hb17:"):
        house = build_hb17(
            seed, minutes=HB17_SCRIPT_MIN, variant=spec.split(":", 1)[1],
            lead_in_minutes=HB17_LEAD_IN_MIN,
        )
        frames = _frames(house.observation_view(), [_hb17_truth(r) for r in house.records])
        return frames, house
    stratum = spec.split(":", 1)[1]
    house = build_scenario(stratum, seed=seed, minutes=6)
    records = house.to_list()
    groups = _lab_non_identifiable(_lab_declared(house))
    truths = []
    for record in records:
        truth = truth_view(record)
        truth["non_identifiable_groups"] = groups
        truths.append(truth)
    return _frames([observation_view(r) for r in records], truths), house


def _value_of(sample: Mapping[str, Any]) -> float | None:
    value = sample.get("value_w", sample.get("value"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Grader.
# --------------------------------------------------------------------------- #


def grade_v3(
    records: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
    *,
    stratum: str = "all",
) -> V3StratumReport:
    """Grade one scenario's per-record published view against simulator truth.

    Each prediction is ``{"devices": {sid: {"point_w", "status"}}, "groups":
    [{"group_id", "member_ids", "point_w"}], "unknown_w": float | None,
    "residual_w": float | None, "measured_refs": {sid: [ct, ...]}}``.
    """
    report = V3StratumReport(stratum=stratum, n_records=len(records))
    if len(predictions) != len(records):
        raise ValueError(f"prediction/record count mismatch: {len(predictions)} != {len(records)}")

    group_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    unknown_series: list[tuple[float, float]] = []
    claimed_series: list[tuple[float, float]] = []
    truth_series: list[tuple[float, float]] = []
    preserved = 0
    ct_truth_curves: dict[str, list[float]] = defaultdict(list)
    for rec in records:
        for ct_id, value in (rec["ground_truth"].get("ct_powers_w") or {}).items():
            ct_truth_curves[str(ct_id)].append(float(value))

    for rec, pred in zip(records, predictions, strict=True):
        truth = rec["ground_truth"]
        at_s = float(truth.get("t_s") or 0.0)
        devices_truth = {
            dev: info for dev, info in (truth.get("devices") or {}).items()
            if info.get("attributable", True)
        }
        inseparable = {m for group in truth.get("non_identifiable_groups") or () for m in group}
        devices: Mapping[str, Any] = pred.get("devices") or {}
        groups: Sequence[Mapping[str, Any]] = pred.get("groups") or ()
        group_members = {str(m) for g in groups for m in (g.get("member_ids") or ())}
        measured_refs: Mapping[str, Sequence[str]] = pred.get("measured_refs") or {}

        claimed_w = 0.0
        report.published_points += len(devices)
        for dev_id, claim in devices.items():
            point = claim.get("point_w")
            status = str(claim.get("status", "unknown")).lower()
            if point is None or status not in _CLAIMED_STATUSES:
                continue
            if dev_id in inseparable or dev_id in group_members:
                report.forced_attribution_count += 1
                continue
            report.attributed_points += 1
            claimed_w += float(point)
            if status == "measured":
                refs = [c for c in (measured_refs.get(dev_id) or ()) if c in ct_truth_curves]
                if refs:
                    # A held sample just before a step was true when sampled:
                    # grade against the closest true level of each observed CT.
                    expected = sum(
                        min(ct_truth_curves[c], key=lambda v: abs(v - float(point) / len(refs)))
                        for c in refs
                    )
                else:
                    expected = float((devices_truth.get(dev_id) or {}).get("power_w", 0.0))
                report.measured_pairs.append((expected, float(point)))
        report.unknown_points += sum(
            1 for dev in devices_truth
            if (devices.get(dev) or {}).get("point_w") is None
            or str((devices.get(dev) or {}).get("status", "unknown")).lower() not in _CLAIMED_STATUSES
        )
        for group in groups:
            point = group.get("point_w")
            if point is None:
                continue
            report.group_points += 1
            claimed_w += float(point)
            group_series[str(group.get("group_id", "group"))].append((at_s, float(point)))
        unknown = pred.get("unknown_w")
        if unknown is not None:
            unknown_series.append((at_s, float(unknown)))
        claimed_series.append((at_s, claimed_w))
        truth_series.append((at_s, sum(float(i.get("power_w", 0.0)) for i in devices_truth.values())))

        main = pred.get("main_w")
        if main is not None:
            report.residual_checked += 1
            residual = float(main) - claimed_w
            unmetered = float(truth.get("unmetered_w") or 0.0)
            if residual >= unmetered - max(0.10 * max(unmetered, 1.0), 5.0):
                preserved += 1

    truth_kwh = trapezoid_energy_kwh(truth_series)
    claimed_kwh = trapezoid_energy_kwh(claimed_series)
    report.truth_energy_kwh = truth_kwh
    report.attributed_pct = (claimed_kwh / truth_kwh * 100.0) if truth_kwh else 0.0
    report.group_identified_energy_kwh = sum(trapezoid_energy_kwh(s) for s in group_series.values())
    report.unknown_energy_kwh = trapezoid_energy_kwh(unknown_series)
    report.residual_preservation_ratio = (
        preserved / report.residual_checked if report.residual_checked else None
    )
    report.error = error_stats(report.measured_pairs)
    return report


# --------------------------------------------------------------------------- #
# Engine drivers.
# --------------------------------------------------------------------------- #


def _snapshot_view(snapshot: Any, main_w: float | None) -> dict[str, Any]:
    if snapshot is None:
        return {"devices": {}, "groups": [], "unknown_w": None, "main_w": main_w}
    return {
        "devices": {
            str(e.subject_id): {"point_w": e.point_w, "status": str(getattr(e.status, "value", e.status))}
            for e in snapshot.device_estimates
        },
        "groups": [
            {"group_id": str(g.group_id), "member_ids": [str(m) for m in g.member_ids], "point_w": g.point_w}
            for g in snapshot.group_estimates
        ],
        "unknown_w": snapshot.reconciliation.unknown_w,
        "main_w": main_w,
    }


def _run_hb17_runtime(house: Any) -> list[dict[str, Any]]:
    """Replay an HB §17 house through EngineCore; one published view per record."""
    from wattmosaic_engine.contracts.entities import PhysicalDevice
    from wattmosaic_engine.realtime import EngineHarness

    start = house.start_time
    observation = house.observation_view()

    def configure(core: Any) -> None:
        at = start - timedelta(days=1)
        for device_id in (*HB17_LIGHTS, "tv"):
            core.db.repos.entities.upsert_device(
                PhysicalDevice(device_id=device_id, name=device_id, origin="user", created_at=at)
            )
        for entity_id, target in HB17_ENTITY_LINKS:
            core.db.repos.entities.add_entity_link(
                {"entity_id": entity_id, "target_kind": "device", "target_id": target,
                 "role": "state", "valid_from": at, "declared_by": "user"}
            )

    harness = EngineHarness.start(observation, start_time=start, devices=HB17_DEVICES, configure=configure)
    views: list[dict[str, Any]] = []
    try:
        for record in observation:
            harness.feed(record)
            main = _value_of((record.get("reader_samples") or {}).get("ct_main") or {})
            view = _snapshot_view(harness.snapshot, main)
            view["measured_refs"] = {"tv": ["ct_plug"]}
            views.append(view)
    finally:
        harness.close()
    return views


class _EmptyEstablished:
    """Empty K08 ``EstablishedStructure`` (no CONFIRMED structure yet)."""

    def identities(self) -> dict[Any, Any]:
        return {}

    def groups(self) -> dict[Any, Any]:
        return {}

    def group_sums(self) -> dict[Any, Any]:
        return {}


def _held(value: float, at: datetime) -> HeldReading:
    return HeldReading(value=value, held_since=at - timedelta(seconds=30), last_confirmed_at=at, state="held")


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None


def _run_lab_steady(frames: Sequence[Mapping[str, Any]], house: Any) -> list[dict[str, Any]]:
    """Pure I7 ``infer_steady`` over the declared branch wiring, per record.

    One endpoint per distinct declared CT set: a single CT (shared by every
    device declared on it) or a double-pole pair whose leg readings are summed
    into the endpoint total.  Only observed CT values reach the engine.
    """
    declared = _lab_declared(house)
    endpoints: dict[str, tuple[str, ...]] = {}
    pairs: dict[tuple[str, str], str] = {}
    for device, cts in sorted(declared.items()):
        endpoint = "+".join(cts)
        endpoints[endpoint] = cts
        pairs[("device", device)] = endpoint
    measured_refs = {dev: list(cts) for dev, cts in declared.items()}
    readers = tuple(sorted(endpoints))
    subjects = tuple(sorted(pairs))
    views: list[dict[str, Any]] = []
    for frame in frames:
        obs = frame["observation"]
        at = _as_datetime(obs.get("timestamp")) or _T0
        latest = obs.get("latest_measurements") or {}
        main_values = [
            _value_of(latest.get(ct) or {}) for ct in ("ct_main_l1", "ct_main_l2")
        ]
        main = sum(v for v in main_values if v is not None) if any(v is not None for v in main_values) else None
        readings: dict[str, HeldReading] = {}
        for endpoint, cts in endpoints.items():
            values = [_value_of(latest.get(ct) or {}) for ct in cts]
            if values and all(v is not None for v in values):
                readings[endpoint] = _held(sum(v for v in values if v is not None), at)
        view: dict[str, Any] = {"devices": {}, "groups": [], "unknown_w": None, "main_w": main}
        if readings:
            clear_operator_cache()
            operator = build_observation_operator(
                FakeTopology.from_tree({r: {} for r in readers}, at=at),
                FakeMapping.from_pairs(pairs),
                None,
                readers,
                subjects,
                at,
            )
            result = infer_steady(
                readings, operator, FakeModelSet.fixed({}), UnknownPriorModel(),
                _EmptyEstablished(), _VERSIONS, lambda at=at: at,
            )
            view["devices"] = {
                str(e.subject_id): {"point_w": e.point_w, "status": str(getattr(e.status, "value", e.status))}
                for e in result.device_estimates
            }
            view["groups"] = [
                {"group_id": str(g.group_id), "member_ids": [str(m) for m in g.member_ids], "point_w": g.point_w}
                for g in result.group_estimates
            ]
        view["measured_refs"] = measured_refs
        views.append(view)
    return views


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #


def run_scenario(
    label: str,
    spec: str,
    *,
    seed: int = 17,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], V3StratumReport]:
    """Build one scenario, drive the engine and grade it."""
    frames, house = _build(spec, seed=seed)
    hb17 = spec.startswith("hb17:")
    predictions = _run_hb17_runtime(house) if hb17 else _run_lab_steady(frames, house)
    return frames, predictions, grade_v3(frames, predictions, stratum=label)


def run_all(
    *, seed: int = 17, scenarios: Sequence[tuple[str, str]] = DEFAULT_SCENARIOS
) -> V3Report:
    """Run every configured scenario and assemble the stratified report."""
    report = V3Report()
    for label, spec in scenarios:
        _frames, _preds, graded = run_scenario(label, spec, seed=seed)
        report.scenarios[label] = graded
    if not any(s.group_points for s in report.scenarios.values()):
        report.notes.append(
            "no group estimate was published in any stratum: I4 publishes a group only with "
            ">= 2 independent historical events (MIN_HISTORY_EVENTS) and the live runtime "
            "never persists subject-level history (strict xfail in test_accuracy_v3 / "
            "test_hb17_walkthrough)"
        )
    return report


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def _fmt(value: float | None, spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


def print_report(report: V3Report) -> None:
    print("v3 simulator accuracy report")
    print("=" * 60)
    for label, stratum in report.scenarios.items():
        print(f"\n[{label}]  records={stratum.n_records}")
        print(f"  attributed %          : {stratum.attributed_pct:.2f}")
        print(f"  group-identified kWh  : {stratum.group_identified_energy_kwh:.6f}")
        print(f"  unknown-load kWh      : {stratum.unknown_energy_kwh:.6f}")
        print(f"  truth device kWh      : {stratum.truth_energy_kwh:.6f}")
        print(f"  residual preserved    : {_fmt(stratum.residual_preservation_ratio, '.4f')}"
              f" ({stratum.residual_checked} checked)")
        print(f"  forced attributions   : {stratum.forced_attribution_count}")
        print(f"  published estimates   : {stratum.published_points}")
        print(f"  attributed points     : {stratum.attributed_points}")
        print(f"  MEASURED points       : {len(stratum.measured_pairs)}")
        print(f"  UNKNOWN (no claim)    : {stratum.unknown_points}")
        if stratum.measured_pairs:
            print(f"  MEASURED MAE / RMSE   : {stratum.error.mae_w:.3f} W / {stratum.error.rmse_w:.3f} W"
                  f" (max {stratum.error.max_abs_w:.3f} W)")
    print("-" * 60)
    print(f"TOTAL forced_attribution_count: {report.forced_attribution_count}")
    for note in report.notes:
        print(f"note: {note}")


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
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
