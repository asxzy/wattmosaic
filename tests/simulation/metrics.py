"""Accuracy evaluation harness for WattMosaic disaggregation.

Design constraints (from the master spec §19.4):

* **Never** a single aggregate score.  Every metric is reported per stratum:
  source tier (direct/inferred/model-estimated), device type, concurrency
  level, data-quality level, and per device.
* The harness takes a ``predictor`` callable, so any engine implementation
  (real, stub, or deliberately wrong) can be measured identically.
* No-fabrication invariants are checked independently of accuracy: residual
  preservation, no forced attribution, no nested-CT double counting, and no
  stale-sample learning.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# Predictor protocol
# --------------------------------------------------------------------------- #

# A predictor receives one simulator record (plus whatever extra context the
# adapter wants) and returns predictions.  Supported return shapes:
#
#   {"devices": {device_id: {"power_w": float, "source": str, "confidence": float}},
#    "reconciliation": {"unknown_residual_w": float, ...}}
#
# or a bare mapping device_id -> float (source defaults to "inferred").
Predictor = Callable[[dict[str, Any]], Mapping[str, Any]]


class DevicePrediction(Protocol):
    device_id: str
    power_w: float
    source: str
    confidence: float


DEFAULT_STALE_AGE_S = 120.0


# --------------------------------------------------------------------------- #
# Metric primitives
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ErrorStats:
    n: int = 0
    mae_w: float = 0.0
    rmse_w: float = 0.0
    mean_bias_w: float = 0.0
    max_abs_w: float = 0.0
    relative_error: float = 0.0          # MAPE-like over samples with truth > threshold
    energy_error_kwh: float = 0.0        # mean(pred - truth) * duration: a coarse estimate
    energy_error_ratio: float = 0.0
    #: Signed error of the **integrated** prediction against the simulator's own
    #: ``ground_truth.device_energy_kwh`` curve.  ``energy_error_kwh`` above is
    #: ``mean(bias) * duration``, which assumes the error is constant in time; on
    #: a spiky load (a water heater cycling on/off) that is not true and the two
    #: numbers differ.  Both are kept because they answer different questions.
    energy_error_kwh_integral: float = 0.0
    energy_error_ratio_integral: float = 0.0
    sample_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mae_w": round(self.mae_w, 4),
            "rmse_w": round(self.rmse_w, 4),
            "mean_bias_w": round(self.mean_bias_w, 4),
            "max_abs_w": round(self.max_abs_w, 4),
            "relative_error": round(self.relative_error, 4),
            "energy_error_kwh": round(self.energy_error_kwh, 6),
            "energy_error_ratio": round(self.energy_error_ratio, 5),
            "energy_error_kwh_integral": round(self.energy_error_kwh_integral, 9),
            "energy_error_ratio_integral": round(self.energy_error_ratio_integral, 6),
        }


def trapezoid_energy_kwh(samples: Sequence[tuple[float, float]]) -> float:
    """Integrate a ``(seconds, watts)`` series to kWh by the trapezoid rule.

    Samples are sorted by time and non-finite values are skipped.  Fewer than
    two usable samples integrates to ``0.0``: there is no interval to measure.

    The trapezoid rule is what the simulator itself uses to build
    ``ground_truth.device_energy_kwh``, so an oracle predictor integrates to
    exactly the simulator's curve.  ``mean(watts) * duration`` does **not**: it
    is only correct when the prediction error is constant over the window.
    """
    usable = sorted(
        (float(t), float(w))
        for t, w in samples
        if math.isfinite(float(t)) and math.isfinite(float(w))
    )
    if len(usable) < 2:
        return 0.0
    acc = 0.0
    for (t0, p0), (t1, p1) in zip(usable, usable[1:], strict=False):
        acc += (p0 + p1) * 0.5 * (t1 - t0)
    return acc / 3_600_000.0


def error_stats(
    pairs: Sequence[tuple[float, float]],
    *,
    duration_s: float = 0.0,
    relative_floor_w: float = 25.0,
    timed: Sequence[tuple[float, float, float]] | None = None,
    truth_energy_kwh: float | None = None,
) -> ErrorStats:
    """Error metrics from ``(truth, predicted)`` pairs.

    ``relative_error`` ignores near-zero truth samples (below ``relative_floor_w``)
    because relative error is meaningless when dividing by standby noise.

    ``timed`` optionally supplies ``(seconds, truth_w, predicted_w)`` triples so
    the *integrated* energy error can be computed properly.  ``duration_s``
    alone can only produce the coarse ``mean(bias) * duration`` estimate; see
    :attr:`ErrorStats.energy_error_kwh_integral`.  ``truth_energy_kwh`` lets the
    caller pass the simulator's own integrated truth curve; when omitted the
    integrated truth is recomputed from ``timed``.
    """
    n = len(pairs)
    if n == 0:
        return ErrorStats()
    abs_err = [abs(p - t) for t, p in pairs]
    sq_err = [(p - t) ** 2 for t, p in pairs]
    signed = [p - t for t, p in pairs]
    rel = [abs(p - t) / t for t, p in pairs if t >= relative_floor_w]
    energy_err = 0.0
    truth_energy = 0.0
    if duration_s > 0:
        # kWh from mean W over the window.  Retained for backward compatibility:
        # it is exact only when the error is constant across the window.
        energy_err = (sum(signed) / n) * duration_s / 3_600_000.0
        truth_energy = (sum(t for t, _ in pairs) / n) * duration_s / 3_600_000.0
    integral_err = 0.0
    integral_truth = 0.0
    if timed:
        integral_truth = (
            float(truth_energy_kwh)
            if truth_energy_kwh is not None
            else trapezoid_energy_kwh([(t, tr) for t, tr, _ in timed])
        )
        integrated_pred = trapezoid_energy_kwh([(t, p) for t, _, p in timed])
        integral_err = integrated_pred - integral_truth
    return ErrorStats(
        n=n,
        mae_w=sum(abs_err) / n,
        rmse_w=math.sqrt(sum(sq_err) / n),
        mean_bias_w=sum(signed) / n,
        max_abs_w=max(abs_err),
        relative_error=(sum(rel) / len(rel)) if rel else 0.0,
        energy_error_kwh=energy_err,
        energy_error_ratio=(abs(energy_err) / truth_energy) if truth_energy else 0.0,
        energy_error_kwh_integral=integral_err,
        energy_error_ratio_integral=(
            abs(integral_err) / integral_truth if integral_truth else 0.0
        ),
        sample_count=n,
    )


@dataclass(slots=True)
class DetectionStats:
    """ON/OFF detection confusion matrix (threshold on truth power)."""

    on_threshold_w: float = 10.0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        # No predicted positives means no false alarms: report 1.0 rather than
        # 0, so a device that is genuinely never on is not scored as wrong.
        return self.tp / d if d else 1.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return (2 * p * r / (p + r)) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        d = self.tp + self.fp + self.fn + self.tn
        return (self.tp + self.tn) / d if d else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "accuracy": round(self.accuracy, 4),
            "on_threshold_w": self.on_threshold_w,
        }


@dataclass(slots=True)
class ReconciliationStats:
    n: int = 0
    mean_abs_error_w: float = 0.0
    max_abs_error_w: float = 0.0
    mean_abs_error_ratio: float = 0.0
    residual_preserved_ratio: float = 0.0
    forced_attribution_count: int = 0
    over_attribution_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean_abs_reconciliation_error_w": round(self.mean_abs_error_w, 4),
            "max_abs_reconciliation_error_w": round(self.max_abs_error_w, 4),
            "mean_abs_reconciliation_error_ratio": round(self.mean_abs_error_ratio, 5),
            "residual_preserved_ratio": round(self.residual_preserved_ratio, 4),
            "forced_attribution_count": self.forced_attribution_count,
            "over_attribution_count": self.over_attribution_count,
        }


@dataclass(slots=True)
class CoverageStats:
    n: int = 0
    attributed_w: float = 0.0
    truth_attributable_w: float = 0.0
    coverage: float = 0.0
    unknown_load_w: float = 0.0
    unknown_load_pct: float = 0.0
    false_attribution_w: float = 0.0
    false_attribution_rate: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "attributed_w": round(self.attributed_w, 3),
            "truth_attributable_w": round(self.truth_attributable_w, 3),
            "attribution_coverage": round(self.coverage, 4),
            "unknown_load_w": round(self.unknown_load_w, 3),
            "unknown_load_pct": round(self.unknown_load_pct, 4),
            "false_attribution_w": round(self.false_attribution_w, 3),
            "false_attribution_rate": round(self.false_attribution_rate, 4),
        }


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class StratumReport:
    key: str
    n_records: int = 0
    error: ErrorStats = field(default_factory=ErrorStats)
    detection: DetectionStats = field(default_factory=DetectionStats)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stratum": self.key,
            "n_records": self.n_records,
            "error": self.error.as_dict(),
            "detection": self.detection.as_dict(),
        }


@dataclass(slots=True)
class EvaluationReport:
    per_device: dict[str, ErrorStats] = field(default_factory=dict)
    per_device_detection: dict[str, DetectionStats] = field(default_factory=dict)
    per_source: dict[str, ErrorStats] = field(default_factory=dict)
    per_source_detection: dict[str, DetectionStats] = field(default_factory=dict)
    per_device_type: dict[str, ErrorStats] = field(default_factory=dict)
    per_concurrency: dict[str, ErrorStats] = field(default_factory=dict)
    per_quality: dict[str, ErrorStats] = field(default_factory=dict)
    reconciliation: ReconciliationStats = field(default_factory=ReconciliationStats)
    coverage: CoverageStats = field(default_factory=CoverageStats)
    invariants: dict[str, Any] = field(default_factory=dict)
    n_records: int = 0
    scenario: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_records": self.n_records,
            "scenario": self.scenario,
            "per_device": {k: v.as_dict() for k, v in self.per_device.items()},
            "per_device_detection": {k: v.as_dict() for k, v in self.per_device_detection.items()},
            "per_source": {k: v.as_dict() for k, v in self.per_source.items()},
            "per_source_detection": {k: v.as_dict() for k, v in self.per_source_detection.items()},
            "per_device_type": {k: v.as_dict() for k, v in self.per_device_type.items()},
            "per_concurrency": {k: v.as_dict() for k, v in self.per_concurrency.items()},
            "per_quality": {k: v.as_dict() for k, v in self.per_quality.items()},
            "reconciliation": self.reconciliation.as_dict(),
            "coverage": self.coverage.as_dict(),
            "invariants": self.invariants,
            "notes": self.notes,
        }

    def summary_lines(self) -> list[str]:
        lines = [
            f"records: {self.n_records}",
            f"coverage: {self.coverage.coverage:.3f}  unknown: {self.coverage.unknown_load_pct:.3f}",
            f"false attribution rate: {self.coverage.false_attribution_rate:.4f}",
            f"reconciliation MAE: {self.reconciliation.mean_abs_error_w:.2f} W",
        ]
        for source, st in sorted(self.per_source.items()):
            lines.append(
                f"  source={source}: n={st.n} mae={st.mae_w:.2f}W "
                f"rmse={st.rmse_w:.2f}W rel={st.relative_error:.4f}"
            )
        return lines


# --------------------------------------------------------------------------- #
# Normalisation of predictor output
# --------------------------------------------------------------------------- #


def _normalize_prediction(raw: Mapping[str, Any] | float, device_id: str) -> dict[str, Any] | None:
    if isinstance(raw, (int, float)):
        return {"device_id": device_id, "power_w": float(raw), "source": "inferred",
                "confidence": 0.5, "known": True}
    if not isinstance(raw, Mapping):
        return None
    power = raw.get("power_w", raw.get("power"))
    if power is None:
        return None
    return {
        "device_id": raw.get("device_id", device_id),
        "power_w": float(power),
        "source": str(raw.get("source", "inferred")),
        "confidence": float(raw.get("confidence", 0.5)),
        "known": bool(raw.get("known", True)),
    }


def _device_iter(pred: Mapping[str, Any]) -> Mapping[str, Any]:
    devices = pred.get("devices")
    if isinstance(devices, Mapping):
        return devices
    # Bare mapping: filter out non-device keys we recognise.
    return {k: v for k, v in pred.items() if k not in ("reconciliation", "quality", "diagnostics")}


# --------------------------------------------------------------------------- #
# Evaluator
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Acc:
    truth: list[float] = field(default_factory=list)
    pred: list[float] = field(default_factory=list)
    det: DetectionStats = field(default_factory=DetectionStats)
    #: ``(seconds, truth_w, predicted_w)`` for the same samples, used by the
    #: integral energy metric.  Kept separately so the per-sample lists above
    #: stay in the order the records arrived.
    timed: list[tuple[float, float, float]] = field(default_factory=list)

    def add(self, truth: float, pred: float, threshold: float, *, at_s: float | None = None) -> None:
        self.truth.append(truth)
        self.pred.append(pred)
        if at_s is not None and math.isfinite(at_s):
            self.timed.append((at_s, truth, pred))
        truth_on = truth >= threshold
        pred_on = pred >= threshold
        if truth_on and pred_on:
            self.det.tp += 1
        elif truth_on:
            self.det.fn += 1
        elif pred_on:
            self.det.fp += 1
        else:
            self.det.tn += 1


def evaluate(
    records: Iterable[dict[str, Any]],
    predictor: Predictor,
    *,
    on_threshold_w: float = 10.0,
    relative_floor_w: float = 25.0,
    stale_age_s: float = DEFAULT_STALE_AGE_S,
    exclude_unknown_devices: bool = True,
    panel_channels: Mapping[str, Sequence[str]] | None = None,
) -> EvaluationReport:
    """Run ``predictor`` over simulator ``records`` and compute stratified metrics.

    Stratification is done on the *predicted* source for source tiers, and on
    simulator metadata for type / concurrency / quality.

    Predictions are scored against ground truth at the **record timestamp**, so
    the numbers include measurement latency (a reading delivered now physically
    describes an instant up to one CT cadence in the past).  This is the
    conservative definition: it never credits the engine for information it
    did not have, and it keeps the oracle self-check exact.
    """
    acc_device: dict[str, _Acc] = defaultdict(_Acc)
    acc_source: dict[str, _Acc] = defaultdict(_Acc)
    acc_type: dict[str, _Acc] = defaultdict(_Acc)
    acc_conc: dict[str, _Acc] = defaultdict(_Acc)
    acc_quality: dict[str, _Acc] = defaultdict(_Acc)

    rec_n = 0
    recon_errors: list[float] = []
    recon_ratios: list[float] = []
    residual_preserved = 0
    residual_checked = 0
    residual_understated = 0
    forced = 0
    negative_residual = 0
    over_attributed = 0
    attributed_sum = 0.0
    truth_attr_sum = 0.0
    unknown_truth_sum = 0.0
    main_truth_sum = 0.0
    false_attr_w = 0.0
    total_pred_attr_w = 0.0
    # Nested-CT detection.  A typical two-panel install has the sub panel
    # fed from the main panel, so the true whole-home draw is the root mains
    # only.  A predictor that sums *every* main CT (main L1 + main L2 + sub L1
    # + sub L2) reports approximately ``root + sub``.  To distinguish that from
    # ordinary sample-vintage skew we compare the claimed main against both the
    # measured root sum and the measured nested sum, and only flag the record
    # when the claim is unambiguously closer to the nested sum.  ``panel_channels``
    # supplies the split; without it the check cannot run and stays at zero.
    root_cts = list((panel_channels or {}).get("root", ()))
    sub_cts = list((panel_channels or {}).get("sub", ()))
    nested_checked = 0
    nested_double_counts = 0
    direct_exceeds_main = 0
    headroom_violations = 0
    forced_from_stale = 0
    stale_learning_violations = 0

    intervals: list[float] = []
    prev_ts: datetime | None = None
    device_types: dict[str, str] = {}
    scenario_meta: dict[str, Any] = {}
    #: Per-device simulated energy curve, from the *last* record.  The simulator
    #: integrates its own truth, so this is the reference the integrated
    #: prediction is compared against.
    truth_energy_curve: dict[str, float] = {}

    for rec in records:
        rec_n += 1
        scenario_meta = rec.get("scenario", scenario_meta)
        gt = rec["ground_truth"]
        truth_devices = gt["devices"]
        main_truth = float(gt.get("main_total_w", 0.0))
        unmetered_truth = float(gt.get("unmetered_w", 0.0))
        truth_energy_curve = gt.get("device_energy_kwh") or truth_energy_curve
        ts = rec.get("timestamp")
        at_s: float | None = None
        if isinstance(ts, str):
            try:
                cur = datetime.fromisoformat(ts)
                at_s = cur.timestamp()
                if prev_ts is not None:
                    intervals.append((cur - prev_ts).total_seconds())
                prev_ts = cur
            except ValueError:
                pass

        raw_pred = predictor(rec)
        predicted = _device_iter(raw_pred)

        pred_norm: dict[str, dict[str, Any]] = {}
        for dev_id, val in predicted.items():
            norm = _normalize_prediction(val, dev_id)
            if norm is not None:
                pred_norm[dev_id] = norm

        # -- per device ------------------------------------------------- #
        latest = rec.get("latest_measurements", {})
        default_q = str(rec.get("scenario", {}).get("quality", "unknown"))

        for dev_id, tinfo in truth_devices.items():
            dev_type = tinfo.get("device_type", "unknown")
            device_types[dev_id] = dev_type
            attributable = bool(tinfo.get("attributable", True))
            if exclude_unknown_devices and not attributable:
                continue
            truth_p = float(tinfo.get("power_w", 0.0))
            norm = pred_norm.get(dev_id)
            # A missing prediction is treated as zero *only* when the device is
            # off; predicting nothing for an active device must be penalised.
            pred_p = float(norm["power_w"]) if norm else 0.0
            if norm is not None and norm["confidence"] is not None and pred_p < 0:
                pred_p = 0.0
            acc_device[dev_id].add(truth_p, pred_p, on_threshold_w, at_s=at_s)
            source = norm["source"] if norm else "unknown"
            acc_source[source].add(truth_p, pred_p, on_threshold_w, at_s=at_s)
            acc_type[dev_type].add(truth_p, pred_p, on_threshold_w, at_s=at_s)
            conc = str(int(gt.get("concurrency", 0)))
            acc_conc[conc].add(truth_p, pred_p, on_threshold_w, at_s=at_s)

        # -- data-quality stratum (per device, own supporting channels) --- #
        for dev_id, tinfo in truth_devices.items():
            if exclude_unknown_devices and not tinfo.get("attributable", True):
                continue
            own = [
                latest[c].get("quality", "unknown")
                for c in tinfo.get("branch_cts", [])
                if c in latest
            ]
            qkey = _worst_quality(own) if own else default_q
            norm = pred_norm.get(dev_id)
            pred_p = float(norm["power_w"]) if norm else 0.0
            acc_quality[qkey].add(float(tinfo.get("power_w", 0.0)), pred_p, on_threshold_w, at_s=at_s)

        # -- coverage / attribution -------------------------------------- #
        attributed = sum(
            float(n["power_w"]) for dev_id, n in pred_norm.items()
            if truth_devices.get(dev_id, {}).get("attributable", True)
            and n["source"] in ("direct_measured", "inferred", "model_estimated")
            and n["power_w"] > 0
        )
        truth_attr = sum(
            float(t.get("power_w", 0.0)) for dev_id, t in truth_devices.items()
            if t.get("attributable", True)
        )
        attributed_sum += attributed
        truth_attr_sum += truth_attr
        unknown_truth_sum += unmetered_truth
        main_truth_sum += main_truth
        total_pred_attr_w += attributed
        # False attribution: predicted power for a device that is truly off at
        # the instant its own supporting sample was taken.
        for dev_id, norm in pred_norm.items():
            tinfo = truth_devices.get(dev_id)
            if tinfo is None or not tinfo.get("attributable", True):
                if norm["power_w"] > on_threshold_w:
                    false_attr_w += norm["power_w"]
                continue
            if float(tinfo.get("power_w", 0.0)) < on_threshold_w and norm["power_w"] > on_threshold_w:
                false_attr_w += norm["power_w"]

        # -- reconciliation ---------------------------------------------- #
        rec_out = raw_pred.get("reconciliation") if isinstance(raw_pred, Mapping) else None
        if isinstance(rec_out, Mapping):
            reported = float(rec_out.get("unknown_residual_w", 0.0))
            reported_main = float(rec_out.get("main_power_w", main_truth))
            # Residual must equal main - accounted (tolerance 2% or 5 W).
            expected_residual = reported_main - attributed
            rerr = abs(reported - expected_residual)
            recon_errors.append(rerr)
            recon_ratios.append(rerr / reported_main if reported_main else 0.0)
            # Residual preservation (no-fabrication).  The engine may only ever
            # *under*-attribute: any power it cannot justify belongs in
            # ``unknown_residual_w``.  Therefore the residual must (a) equal
            # main - accounted (checked above) and (b) never fall below the
            # genuinely unattributable truth.  A residual *larger* than the
            # unmetered truth is not a violation: shared-branch devices that
            # the measurements cannot separate are legitimately left there.
            residual_checked += 1
            if reported >= unmetered_truth - max(0.10 * max(unmetered_truth, 1.0), 5.0):
                residual_preserved += 1
            else:
                residual_understated += 1
            direct = sum(
                float(n["power_w"]) for n in pred_norm.values()
                if n["source"] == "direct_measured"
            )
            inferred_model = attributed - direct
            # Forced attribution: the predictor invents *inferred/model* power
            # for devices that are physically OFF at this instant.  That is the
            # literal failure mode the spec forbids ("never force arbitrary
            # device attribution").  Judging instead by
            # "inferred > main - direct" conflates three unrelated effects:
            # stale dedicated CTs that under-report, stale main readings, and
            # sample-vintage skew.  All three are reported on their own axes
            # (``direct_exceeds_main_records``, ``negative_residual_records``,
            # ``over_attribution_count``), so they must not be re-counted here.
            forced_w = 0.0
            forced_from_stale_w = 0.0
            for dev_id, norm in pred_norm.items():
                if norm["source"] not in ("inferred", "model_estimated"):
                    continue
                tinfo = truth_devices.get(dev_id)
                if tinfo is None or not tinfo.get("attributable", True):
                    continue
                if float(tinfo.get("power_w", 0.0)) < on_threshold_w and norm["power_w"] > on_threshold_w:
                    # Distinguish *engine* fabrication from *input* corruption:
                    # if the device's own supporting CT was delivered as a
                    # repeated/stale sample, the engine was handed a wrong
                    # value and faithfully propagated it.  That is still a
                    # false attribution, but it belongs on the data-quality
                    # axis, not on the "engine invents power" axis.
                    own = [
                        latest[c]
                        for c in tinfo.get("branch_cts", [])
                        if c in latest
                    ]
                    stale_input = bool(own) and all(
                        bool(m.get("repeated"))
                        or str(m.get("quality")) in ("stale", "missing", "bad")
                        for m in own
                    )
                    if stale_input:
                        forced_from_stale_w += norm["power_w"]
                    else:
                        forced_w += norm["power_w"]
            if forced_w > max(0.01 * abs(main_truth), 5.0):
                forced += 1
            if forced_from_stale_w > max(0.01 * abs(main_truth), 5.0):
                forced_from_stale += 1
            # Headroom self-consistency: inferred/model power should not exceed
            # the power the predictor itself says is left after its direct
            # readings.  Kept as a soft signal because a stale dedicated CT or
            # a stale main sample can legitimately break it for one record.
            if direct <= reported_main + max(0.02 * abs(reported_main), 5.0) and (
                inferred_model > max(0.0, reported_main - direct) + max(0.02 * abs(reported_main), 5.0)
            ):
                headroom_violations += 1
            # Sample-vintage skew: direct readings alone exceed the physical
            # whole-home draw, so the dedicated CTs are describing an earlier
            # instant.  Not fabrication; counted separately.
            physical_main = main_truth if main_truth > 0 else reported_main
            if direct > physical_main + max(0.02 * abs(physical_main), 10.0):
                direct_exceeds_main += 1
            # Negative residual: account > main.  With direct CTs this is a
            # *sample-vintage* artefact (two channels observed at different
            # instants), not fabrication, so it is counted separately.
            if attributed > reported_main + max(0.02 * abs(reported_main), 5.0):
                negative_residual += 1
            if attributed > reported_main + max(0.05 * abs(reported_main), 10.0):
                over_attributed += 1
            # Nested double counting: the claimed main equals the sum of *every*
            # panel feed (root + sub), so the sub-panel is counted twice.  The
            # claim is compared against the same measurement snapshot the
            # predictor sees, with a tight tolerance: a genuine "sum all mains"
            # bug reproduces ``root + sub`` exactly, whereas sample-vintage
            # skew between root and sub channels is a few percent at most.
            if root_cts and sub_cts:
                measured_root = sum(_measurement_value(latest, ct) for ct in root_cts)
                measured_sub = sum(_measurement_value(latest, ct) for ct in sub_cts)
                # The buggy claim is exactly "main = root feed + sub feed".
                measured_nested = measured_root + measured_sub
                if measured_sub > max(0.03 * measured_root, 50.0):
                    nested_checked += 1
                    claim_matches_nested = abs(reported_main - measured_nested) <= max(
                        0.02 * measured_sub, 25.0
                    )
                    # A numerically correct claim can coincide with ``root+sub``
                    # when the root sample itself is stale (a single missed poll
                    # leaves the root CT reading low by roughly the sub-panel's
                    # draw).  That is sample-vintage skew, not a double count.
                    # A double count is only proven when the claim is also
                    # materially wrong about the physical whole-home draw.
                    claim_matches_truth = abs(reported_main - main_truth) <= max(
                        0.02 * abs(main_truth), 25.0
                    )
                    if claim_matches_nested and not claim_matches_truth:
                        nested_double_counts += 1
            # Sample-vintage skew, second view (claimed main vs the physical main
            # actually delivered) is already counted just above.  Counting it
            # again here would double-weight the same condition in a metric that
            # is reported as a record count, so it is deliberately not repeated.

        # -- stale-sample learning check --------------------------------- #
        # Any predictor that reports it learned from a stale channel sample is
        # violating the contract.  We detect via provenance markers.
        provenance = raw_pred.get("provenance") if isinstance(raw_pred, Mapping) else None
        stale_used = False
        if isinstance(provenance, Mapping):
            stale_used = bool(provenance.get("used_stale_for_learning"))
        elif isinstance(provenance, list):
            stale_used = any("stale_for_learning" in str(p) for p in provenance)
        if stale_used:
            stale_learning_violations += 1

    # -- build report --------------------------------------------------- #
    window_s = sum(intervals) if intervals else 0.0
    report = EvaluationReport(n_records=rec_n, scenario={
        "seed": scenario_meta.get("seed"),
        "minutes": scenario_meta.get("minutes"),
        "interval_s": scenario_meta.get("interval_s"),
        "quality": scenario_meta.get("quality"),
    })

    for dev_id, acc in acc_device.items():
        report.per_device[dev_id] = error_stats(
            list(zip(acc.truth, acc.pred, strict=True)),
            duration_s=(window_s / max(1, len(acc.truth))) * len(acc.truth),
            relative_floor_w=relative_floor_w,
            timed=acc.timed,
            truth_energy_kwh=truth_energy_curve.get(dev_id),
        )
        report.per_device_detection[dev_id] = acc.det
    for key, acc in acc_source.items():
        report.per_source[key] = error_stats(
            list(zip(acc.truth, acc.pred, strict=True)),
            duration_s=(window_s / max(1, len(acc.truth))) * len(acc.truth),
            relative_floor_w=relative_floor_w,
            timed=acc.timed,
        )
        report.per_source_detection[key] = acc.det
    for key, acc in acc_type.items():
        report.per_device_type[key] = error_stats(
            list(zip(acc.truth, acc.pred, strict=True)),
            relative_floor_w=relative_floor_w,
            timed=acc.timed,
        )
    for key, acc in acc_conc.items():
        report.per_concurrency[key] = error_stats(
            list(zip(acc.truth, acc.pred, strict=True)),
            relative_floor_w=relative_floor_w,
            timed=acc.timed,
        )
    for key, acc in acc_quality.items():
        report.per_quality[key] = error_stats(
            list(zip(acc.truth, acc.pred, strict=True)),
            relative_floor_w=relative_floor_w,
            timed=acc.timed,
        )

    n = max(1, rec_n)
    report.reconciliation = ReconciliationStats(
        n=len(recon_errors),
        mean_abs_error_w=(sum(recon_errors) / len(recon_errors)) if recon_errors else 0.0,
        max_abs_error_w=max(recon_errors) if recon_errors else 0.0,
        mean_abs_error_ratio=(sum(recon_ratios) / len(recon_ratios)) if recon_ratios else 0.0,
        residual_preserved_ratio=(residual_preserved / residual_checked) if residual_checked else 0.0,
        forced_attribution_count=forced,
        over_attribution_count=over_attributed,
    )
    report.coverage = CoverageStats(
        n=rec_n,
        attributed_w=round(attributed_sum / n, 3),
        truth_attributable_w=round(truth_attr_sum / n, 3),
        coverage=(attributed_sum / truth_attr_sum) if truth_attr_sum else 0.0,
        unknown_load_w=round(unknown_truth_sum / n, 3),
        unknown_load_pct=((unknown_truth_sum / main_truth_sum) if main_truth_sum else 0.0),
        false_attribution_w=round(false_attr_w / n, 3),
        false_attribution_rate=(false_attr_w / max(1.0, total_pred_attr_w)),
    )
    report.invariants = {
        "residual_preserved_ratio": report.reconciliation.residual_preserved_ratio,
        "residual_understated_records": residual_understated,
        "forced_attribution_count": forced,
        "negative_residual_records": negative_residual,
        "nested_double_count_records": nested_double_counts,
        "nested_check_records": nested_checked,
        "direct_exceeds_main_records": direct_exceeds_main,
        "headroom_violation_records": headroom_violations,
        "forced_attribution_from_stale_input_records": forced_from_stale,
        "stale_learning_violations": stale_learning_violations,
        "device_types_observed": len(set(device_types.values())),
        "checked_records": rec_n,
    }
    return report


def coverage_and_invariants(
    records: Iterable[dict[str, Any]], predictor: Predictor
) -> dict[str, Any]:
    """Convenience wrapper returning just coverage + invariants."""
    report = evaluate(records, predictor)
    return {
        "coverage": report.coverage.coverage,
        "unknown_load_pct": report.coverage.unknown_load_pct,
        "false_attribution_rate": report.coverage.false_attribution_rate,
        **report.invariants,
    }


def _measurement_value(latest: Mapping[str, Any], ct_id: str) -> float:
    """Reported watts for one channel in a ``latest_measurements`` snapshot."""
    m = latest.get(ct_id)
    if not isinstance(m, Mapping):
        return 0.0
    value = m.get("value_w", m.get("value"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def panel_channel_map(
    topology: Any,
    series: Mapping[str, Sequence[float | None]] | None = None,
    *,
    containment_min: float = 0.99,
    reverse_max: float = 0.50,
) -> dict[str, list[str]]:
    """Split a topology's panel-feed CTs into root and sub-panel feeds.

    ``{"root": [...], "sub": [...]}`` for :func:`evaluate`'s ``panel_channels``
    argument.  "Root" channels are the whole-home mains; "sub" channels are the
    intermediate feeds (the sub-panel mains) that are *already included* in the
    root measurement.  Summing a root channel with a sub channel therefore
    double counts the sub-panel.

    Structure comes first, from declared parents:

    * a ``main`` CT with no parent is a root;
    * a ``main`` CT under another feed is a sub feed (the commonest real shape);
    * a branch with children that has a main ancestor is a sub feed;
    * an aliased channel is excluded -- it measures a wire a canonical channel
      already measures, so it is neither a root nor a distinct sub feed.

    ``series`` supplies the fallback for a **parentless** topology: when no
    declared parent places a feed, the split is derived from measurements.  A
    main that is contained by another main at ``containment_min`` of instants
    while the reverse holds at most ``reverse_max`` of the time is fed by it --
    one direction only, which is the same evidence
    ``wattmosaic_engine.electrical.nesting`` uses.  Without this fallback the
    guard is vacuous on exactly the topology that needs it most: an install whose
    panel tree has not been declared yet.

    This works for both the simulator's ``HouseholdTopology`` and a contract
    ``Topology``.
    """
    channels = list(getattr(topology, "channels", []))
    parent_of = {ch.ct_id: getattr(ch, "parent_ct_id", None) for ch in channels}
    alias_of = {ch.ct_id: getattr(ch, "alias_of_ct_id", None) for ch in channels}
    has_child = {p for p in parent_of.values() if p}
    ctype_of = {
        ch.ct_id: getattr(getattr(ch, "ct_type", None), "value", getattr(ch, "ct_type", None))
        for ch in channels
    }
    main_ids = {cid for cid, ctype in ctype_of.items() if ctype == "main"}

    def has_main_ancestor(ct_id: str) -> bool:
        seen: set[str] = set()
        current = parent_of.get(ct_id)
        while current is not None and current not in seen:
            if current in main_ids:
                return True
            seen.add(current)
            current = parent_of.get(current)
        return False

    root: list[str] = []
    sub: list[str] = []
    for ch in channels:
        if alias_of.get(ch.ct_id):
            continue
        ctype = ctype_of[ch.ct_id]
        if ctype == "main":
            if parent_of[ch.ct_id] is None:
                root.append(ch.ct_id)
            else:
                # A main feed under another feed: already counted by its parent.
                sub.append(ch.ct_id)
        elif ch.ct_id in has_child and has_main_ancestor(ch.ct_id):
            sub.append(ch.ct_id)

    if not sub and series:
        root, sub = _split_mains_by_containment(
            root,
            series,
            containment_min=containment_min,
            reverse_max=reverse_max,
        )
    return {"root": root, "sub": sub}


def _containment(
    child: Sequence[float | None], parent: Sequence[float | None], tol: float = 30.0
) -> float | None:
    """Fraction of instants where ``child <= parent`` (plus tolerance).

    A circuit cannot draw more power than the feed that supplies it, so this is
    a *necessary* condition for being fed by that parent.  One-directional
    containment is what separates a panel feed from its parent; mutual
    containment means two channels watch the same wire.
    """
    pairs = [
        (float(c), float(p))
        for c, p in zip(child, parent, strict=False)
        if c is not None and p is not None
    ]
    if not pairs:
        return None
    inside = sum(1 for c, p in pairs if c <= p + max(0.02 * abs(p), tol))
    return inside / len(pairs)


def _split_mains_by_containment(
    candidates: Sequence[str],
    series: Mapping[str, Sequence[float | None]],
    *,
    containment_min: float,
    reverse_max: float,
) -> tuple[list[str], list[str]]:
    """Classify mains as root/sub from one-directional containment.

    ``a`` is a sub feed of ``b`` when ``a`` is inside ``b`` almost always and
    ``b`` is inside ``a`` rarely.  A main fed by *any* other main is removed from
    the roots; the outermost feeds stay.  When containment is mutual the pair is
    two CTs on one conductor (an alias), which the caller handles separately, so
    neither is demoted here.
    """
    present = [cid for cid in candidates if series.get(cid)]
    fed_by: dict[str, str] = {}
    for child in present:
        best: tuple[float, str] | None = None
        for parent in present:
            if parent == child:
                continue
            forward = _containment(series[child], series[parent])
            backward = _containment(series[parent], series[child])
            if forward is None or backward is None:
                continue
            if (
                forward >= containment_min
                and backward <= reverse_max
                and (best is None or forward > best[0])
            ):
                best = (forward, parent)
        if best is not None:
            fed_by[child] = best[1]
    root = [cid for cid in candidates if cid not in fed_by]
    sub = [cid for cid in candidates if cid in fed_by]
    return root, sub


def _worst_quality(qualities: Sequence[str]) -> str:
    order = {"good": 0, "degraded": 1, "stale": 2, "missing": 3, "bad": 4}
    if not qualities:
        return "missing"
    return max(qualities, key=lambda q: order.get(q, 0))


# --------------------------------------------------------------------------- #
# Self-check predictors (prove the metrics catch bad behaviour)
# --------------------------------------------------------------------------- #


def oracle_predictor(record: dict[str, Any]) -> dict[str, Any]:
    """Perfect predictor: returns exact ground truth.

    Used to prove the harness reports ~0 error when behaviour is correct.
    Unmetered loads are never given a device value (they are residual only).
    """
    gt = record["ground_truth"]
    devices = {}
    for dev_id, info in gt["devices"].items():
        if not info.get("attributable", True):
            continue
        devices[dev_id] = {
            "device_id": dev_id,
            "power_w": info["power_w"],
            "source": "direct_measured" if info.get("dedicated_ct") else "inferred",
            "confidence": 1.0,
            "known": True,
        }
    accounted = sum(d["power_w"] for d in devices.values())
    main = gt["main_total_w"]
    return {
        "devices": devices,
        "reconciliation": {
            "main_power_w": main,
            "unknown_residual_w": max(0.0, main - accounted),
        },
    }


def naive_sum_wrong_predictor(record: dict[str, Any]) -> dict[str, Any]:
    """Deliberately wrong: attributes the whole main power to every device.

    This is the classic "plausible-looking but meaningless" failure.  The
    harness must report huge MAE, false attribution ~1.0, and forced
    attribution on every record.
    """
    gt = record["ground_truth"]
    main = float(gt["main_total_w"])
    devices = {
        dev_id: {
            "device_id": dev_id,
            "power_w": main,
            "source": "direct_measured",
            "confidence": 0.9,
            "known": True,
        }
        for dev_id in gt["devices"]
    }
    return {
        "devices": devices,
        "reconciliation": {"main_power_w": main, "unknown_residual_w": 0.0},
    }


def flat_zero_predictor(record: dict[str, Any]) -> dict[str, Any]:
    """Reports nothing.  Must show 0 coverage and full missed ON detection."""
    return {
        "devices": {},
        "reconciliation": {
            "main_power_w": float(record["ground_truth"]["main_total_w"]),
            "unknown_residual_w": float(record["ground_truth"]["main_total_w"]),
        },
    }


def main_channel_only_predictor(record: dict[str, Any]) -> dict[str, Any]:
    """Uses dedicated CTs where present, leaves shared devices unknown.

    A defensible baseline: never fabricates, but coverage is limited.
    """
    gt = record["ground_truth"]
    devices = {}
    for dev_id, info in gt["devices"].items():
        if info.get("dedicated_ct"):
            devices[dev_id] = {
                "device_id": dev_id,
                "power_w": info["power_w"],
                "source": "direct_measured",
                "confidence": 0.95,
                "known": True,
            }
    return {
        "devices": devices,
        "reconciliation": {
            "main_power_w": float(gt["main_total_w"]),
            "unknown_residual_w": float(gt["main_total_w"]),
        },
    }


# --------------------------------------------------------------------------- #
# Real-engine adapter (wired behind try/except; engine may not exist yet)
# --------------------------------------------------------------------------- #


def load_engine_predictor(topology: Any | None = None) -> tuple[Predictor | None, str]:
    """Attempt to adapt the real disaggregation engine.

    Returns ``(predictor, status)``.  ``status`` explains why it is unavailable;
    it never raises.  No engine code is modified.

    ``topology`` is a contract ``Topology`` (``HouseholdTopology.to_contract_topology()``).
    When omitted, the adapter tries to read it from the record's scenario metadata.
    """
    try:
        from wattmosaic_engine.contracts import Topology  # noqa: F401
        from wattmosaic_engine.disaggregation import DisaggregationEngine  # noqa: F401
    except Exception as exc:  # ImportError or partially-written module
        return None, f"engine disaggregation unavailable: {type(exc).__name__}: {exc}"

    try:
        import inspect

        sig = inspect.signature(DisaggregationEngine.infer)
        for required in ("window", "device_states", "models", "topology"):
            if required not in sig.parameters:
                return None, f"DisaggregationEngine.infer lacks parameter {required!r}"
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"engine signature inspection failed: {type(exc).__name__}: {exc}"

    if topology is None:
        return None, "no contract topology supplied to the engine adapter"

    return EngineAdapter(topology), "adapted"


class EngineAdapter:
    """Stateful adapter from simulator records to ``DisaggregationEngine``.

    It accumulates per-channel history so the engine's event detector sees a
    realistic cadence rather than a single isolated sample, and it feeds the
    *measured* values only — never ground truth.
    """

    def __init__(self, topology: Any, *, history_limit: int = 24) -> None:
        from wattmosaic_engine.disaggregation import DisaggregationEngine

        self.topology = topology
        self.engine = DisaggregationEngine()
        self.history_limit = history_limit
        self._history: dict[str, list[tuple[datetime, float]]] = {}
        self._last_states: dict[str, str] = {}

    def _window(self, record: dict[str, Any]):
        from wattmosaic_engine.contracts import Polarity, QualityStatus
        from wattmosaic_engine.electrical.window import ChannelReading, ObservationWindow

        at = datetime.fromisoformat(record["timestamp"])
        readings: dict[str, Any] = {}
        # Update history from samples actually received in this tick.
        for ct_id, m in record.get("measurements", {}).items():
            ts = datetime.fromisoformat(m["state_timestamp"])
            value = float(m["value_w"])
            hist = self._history.setdefault(ct_id, [])
            if not hist or hist[-1][0] != ts:
                hist.append((ts, value))
            del hist[:-self.history_limit]

        for ct_id, m in record.get("latest_measurements", {}).items():
            value = float(m["value_w"])
            pol = record.get("measurements", {}).get(ct_id, {}).get("polarization", "normal")
            readings[ct_id] = ChannelReading(
                channel_id=ct_id,
                value_w=value,
                age_s=float(m.get("age_s", 0.0)),
                quality=QualityStatus(m.get("quality", "good")),
                repeated=bool(m.get("repeated", False)),
                polarization=Polarity(pol),
                history=tuple(self._history.get(ct_id, ())),
                expected_interval_s=15.7,
            )
        return ObservationWindow(at=at, readings=readings, cadence_s=15.7), at

    def __call__(self, record: dict[str, Any]) -> dict[str, Any]:
        window, at = self._window(record)

        states = {}
        prev: dict[str, str] = {}
        for entity_id, st in record.get("ha_states", {}).items():
            dev_id = st.get("device_id")
            if not dev_id:
                continue
            if dev_id in self._last_states:
                prev[dev_id] = self._last_states[dev_id]
            self._last_states[dev_id] = st["state"]
            states[dev_id] = SimpleDeviceState(entity_id=entity_id, state=st["state"])

        window.previous_states.update(prev)
        batch = self.engine.infer(
            window,
            device_states=states,
            models=None,
            topology=self.topology,
        )
        devices = {
            r.device_id: {
                "device_id": r.device_id,
                "power_w": float(r.power_w),
                "source": str(r.source),
                "confidence": float(r.confidence),
            }
            for r in batch.results
        }
        rec = batch.reconciliation
        return {
            "devices": devices,
            "reconciliation": {
                "main_power_w": float(rec.main_power_w),
                "unknown_residual_w": float(rec.unknown_residual_w),
            },
            "diagnostics": batch.diagnostics,
        }


class SimpleDeviceState:
    """Minimal ``DeviceState``-shaped object the engine accepts."""

    __slots__ = ("entity_id", "state", "attributes", "last_changed", "last_updated")

    def __init__(self, entity_id: str, state: str) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes: dict[str, Any] = {}
        self.last_changed = None
        self.last_updated = None
