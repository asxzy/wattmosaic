# Testing Strategy

Owner: Agent I (Test & QA).
Code: `tools/simulator/`, `tests/simulation/`, `tests/end_to_end/`,
`tests/conftest.py`.

This document describes what the test layers prove, how to run them, and — most
importantly — what they *cannot* prove. Measured results live in
[`docs/acceptance_report.md`](acceptance_report.md); this file is about method.

---

## 1. The problem the tests have to solve

WattMosaic infers per-device power from a small number of circuit-level CTs in a
real house. Accuracy claims are easy to fake: you can always produce numbers
that look good if you grade yourself against your own output, or if you report
one aggregate "score" that hides the devices the system cannot see.

The QA deliverables therefore have three jobs:

1. **Generate truth that is not derived from the measurements.** A simulator
   whose ground truth came from the same pipeline it scores would prove nothing.
2. **Grade against truth, stratified.** Accuracy is reported per source tier,
   per device type, per concurrency level, per data-quality level and per
   individual device. There is deliberately no single aggregate number.
3. **Check safety invariants separately from accuracy.** "Unknown load stays
   unknown" and "no device is credited with power it did not draw" are
   correctness properties, not error statistics, and are asserted directly.

---

## 2. Layers

| Layer | Location | What it proves |
| --- | --- | --- |
| Simulator unit | `tests/simulation/test_simulator.py` | The synthetic house, its impairments, determinism and CLI contract |
| Harness unit | `tests/simulation/test_metrics.py` | The grading code itself is correct, and known-bad predictors are caught |
| Accuracy regression | `tests/simulation/test_accuracy.py` | The real engine's measured accuracy, pinned so regressions fail |
| Storage E2E | `tests/end_to_end/test_storage_e2e.py` | Schema, migrations, persistence, restart recovery, retention |
| API E2E | `tests/end_to_end/test_api_e2e.py` | A real HTTP server, real engine facade, real SQLite, real simulator |
| Pipeline E2E | `tests/end_to_end/test_pipeline_e2e.py` | Aligner → quality → disaggregation → learning-sample rejection |

### 2.1 Simulator (`tools/simulator/`)

A deterministic synthetic household that reproduces the *shape* of a real
multi-panel CT install. It describes no real building; the fixture is generated
by `tools/dev/make_reference_house.py` into
`data/examples/reference_house/` and rendered as `docs/ha_environment_audit.md`
and `docs/ct_update_profile.md`:

* Two energy-monitor panels, **18 channels each**, synthetic entity IDs
  (`sensor.panel_a_ct1_power`, `sensor.panel_b_water_heater_ct1_power`, …).
* The sub panel is **nested inside** the main panel. Summing both panels' mains
  double counts the sub-feed — a real hazard the simulator reproduces so the
  tests can detect it.
* Double-pole 240 V loads (water heater, dishwasher, deck heater, upper plug)
  are split across two leg CTs, each carrying half the device power.
* **Median CT cadence 15.7 s**, measured from the synthetic profile in
  `data/examples/reference_house/ct_update_profile.json`.
* All required device classes: binary, standby, dimmable, variable_speed, HVAC,
  refrigerator, washing machine, dishwasher, dryer, double-pole, and unmetered
  ("unknown") loads.

**Ground truth is independent of the measurements.** Device power profiles are
evaluated from their own seeded RNG and a simulation clock. The measurement
pipeline then *impairs* channel values; the truth fields are never read back
from the sensor path. `test_ground_truth_is_independent_of_measurements`
asserts this by comparing runs at different quality presets: truth is
byte-identical, measurements are not.

**Impairments** are applied as an ordered post-truth pipeline
(`tools/simulator/sensors.py`): gain/drift/polarity → noise → quantisation →
delivery delay → staleness/repeats → dropouts/outages. Each stage is
individually testable and individually inspectable in the output.

**Three timestamps per measurement**, as required:

| Field | Meaning |
| --- | --- |
| `state_timestamp` | when Home Assistant says the state changed |
| `received_at` | when the engine/collector received it |
| `physical_timestamp` | optional, when the underlying physical sample was taken |

Delivery is modelled honestly: a sample is only visible once
`received_at <= tick end`, and late samples carry into the next tick rather
than being silently dropped. Records are **as-of tick-end snapshots**:
`timestamp` is `t_next`, ground truth is evaluated at `t_next`, and the
measurements are those received in `(t, t_next]`.

**CLI** (`python -m tools.simulator.cli`):

```bash
python -m tools.simulator.cli --seed 202 --minutes 60 --interval 15 --out /tmp/sim
python -m tools.simulator.cli --seed 202 --minutes 10 --interval 15 --format json
python -m tools.simulator.cli --print-topology
```

It writes `household.jsonl` (or `household.json`), `samples.jsonl` (one
Measurement-shaped row per observation) and `manifest.json` (config, counts and
SHA-256 hashes). Flags exist for every impairment (`--quality`, `--invert-ct`,
`--drift-ct`, `--outage`, `--no-unknown-loads`), so impaired datasets can be
generated from the shell without writing Python.

**Python API**:

```python
from tools.simulator import HouseholdScenario, SimulatorConfig

scenario = HouseholdScenario(SimulatorConfig(seed=202, minutes=60, interval=15))
for record in scenario.records():
    record["ground_truth"]["devices"]      # true device power
    record["latest_measurements"]          # as-of view the engine sees
    record["measurements"]                 # samples delivered this tick
```

### 2.2 Evaluation harness (`tests/simulation/metrics.py`)

`evaluate(records, predictor, panel_channels=...)` runs any callable
`predictor(record) -> {device_id: {power_w, source, confidence}}` over the
record stream and returns an `EvaluationReport` containing:

* `per_device`, `per_device_detection`
* `per_source`, `per_source_detection` (`direct_measured` / `inferred` /
  `model_estimated` / `unknown`)
* `per_device_type`
* `per_concurrency`
* `per_quality` (worst supporting channel per device)
* `reconciliation` (residual preservation, forced attribution, over-attribution)
* `coverage` (attribution coverage, unknown-load share, false-attribution rate)
* `invariants` (see below)

Nothing in the report is a single scalar. `test_no_single_aggregate_score_is_exposed`
fails if an `accuracy` / `score` / `overall` key ever appears at the top level.

**Scoring instant.** Predictions are compared with ground truth at the *record
timestamp* (`t_next`), not at the sample's `state_timestamp`. This is the
conservative choice: a reading just delivered describes an instant up to one CT
cadence earlier, and the engine is not credited for information that had not
arrived. It also makes the oracle self-check exact.

**Safety invariants**, and why each is defined the way it is:

| Invariant | Definition | Rationale |
| --- | --- | --- |
| `residual_preserved_ratio` | fraction of records with `residual >= unmetered_truth − tolerance` | Under-attributing leaves a *larger* residual, which is legitimate; over-attributing shrinks it below truth and is not |
| `forced_attribution_count` | records where **inferred/model** power is claimed for a device genuinely OFF at that instant | The only unambiguous fabrication test; direct CT readings of a device that just switched are not fabrication |
| `forced_attribution_from_stale_input_records` | as above, but all of the device's supporting CTs were `repeated`/`stale`/`missing`/`bad` | An input-corruption fault, reported on the quality axis so it is not confused with model fabrication |
| `negative_residual_records` | records with `accounted > main` | Usually a sample-vintage artefact, not fabrication; counted separately |
| `over_attribution_count` | attributable total exceeds main by >5% + 10 W | Magnitude-based fabrication check |
| `headroom_violation_records` | inferred power exceeds the predictor's own `main − direct` | Soft signal: can legitimately break when samples are stale |
| `direct_exceeds_main_records` | direct readings exceed physical whole-home draw | Sample-vintage skew indicator |
| `nested_double_count_records` | only when `panel_channels` is supplied and the claimed main reproduces `root + sub` | A main that is merely noisy is not flagged; only one that is *structurally* the nested sum |
| `nested_check_records` | how many records were actually checkable | Makes a zero meaningful rather than vacuous |
| `stale_learning_violations` | accepted learning samples whose evidence was stale | Learning must not train on expired data |

`panel_channel_map(topology)` derives root vs sub feeds structurally (root =
`main` CT with no parent; sub = non-main intermediate node with children), so
it works for both the simulator topology and the exported contract topology.

**Self-check predictors.** The harness ships predictors whose expected
behaviour is known in advance, and the unit tests assert the harness catches
them:

| Predictor | Expected |
| --- | --- |
| `oracle_predictor` | Perfect: MAE 0, coverage 1.0, every invariant clean |
| `naive_sum_wrong_predictor` | Claims every device at full main power: coverage ≫ 1, high false attribution, over-attribution on every record |
| `flat_zero_predictor` | Coverage 0.0, no fabrication |
| `main_channel_only_predictor` | Honest but blind: coverage ≈ engine, reconciliation MAE ≈ whole-home draw |

If the oracle ever fails, the harness is broken — not the engine. That check
guards every other number in the report.

### 2.3 Storage end-to-end (`tests/end_to_end/test_storage_e2e.py`)

Runs against the real `Database` and real SQLite, not mocks:

* schema version and `PRAGMA integrity_check`
* migrations idempotent on re-open, and a simulated v1 database upgrading in place
* topology round-trip (36 channels / 37 attributable devices) with per-field
  equality, and topology versioning
* all three timestamps persisted and filterable by `ct_samples_between`
* inference and reconciliation rows persist and read back
* WAL mode survives process-style close/reopen
* energy aggregation persists buckets
* restart recovery reproduces the same energy total, idempotently across three
  replays
* retention pruning removes old raw rows only (never aggregates or topology)

### 2.4 API end-to-end (`tests/end_to_end/test_api_e2e.py`)

Starts a **real `aiohttp` server** on an ephemeral loopback port, wired to the
real `EngineCore` facade, real SQLite and the real backend, then drives it with
simulated data through aligner → engine → SQLite → HTTP. It checks health and
contract headers, auth (401), 404 for unknown devices, Prometheus text metrics,
topology PUT round-trip and validation, that inference results for dedicated
devices reach the API, that the residual is never redistributed, and that
energy aggregation works over a full inference history.

Two tests are **strict xfails** for engine defects found by this suite (see
`docs/acceptance_report.md` §6). Strict means: once the engine is fixed, the
tests fail, so the marker cannot be forgotten.

### 2.5 Pipeline end-to-end (`tests/end_to_end/test_pipeline_e2e.py`)

Exercises the data path between simulator and engine with production contract
objects: simulator record → `Measurement` → `SampleAligner` → `AlignedWindow`
→ `DataQualityEngine` / `DisaggregationEngine` / `SampleFilter`.

It verifies that repeated samples are not treated as fresh, stale samples are
marked stale, quality scoring penalises staleness, the disaggregation engine
runs on a real aligned window without double counting nested panels, and that
low-quality learning samples are rejected before they can train a model.

---

## 3. Running the tests

```bash
# The QA deliverable, as required by the acceptance criteria
.venv/bin/python -m pytest tests/simulation tests/end_to_end -q

# One layer
.venv/bin/python -m pytest tests/simulation -q
.venv/bin/python -m pytest tests/end_to_end/test_storage_e2e.py -q
.venv/bin/python -m pytest tests/end_to_end/test_api_e2e.py -q
.venv/bin/python -m pytest tests/end_to_end/test_pipeline_e2e.py -q

# Show measured numbers while running
.venv/bin/python -m pytest tests/simulation/test_accuracy.py -q -s

# Whole repository (includes other agents' unit/integration suites)
.venv/bin/python -m pytest -q
```

The suite needs no network access, no Home Assistant instance and no
credentials. The API tests bind to `127.0.0.1` on an OS-assigned port.

### 3.1 Why some tests can be skipped

Skips are only allowed when a component genuinely does not exist. The engine
adapter tests (`test_metrics.py`, `test_accuracy.py`) skip with the reason
`real engine unavailable: <status>` if the engine cannot be imported. Every
end-to-end test — matrix, database, simulator, pipeline — is unconditional: the
components exist, so skipping would hide a failure, and none are skipped today.

---

## 4. Reproducibility

The simulator is a pure function of its configuration. `SimulatorConfig(seed=S,
minutes=M, interval=I, quality=Q)` always produces identical bytes; the CLI
writes the SHA-256 of each output file into `manifest.json`.

To reproduce the pinned accuracy numbers: seed 202, 60 simulated minutes,
15 s records, quality `good`, audited topology.

---

## 5. What this suite does *not* prove

Being explicit matters more than being reassuring:

* **Not real-house accuracy.** The simulator mirrors the audited topology,
  cadence and impairment classes, but the device power profiles are synthetic.
  Results show the engine behaves correctly on a house with these
  characteristics; they do not measure accuracy on the physical installation.
* **No hardware, transport or Home Assistant runtime coverage.** The
  WebSocket/reconnect path, the HA event loop and the add-on container are
  other agents' scope (`tests/integration/test_ha_integration.py`).
* **No long-horizon drift.** Runs are minutes, not months; seasonal effects and
  model retraining over weeks are not exercised.
* **The 0.79 coverage ceiling is real.** Shared-branch devices cannot be
  separated by this hardware, and the engine correctly declines to invent them.
  Higher coverage would require more CTs, not a better algorithm. See the
  acceptance report for the exact list.
* **A passing test proves the assertion held on the measured run**, not that
  the underlying thresholds are physically optimal. Thresholds are documented
  where they are pinned.
