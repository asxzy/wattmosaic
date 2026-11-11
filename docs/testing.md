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
| Energy / leg-split / state-lag | `tests/simulation/test_{energy_integral,leg_split_asymmetric,state_lag}.py` | The metrics and the measurement model on the failure modes that a mean hides |
| Replay tool | `tests/simulation/test_replay_capture.py` | `tools/audit/replay_capture.py` is correct, using a simulator run as truth |
| Real-data gates | `tests/simulation/test_real_topology.py` | The captured installation itself; skips cleanly without a capture |
| Validation tooling | `tests/simulation/test_validation_tools.py` | The two documented reproduction commands actually run |
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
`main` CT with no parent; sub = a feed under another feed, including a nested
`main`), so it works for both the simulator topology and the exported contract
topology. When the topology declares **no** parents at all — the commonest real
shape before the wiring is confirmed — the split is derived from measurements
instead: a main that is contained by another main almost always, while the
reverse holds rarely, is fed by it. That fallback is what keeps
`nested_double_count_records` meaningful on exactly the topology that needs it
most; without it the guard would only ever run on a topology that had already
been fixed.

**Energy is integrated, not averaged.** `ErrorStats` reports two energy numbers
and they are not interchangeable:

| Field | Definition | Exact when |
| --- | --- | --- |
| `energy_error_kwh` | `mean(pred − truth) × duration` | the error is constant over the window |
| `energy_error_kwh_integral` | `trapezoid(pred) − trapezoid(truth)` | always, for a piecewise-linear signal |

The first is kept for continuity; the second is the one to trust on a cycling
load. A 6 kW water heater sampled every two minutes can be scored as *zero*
energy error by the mean formula while being materially wrong on kWh, and
`test_zero_mean_error_can_still_be_wrong_energy` fails if the integral ever
degenerates into the mean. `trapezoid_energy_kwh` is exposed separately so a
test can pin it against hand-computed values on a non-uniform time grid rather
than against "the oracle is near zero" (which a broken zero-returning
implementation also satisfies).

**The leg split is a declared coefficient, not an inference.** A 240 V load
measured by two CTs is summed: the asymmetry between the legs is irrelevant and
the declared `phase_split_l1` must not perturb that sum. A load measured by one
CT is solved as `row = split × P_device`, so the reported total is
`measured_leg / split`; declaring a wrong split is a large systematic error in a
predictable direction. Both behaviours are pinned in
`test_leg_split_asymmetric.py`, including the wrong-split cases, because either
design choice is defensible and the engine must not silently switch.

**State-stream lag is not measurement lag.** `SimulatorConfig.state_lag_s`
delays only the Home Assistant state path (`last_updated` and the reported
value), never the CT samples, which is what a cloud-polled integration looks
like. `test_state_lag.py` asserts the knob is surgical (ground truth and
measurements byte-identical), that transitions are detected `lag / cadence`
ticks later without being back-dated, and that a device whose own CT shows 6 kW
is never reported as 0 W merely because its state entity has not caught up.

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

### 2.6 App gateway and ingress panel

The panel is static files (`addon/ui/`) served by `addon/gateway.py`. Two layers
cover it, neither needing a browser:

- `tests/unit/test_panel.py` drives the real aiohttp app: the shell is returned
  for a trusted ingress peer, direct peers get a JSON descriptor at `/` and
  `401` for `/ui/*`, each asset has the right content type, unknown assets
  `404`, traversal cannot escape the allowlist, and a missing `ui/` directory
  returns `503` instead of a blank page. It also asserts the CSP and the
  `no-store` cache policy, and negative-verifies the static checker below against
  a mutated copy of the panel.
- `tools/dev/check_panel.py` is static: no external origin, no inline script or
  style block, no `on*` attribute, no missing asset, and no
  `eval`/`new Function`/`document.write`. It runs inside
  `scripts/validate_addon.sh`, so the deployment gate fails on a broken shell
  without building the image.

The panel's *visual* behaviour (layout at 390/900/1440 px, no console errors) was
verified against a stub engine with headless Chromium. That check is a
screenshot review, not a committed test, because it needs a browser download.

---

## 3. Real-data replay and regression gates

The simulator proves the engine behaves correctly on a house with the *shape* of
the reference installation. It cannot prove anything about the installation
itself, because the simulated measurements are synthetic. Two things bridge that
gap: a replay tool, and a small set of gates that read the captured database.

### 3.1 `tools/audit/replay_capture.py`

Replays a captured stream through the **production** path —
`Measurement` → `SampleAligner` → `DisaggregationEngine` — and emits a report
shaped like `EvaluationReport`. Input is either the engine's `ct_samples` table
or a `ct_events.jsonl` written by `collect_samples.py`; the database is opened
`mode=ro`.

```bash
# The shipped topology, as stored
.venv/bin/python tools/audit/replay_capture.py --db data/wattmosaic.db --minutes 60

# Same capture, with the panel tree declared (a hypothesis under test)
.venv/bin/python tools/audit/replay_capture.py --minutes 60 --topology-nested

# Offline JSONL capture, machine-readable output
.venv/bin/python tools/audit/replay_capture.py \
    --jsonl data/raw/ct_events.jsonl --topology-nested --out /tmp/replay.json
```

Four design decisions matter when reading its output:

* **It ticks on a cadence, not per sample.** The engine is called once per
  audited CT interval (`--minutes` worth of `15.7 s` ticks), because the aligner
  is a point-in-time view: consecutive samples of one channel add nothing, and
  per-sample inference on a day-long capture takes hours rather than seconds.
  Measured: the nested variant replays 120 minutes (13 681 samples, 459 windows)
  in ~9.5 s.
* **There is no ground truth, so `per_device` and the error strata are empty.**
  The report says why in `notes`. What *is* computable from measurements alone is
  reported: whole-home power, the reconciliation balance, per-device attribution
  and the nested-double-count invariants. Grading the engine against its own
  output would be the failure mode §1 describes.
* **A refused total is missing, not `0 W`.** When the engine declines to state a
  whole-home total for an instant — `main_power.usable is False`, for example an
  undecidable same-leg feed relation — the reconciliation is
  `quality_status=MISSING`. The replay counts that window in
  `n_unavailable_windows` and **excludes it from every mean**; it never appends
  `0.0`. `docs/electrical_model.md` §5 states the rule for a single reading
  ("a missing main is `missing_channels`; it is **not** treated as `0 W`") and it
  applies just as much to an aggregate over a replay. Because the two topology
  variants refuse different instants, the shipped-vs-nested comparison is also
  **paired per shared instant** (`_paired_comparison`), not two means over
  different window sets.
* **Every run has a hard deadline.** `SIGALRM` raises `ReplayTimeout`, and a
  watchdog thread calls `os._exit(124)` if the solver swallows it inside a C
  loop. A wedged solver must not wedge CI. The default deadline is 120 s, and
  the comparison run gets its own 20 s budget.

`tests/simulation/test_replay_capture.py` exercises the tool against a simulator
run, where truth exists: the replay must reproduce the simulated whole-home
power within 5 %. That is the positive control. The real-data gates below can
only show a *difference* between two topologies; this one shows the tool is
right at all, and it runs unconditionally.

### 3.2 Regression gates against the real capture

`tests/simulation/test_real_topology.py` reads `data/wattmosaic.db` and skips
cleanly when it is absent (the file is git-ignored — it identifies a household).
It opens read-only and never writes.

```bash
# With the capture in place
.venv/bin/python -m pytest tests/simulation/test_real_topology.py -q

# From a worktree, pointing at another checkout's capture
WATTMOSAIC_REAL_DB=/path/to/wattmosaic.db \
    .venv/bin/python -m pytest tests/simulation/test_real_topology.py -q

# No capture: the gates skip, nothing else changes
.venv/bin/python -m pytest tests/simulation -q
```

| Test | Asserts |
| --- | --- |
| `test_shipped_topology_declares_nested_parents` | As shipped, all six mains are independent and the panel split is empty; the nested variant is a real tree (sub feeds have a root parent) |
| `test_nested_check_is_not_vacuous` | `nested_check_records > 0` for the nested tree, so `nested_double_count_records == 0` means something |
| `test_parentless_topology_double_count_is_caught` | With every parent stripped, the measurement-derived split still finds the sub feed and still flags "sum every main" |
| `test_nested_replay_terminates_and_balances` | The declared tree terminates, reports positive main power, conserves power, and is not flagged |
| `test_real_replay_reports_whole_home_once` | Shipped and nested replays agree within 5 % on the instants **both** produced a total (paired, E2-1 fixed) |
| `test_real_replay_excludes_refused_totals_from_the_mean` | A window where the engine refuses a total is counted as unavailable and excluded from every mean, never averaged in as `0 W` |
| `test_shipped_replay_terminates` | The shipped topology finishes inside the deadline (E2-2 fixed) |
| `test_shipped_replay_does_not_double_count` | The inverse of the old defect gate: shipped / nested paired ratio is within 5 % |
| `test_the_real_install_has_a_live_direct_measurement_path` | C1: the topology keeps its dedicated channels, each resolves to exactly one device, at least one window produces a usable direct reading, and a majority of capable CTs is read in ≥ 90 % of windows (a stale-capture detector) |

The gates are plain assertions now. Both were `xfail`s naming E2-1/E2-2 while
the engine was broken; they flipped to passes when the fixes landed, and a
future regression fails the test instead of silently XPASSing.

**Two guards added with the C1 decision.** `main_power_w` now forces
`complete=False` and names the channel (`feed_group_unmeasured`) when a feed
inside a same-leg conflict group has no reading — it used to be dropped from the
group silently, publishing part of the service as the whole-home total. And a
0 W direct count carries a verdict (`direct_availability`), so "every circuit is
a branch" (legitimate, ADR-0026) is distinguishable from "the Level-1 path is
dead" (a defect). Both are negative-verified: reverting the engine change makes
the new unit tests fail on exactly the silent behaviour described.

**What was fixed, and why the gate is not a simulator test.** The shipped
topology declares the load manager, the main panel and the sub panel as six
parallel mains. Physically there is one service feed, one main panel and one sub
panel, so naively summing them counts most of the house two to three times;
measuring that on the 120-minute window gave `main_power_w` **3943 W** as
shipped against **1600 W** with the tree declared — ratio **2.46** — plus a
non-terminating solve.

The engine cannot re-parent a stored topology, so the fixes are behavioural:

* `electrical/main_power.py` groups the six feeds into measurement nodes, finds
  that the load-manager pair measures the same two legs as the main-panel pair
  and the sub-panel pair, and resolves which is outermost **from the
  measurements**. The excluded feeds and the reason are named in
  `MainPower.notes` (`undeclared_feed_relation_resolved_by_measurement`); it
  refuses (`parallel_mains_without_declared_relation`) only when containment
  cannot be decided.
* `electrical/solver.py` preconditions duplicate rows and bounds the iteration
  count, so the ill-conditioned system converges instead of hanging.

Measured on the shipped gate window (`REPLAY_MINUTES=120`, `REPLAY_TICK_S=15.7`,
459 windows / 13 681 samples):

```text
as-shipped   main 1568.9 W   accounted 1033.8 W   residual 535.2 W   11.5 s
             24/459 windows refused (no whole-home total; excluded from the mean)
nested       main 1599.1 W   accounted 1082.9 W   residual 516.2 W    9.9 s
paired on 435 shared instants: shipped 1568.9 W vs nested 1555.0 W   ratio 1.009
```

The window is 120 minutes, not 5, because the defect this gate now also covers
is *invisible* on a five-minute tail. The shipped topology's six independent
mains are usually separable by measurement — the load-manager pair contains the
others — but on 24 of 459 instants the three panel feeds sit inside the 30 W /
2 % containment tolerance of each other and `main_power_w` correctly refuses
(`parallel_mains_without_declared_relation`, `quality_status=MISSING`). The
replay harness used to append `rec.main_power_w` (i.e. `0.0`) for those windows
and average them in, which pulled the shipped mean **7 %** below the nested one
and failed the ratio gate for a reason that had nothing to do with double
counting: the engine was right and the aggregation invented a collapse to no
load. `docs/electrical_model.md` §5 already forbids exactly that ("a missing
main is `missing_channels`; it is **not** treated as `0 W`"), so the fix is in
the harness: refused windows are counted (`n_unavailable_windows`) and excluded
from every mean, and the two variants are compared **per shared instant** rather
than as two means over different window sets.

Neither number can be produced by the simulator, whose topology always declares
its tree. The failing mode that used to be invisible — a hung solve — is still
guarded by the fixture's hard deadline, so a regression fails in 60 s rather
than hanging CI.

### 3.3 The two documented validation commands

`docs/real_ha_validation.md` advertises an offline reproduction path. Both
halves are now real, and `tests/simulation/test_validation_tools.py` compares
their output byte-for-byte against the committed artifacts:

```bash
.venv/bin/python tools/dev/make_reference_house.py --passive-validation
.venv/bin/python tools/audit/passive_validation.py \
    --from data/examples/reference_house/passive_validation.json
```

`--passive-validation` runs the real passive-analysis code over a deterministic
simulator capture and writes
`data/examples/reference_house/passive_validation.json`. `--from` renders the
report from such a file with no network, no Home Assistant and no credentials;
it accepts either the shape that tool writes (`states` + computed `result`) or a
raw `analyse()` dump (`series`), recomputing the statistics when they are
missing. A file that is not a capture is rejected with a non-zero exit rather
than rendered as an empty table.

---

## 4. Running the tests

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

### 4.1 Why some tests can be skipped

Skips are only allowed when a component genuinely does not exist. The engine
adapter tests (`test_metrics.py`, `test_accuracy.py`) skip with the reason
`real engine unavailable: <status>` if the engine cannot be imported. Every
end-to-end test — matrix, database, simulator, pipeline — is unconditional: the
components exist, so skipping would hide a failure, and none are skipped today.

The one new skip class is the real-data gates in `test_real_topology.py`. They
skip only when `data/wattmosaic.db` is absent, and the reason string says how to
produce one or how to point `WATTMOSAIC_REAL_DB` at an existing capture. That is
a genuine absence (the file is deliberately git-ignored), not a hidden failure:
everything the gates assert about the *harness* — the panel split, the vacuity
check, the measurement-derived fallback — is also covered unconditionally in
`test_replay_capture.py` and `test_metrics.py`.

---

## 5. Reproducibility

The simulator is a pure function of its configuration. `SimulatorConfig(seed=S,
minutes=M, interval=I, quality=Q)` always produces identical bytes; the CLI
writes the SHA-256 of each output file into `manifest.json`.

To reproduce the pinned accuracy numbers: seed 202, 60 simulated minutes,
15 s records, quality `good`, audited topology.

A real replay is *not* bit-reproducible in the same sense — it reads a capture —
but it is deterministic given one: the same database, window and `--topology-*`
flag produce the same report. The tool prints `elapsed_s`, which is the only
output that varies between runs, and it is reported separately from the watts.

---

## 6. What this suite does *not* prove

Being explicit matters more than being reassuring:

* **Not real-house accuracy.** The simulator mirrors the audited topology,
  cadence and impairment classes, but the device power profiles are synthetic.
  Results show the engine behaves correctly on a house with these
  characteristics; they do not measure accuracy on the physical installation.
  The replay tool reports no per-device error at all, for the same reason: there
  is no ground truth for a real house, and grading the engine against its own
  output would prove nothing.
* **The real-data gates prove a difference, not an absolute.** `test_real_topology`
  can show that the shipped topology disagrees with the nested one and that the
  shipped one does not terminate. It cannot say which wattage was physically
  correct — only that the two cannot both be.
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
