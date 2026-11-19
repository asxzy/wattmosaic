# Testing Strategy (contract 3.0)

Owner: Agent J (docs). Code: `tests/`, `tools/simulator/`. The mandatory
acceptance criteria are HB §21 in the
[technical handbook](../docs/specs/realtime-topology-disaggregation/handbook.md);
the test inventory and closing tasks are in the
[spec's traceability](../docs/specs/realtime-topology-disaggregation/traceability.md).

This document describes what the test layers prove and — most importantly —
what they *cannot* prove. Accuracy figures derived from simulated ground truth
live in [`docs/acceptance_report.md`](acceptance_report.md); this file is about
method.

---

## 1. The problem the tests have to solve

WattMosaic infers per-device power from a small number of endpoint measurements
in a real house. Accuracy claims are easy to fake: you can always produce numbers
that look good if you grade yourself against your own output, or if you report
one aggregate "score" that hides the devices the system cannot see.

QA therefore has three jobs:

1. **Generate truth that is not derived from the measurements.** The simulator's
   ground truth comes from its own seeded clock, not the measurement pipeline it
   scores.
2. **Grade against truth, stratified** — by estimate status, device type,
   concurrency, data quality and individual device. There is deliberately no
   single aggregate number.
3. **Check safety invariants separately from accuracy.** "Unknown stays
   unknown", "a non-identifiable member gets no number" and "no device is
   credited with power it did not draw" are correctness properties, asserted
   directly.

## 2. Layers

| Layer | Location | What it proves |
| --- | --- | --- |
| Unit | `tests/unit/` | contracts, null policy, config validation, vocabulary gates, release checks |
| Acceptance | `tests/acceptance/test_at01…at20.py` | the 20 HB §21 mandatory requirements against the §17 reference house |
| Simulator | `tests/simulation/` | the synthetic house, impairments, determinism and CLI contract |
| Evaluation harness | `tests/simulation/metrics.py` | grading is correct; known-bad predictors are caught |
| End-to-end | `tests/end_to_end/` | ingestion → runtime → storage → HTTP on real components |
| Integration | `tests/integration/` | device history, HA boundary behaviour |
| Validation tooling | `tests/simulation/test_validation_tools.py` | the documented offline reproduction commands actually run |

### 2.1 The 20 acceptance tests (HB §21)

`tests/acceptance/` holds exactly one file per mandatory requirement AT-01…AT-20.
Each file is in one of two states: **open** (one strict
`@pytest.mark.xfail(strict=True, reason="closed by <TASK>")`) or **closed** (no
xfail, a `CLOSED_BY` constant). A strict xfail means an accidental pass fails CI,
so progress stays visible. `test_closing_task_ids.py` enforces that contract and
checks each closing task id against the plan. The tests cover nested no-double-
count, parent residuals, response delays, joint inference, non-fabricated groups,
plug anchors, covariance, plug relocation, reader reconciliation, missing-not-zero,
late-event revision, replay, matrix cells and UI explanation.

### 2.2 Simulator (`tools/simulator/`)

A deterministic synthetic household reproduces the shape of a real
multi-panel install but describes no real building. It models nested panels,
double-pole 240 V loads, a measured ~15.7 s median CT cadence (not the old 2 s
assumption), all required device classes, and ordered impairments
(gain/drift/polarity → noise → quantisation → delivery delay →
staleness/repeats → dropouts). Three timestamps per sample are kept distinct:
`observed_at`, `received_at` and an optional hardware `physical_timestamp`.

Ground truth is evaluated from the simulator's own clock and RNG; the
measurement path only *impairs* it and never reads truth back.
`test_ground_truth_is_independent_of_measurements` asserts truth is byte-identical
across quality presets while measurements are not.

### 2.3 Evaluation harness

`evaluate(records, predictor, ...)` grades any predictor against truth and
returns a stratified report: per device, per estimate status
(`MEASURED`/`ESTIMATED`/`ALLOCATED`/`UNKNOWN`), per device type, per concurrency,
per quality axis, reconciliation and coverage. Nothing in the report is a single
scalar — `test_no_single_aggregate_score_is_exposed` fails if `accuracy`, `score`
or `overall` ever appears at the top level.

Safety invariants are asserted directly, including:

| Invariant | Meaning |
| --- | --- |
| `forced_attribution_count` | power claimed for a device genuinely OFF — the unambiguous fabrication test; must be 0 |
| residual preservation | under-attributing leaves a larger residual (legitimate); over-attributing shrinks it below truth (not) |
| over-attribution | attributed total exceeds main beyond tolerance |
| nested double count | the claimed main reproduces `root + sub` structurally |
| null publication | non-identifiable/unknown estimates publish `None`, never a fabricated 0 W |

## 3. What the tests cannot prove

- No real-house accuracy exists: all numbers come from the synthetic household.
- A passing invariant test proves a property did not fire on captured cases; it
  is not a proof of absence for every possible wiring.
- Confidence components are *not* calibrated: no test asserts a component of
  0.9 is right 90 % of the time.

## 4. Running the suite

```bash
.venv/bin/ruff check engine tools tests
.venv/bin/python -m pytest -q -p no:cacheprovider
python tools/dev/render_validation_docs.py --check   # rendered docs are current
python tools/dev/check_release.py                    # release contract is clean
```

`tools/dev/verify.py` is the orchestrator-owned gate (spec status, schema sync,
Z1 no-legacy-symbol check); it rewrites tracked report files and is run by the
orchestrator, not by builders.
