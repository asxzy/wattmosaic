# Load Disaggregation Algorithms

Owner: Agent E.
Code: `engine/wattmosaic_engine/disaggregation/`.
Companion: `docs/electrical_model.md` (topology, matrix, solver).
Spec: `WATTMOSAIC_MASTER_INSTRUCTIONS.md` §9, §12, §19.3.

All algorithms here are deterministic, explainable and classical
(non-negative least squares, robust statistics). No deep learning. Every output
carries an `explanation` list so a human can audit *why*.

---

## 1. Pipeline

```text
window ──► topology validation ──► Level 1 direct ──► measurement matrix
                                              │
                                              ▼
                     event detection ◄── observed cadence
                              │
                              ▼
        bounded NNLS + unknown-load slack ──► SVD identifiability
                              │
                              ▼
                 ambiguity gate ──► per-device hierarchy
                              │
                              ▼
        reconciliation (main − accounted, never redistributed)
```

Nothing is mutated between calls: `infer()` is a pure function of
`(window, device_states, models, topology)` with constructor-level
configuration. This makes every result reproducible.

---

## 2. Measurement hierarchy (spec §9.1)

Applied per device, in strict order. A lower level can never overwrite a higher
one.

| Level | Source | Condition |
| --- | --- | --- |
| 1 | `direct_measured` | verified dedicated CT, fresh, usable |
| 2 | `inferred` | device is identifiable **and** its rows are fresh |
| 3 | `model_estimated` | a validated model gives a value for the current state |
| 4 | `unknown` | none of the above, or ambiguity |

A `branch` CT is never Level 1, however alone the device is on the circuit
(ADR-0026): the branch may carry unmodelled load. Level 1 requires a
`DEDICATED_DEVICE` channel or a declared `dedicated_ct_ids` entry.

Because of that gate a batch can legitimately contain **zero** direct readings --
on a branch-only install nothing is capable of one. `InferenceBatch.diagnostics`
therefore carries a verdict (`direct_availability`:
`measured` / `explained_unavailable` / `no_dedicated_ct` /
`unexplained_unavailable`, with `direct_capable_count` and
`unexplained_direct_devices`) so "0 W because every circuit is a branch" is
distinguishable from "the direct path produced nothing". Only
`unexplained_unavailable` is a defect. See `docs/electrical_model.md` §7.

Guarantees:

* A direct measurement is never replaced by a model estimate (tested with a
  model that claims 900 W against a 42 W CT).
* `unknown` results carry `power_w = 0.0` as a **placeholder only**; the real
  energy is in `Reconciliation.unknown_residual_w`. Provenance says so
  explicitly (`"power_w_is_placeholder": true`).
* The sum of attributed powers is never forced to equal main power.

---

## 3. Event detection

### 3.1 Why not 2 seconds

Documented behaviour for polling multi-channel CT integrations is a **median CT
state-update interval of 15.7 s**, not 2 s; `two_second_assumption_supported =
false`, with wide per-channel spread (some channels faster than 1 s, some slower
than 30 s). A fixed 2 s window would contain at most one sample and could not
form a baseline.

### 3.2 Windows from observed cadence

```text
cadence  = median over channels of (median of that channel's sample intervals)
           clamped to [0.25 s, 120 s]
before   = clamp(2·cadence, 5 s, 60 s)
after    = clamp(1·cadence, 2.5 s, 30 s)
```

Fallback when a window carries no timing at all: `15.7 s` (the documented
typical value), never `2 s`.

### 3.3 Change statistic (MAD-robust)

For each candidate change time `τ` in the channel history:

```text
baseline = median{ v(t) : t ∈ [τ−before, τ) }
after    = median{ v(t) : t ∈ [τ, τ+after] }
delta    = after − baseline
noise    = 1.4826 · median{ |v − baseline| }          (MAD → σ)
threshold = max( min_delta_w, k_mad · max(noise, noise_floor), resolution )

detected  = |delta| ≥ threshold
```

* `1.4826` makes the MAD an unbiased σ estimator for Gaussian noise.
* `k_mad = 4` by default (a 4σ step).
* `min_delta_w = 5 W` prevents flagging resolution jitter.
* At least **4** pre-samples and 1 post-sample are required. Two samples give
  `MAD = 0` for a perfectly flat channel, so the threshold collapses to
  `min_delta_w` and a single resolution tick reads as an event. When the
  cadence-derived window is too sparse to hold four samples, the baseline
  reaches back to `4 · cadence`, still strictly before the candidate.
* `noise_floor_w` defaults to `None` → the historical **1 W** floor. The quality
  layer may supply a per-channel measured MAD instead (`noise_floor_w={"ct_bidet":
  8.0}` or a per-channel value in `detect_step`), which raises the threshold to
  `k_mad · floor`. `min_delta_w` and the channel resolution stay hard floors, so
  a floor of `0` never lowers the threshold below them.
* The split with the largest `|delta| / threshold` wins (latest on ties).

#### Anchored detection (HA transition time)

Each HA state transition carries the real instant it occurred (`occurred_at`,
`_Transition.at`). `detect_step` and `detect_changes` accept it as `anchor`:

```text
candidate splits ∈ [anchor − anchor_before_s − anchor_grace_s,
                    anchor + anchor_after_s + anchor_grace_s]
score = |delta| / threshold · exp(−|τ − anchor| / cadence)
```

* The window is a hard restriction. If no candidate split falls inside it the
  result is `None` (the channel is omitted by `detect_changes`) — there is
  **no** fallback to the global scan and **no** `previous_snapshot` fallback,
  because both ignore *when* the step happened and are what allowed a lamp
  switching on to claim a CT step 47 s away.
* Proximity is a multiplicative penalty, so the nearest sufficiently large step
  beats a far larger distant one. A step needs `|delta| ≥ threshold` to be
  `detected=True` regardless of proximity.
* `anchor=None` (default) is the unanchored scan, unchanged.
* Validation: negative or non-finite windows raise `ValueError`; mixing a naive
  and an aware `anchor`/`at` raises `ValueError`. A consistently naive pair is
  normalised to aware UTC, matching the rest of the engine.

Detected steps appear in `diagnostics["ct_steps"]`; a two-point fallback using
the previous inference cycle is marked `source="previous_snapshot"` and is used
only when it exceeds the threshold and no anchor window applies.

##### How `infer` wires the anchor

The unanchored scan answers "did this channel move somewhere in the buffer",
which is not the same question as "did it move when this device switched". On a
real capture the two differ by up to a minute (15.7 s CT cadence, 5 s engine
cadence). So after the unanchored scan:

1. Only transitions that HA timestamped itself are eligible. `_Transition`
   records `anchored=True` for these; a transition derived from the previous
   cycle's snapshot carries `window.at` — the time we *looked* — and anchors
   nothing, because that is not when the device switched.
2. For each anchored transition, the channels that observe that device
   (`graph.device_channels`) are rescanned with
   `anchor=occurred_at`, `anchor_before_s = anchor_after_s = after_s`,
   `anchor_grace_s = cadence_s`, `cadence_s = cadence_s`.
3. A channel that produced a step in the anchored rescan takes that step.
4. A channel whose unanchored step sits outside
   `[anchor − after_s − cadence, anchor + after_s + cadence]` loses it, and the
   loss is reported in `diagnostics["anchored_steps_dropped"]` plus the warning
   `ct_step_outside_transition_anchor`. The movement belongs to some other load,
   so citing it would credit one device with another circuit's power.
5. A `ValueError` from a naive/aware mismatch is swallowed per device: the data
   cannot be anchored, so the unanchored scan stands rather than aborting
   inference. The device is listed in `diagnostics["anchor_skipped_devices"]`.

A channel shared by several devices survives if *any* of its anchored devices'
windows contains the step.

##### Unexplained residual with no transition

Ambiguity requires at least two claimants, so `_detect_ambiguity` returns
nothing when fewer than two devices changed state. That is not the same as
"solved": a residual that is large while nothing switched is an event the state
layer never saw (an always-on load cycling, a device with no state entity, a
manual switch). `_reconcile` records it in
`diagnostics["unexplained_residual_without_transition"]` with the residual and
main power, and leaves the power in `unknown_residual_w`. It is a diagnostic,
never an ambiguity and never a fabricated claimant.

### 3.5 Supplying history in production (`StepTracker`)

The production synchroniser (`synchronization.AlignedWindow`) intentionally
exposes only a point-in-time snapshot: no per-channel history, no cadence, no
previous snapshot. Without a buffer, step detection would never run outside
tests.

`StepTracker` is a bounded (default 64 samples/channel) observation buffer. Pass
it once:

```python
engine = DisaggregationEngine(tracker=StepTracker())
```

The engine is called every ~5 s while CTs publish a new state every ~15.7 s, so
a naive buffer is a staircase of duplicated readings. When a new value equals
the last stored value the tracker refreshes that point's timestamp instead of
appending, and `cadence_s()` therefore measures the median interval between
*changes* — the CT's real update cadence — rather than the inference cycle.
`max_samples` bounds distinct values, so the retained horizon is longer.

Each `infer()` call

1. fills missing `history` and `cadence_s` from the tracker,
2. runs detection against that padded-out history,
3. records the current readings **after** detection, so a cycle is never used
   to explain itself.

The tracker never rewrites a *different* past observation and appends at most one
point per channel per value change, so a given call sequence produces identical
results. A repeated reading refreshes the last point's timestamp instead of
being re-recorded as a new physical sample.

### 3.4 Device events

A device's event is the *most specific* CT step among the CTs that observe it:
dedicated CT first, then branch, then main. A direction conflict between the HA
transition and the CT step is visible in `explanation`
(`branch_power_increase` / `branch_power_decrease` only added when they agree).

---

## 4. Identifiability and ambiguity

### 4.1 Null space

SVD of the weighted system `Aw = U S Vᵀ`. With rank `r`:

```text
N = V[:, r:]                  null-space basis (n × (n−r))
null_share_j = ‖N[j, :]‖₂²    fraction of device j's variance that is free
```

Device `j` is uniquely identifiable iff `null_share_j ≤ 1e-8`. Two devices that
share a null direction form an **ambiguous group**.

### 4.2 The ambiguity gate

Two independent triggers, both required to be genuine (not fabricated):

1. **Structural**: ≥ 2 changed devices fall in the same ambiguous group.
2. **Unresolvable change**: a changed device is unidentifiable for any reason.

When triggered:

```json
"diagnostics": {
  "ambiguity": {
    "ambiguous": true,
    "explanation": "ambiguous_concurrent_events",
    "groups": [["d0","d1"]],
    "reason": "concurrent state changes cannot be separated ..."
  }
}
```

* Affected devices → `unknown`, `power_w = 0`, confidence ≤ 0.25,
  `explanation += ["ambiguous_concurrent_events"]`.
* Their power stays in `Reconciliation.unknown_residual_w`.
* Models do **not** override this: if the measurements cannot say which device
  moved, a model coincidence is not evidence (tested).

### 4.3 Other sources of unidentifiability → Level 3/4

If a device is unidentifiable but no concurrent event happened, it is
`model_estimated` when a validated model exists, otherwise `unknown`. The
explanation distinguishes `measurements_do_not_identify_device` from
`ambiguous_concurrent_events`.

### 4.4 Worked example

Shared branch, both devices ON, 500 W measured:

```text
A = [ 1  1 ]   (branch_shared)
x = [ x_d0, x_d1 ] ≥ 0

rank = 1, n = 2 -> null space dimension 1, N = [0.707, -0.707]
null_share = [0.5, 0.5]  -> both unidentifiable
```

Everything in `{(t, 500−t) : 0 ≤ t ≤ 500}` fits equally well. Correct output:
`unknown` for both, 500 W residual. Incorrect output: `250/250`.

With one device explicitly OFF (upper bound 15 W):

```text
x_d1 ≤ 15  ->  x_d0 = 500 − x_d1 ∈ [485, 500]
```

→ `d0` is now identifiable and reported as `inferred ≈ 490 W`. A state
constraint is what creates information here, not the solver.

Only *positive* OFF states (`off`, `idle`, `standby`, `closed`, `paused`,
`none`) pin a device. `unavailable` and `unknown` mean "we do not know" and are
**never** treated as OFF — doing so would silently force a live device to zero
and hand its power to its neighbour.

---

## 5. Models and bounds (Level 3 support)

`electrical/models.py` adapts Agent F's `ModelRegistry` without importing it
(duck typing: `active()`, `estimate()`, or a plain mapping).

* Expected power for a state: `state_map` → `curve` interpolation → `on/off/
  standby` parameters.
* **Upper bound** for the solver `u_j`:
  `1.35 × max(on_power, stage_mean + 3σ, curve_max)`, capped at `30 kW`.
  1.35 allows a genuine overload/start-up surge; the absolute cap stops an
  unconstrained device absorbing unknown load.
* **Model quality** `q` (0–1): explicit `quality`/`r2` metric, else
  `1 − mae/mean_power`, else from residual spread. Blended 65/35 with model
  self-confidence.
* A bound is **never** allowed to cap a measurement: for a device alone in its
  attribution row no bound is applied, and an active bound that worsens the fit
  by >2% triggers a relax-and-refit (`model_upper_bound_relaxed`).

---

## 6. Confidence scoring

An **engineering score in [0,1]**, explicitly not a probability.
Every result sets `confidence_is_calibrated = false`.

```text
confidence = base_source × Σ w_k · component_k × Π penalties
```

| Component | Weight | Value |
| --- | --- | --- |
| measurement_quality | 0.30 | quality score × age decay |
| ct_support | 0.15 | `min(1, supporting_CTs / 2)` |
| model_quality | 0.20 | model validation quality |
| event_isolation | 0.15 | see below |
| topology_certainty | 0.10 | confirmed 1.0 / candidate 0.6 / rejected 0.2 |
| residual_quality | 0.10 | `1 / (1 + (|r| / 4σ)²)` |

Source ceilings (`base_source`):

| Source | Base |
| --- | --- |
| `direct_measured` | 0.97 |
| `inferred` | 0.88 |
| `model_estimated` | 0.60 |
| `unknown` | 0.15 |

`event_isolation`: isolated own event 1.0, own event with concurrency 0.45,
no event 0.60, ambiguous 0.15.

Age decay: 1.0 up to 30 s (soft), linear to 0 at 180 s (hard).

Penalties (multiplicative): stale ×0.55, missing rows ×0.50,
ambiguity ×0.50, repeated-but-not-fresh ×0.90.

Floors: a healthy direct measurement never drops below 0.55 (0.30 if stale); an
`unknown` result is capped at 0.25. The breakdown is emitted in
`provenance["confidence"]` so a user can see *why* the score is what it is.

Worked example — inferred device, good CTs aged 2 s, 2 supporting CTs, model
quality 0.8, isolated event, confirmed topology, small residual:

```text
components = 1.00, 1.00, 0.80, 1.00, 1.00, 0.99
blend      = .30+.15+.16+.15+.10+.099 = 0.959
confidence = 0.88 × 0.959 = 0.844
```

---

## 7. Reconciliation

```text
direct_measured_w = Σ power of direct_measured results
inferred_w        = Σ power of inferred results
model_estimated_w = Σ power of model_estimated results
accounted_w       = direct + inferred + model

main_power_w      = Σ effective_feeds()             (sign preserved)
unknown_residual  = main_power_w − accounted_w     (signed, never clamped)
accounted_fraction = clamp(accounted / main, 0, 1)

reconciliation_error_w = Σ_rows | y_row − (A x)_row |
reconciliation_error_ratio = error / |main|
measurement_uncertainty_w  = √(Σ_rows σ²)
```

* `reconciliation_error` is a **fit** diagnostic: how well the CT rows are
  explained. `unknown_residual` is the **power balance**. They are different
  numbers and both are reported.
* The residual is **never** distributed among devices. `unknown` devices keep
  `power_w = 0`; the residual exists only in `Reconciliation`.
* When mains are nested, `main_power_w` sums only the outermost feed of each
  branch of the tree, so a sub-panel feed is never added on top of the feed that
  already contains it. `MainPower.notes` names every excluded feed
  (`nested_feeds_not_summed: ...`) and every duplicate feed counted once
  (`duplicate_feeds_counted_once: ...`); the residual is computed from that
  resolved total rather than being abandoned.

### 7.1 Worked example

```text
main_l1 = 2 000 W, main_l2 = 0
devices: lights 700 (inferred), fridge 200 (inferred), washer 0, tv 0
=> direct 0, inferred 900, model 0, accounted 900
   unknown_residual = 2 000 − 900 = 1 100 W
   accounted_fraction = 0.45
```

A system that "balances" by adding 275 W to each device is wrong; this one
reports 1 100 W of unknown load.

---

## 8. Data quality handling

| Situation | Behaviour |
| --- | --- |
| Missing channel | Row excluded (`missing_measurement`); never zero-filled |
| Missing nested child | Parent row excluded (`nested_child_missing:...`) |
| Age > `max_age_s` (120 s default) | Row excluded (`stale_measurement`); device falls back to model/unknown, quality `STALE` |
| Age > 30 s | Used, quality `DEGRADED`, confidence reduced |
| `QualityStatus.BAD`/`MISSING` | Excluded |
| Repeated value | Still used (best known value) but weight ×0.5, confidence ×0.9, explanation `repeated_state_update` |
| Partial double-pole pair | Sum of available legs + `partial_leg_measurement`, confidence reduced |
| Negative reading | Preserved; only device *consumption* is clipped at 0 by NNLS |
| Bad model object | Isolated per device; batch continues |

Per-device error isolation: each device's inference runs inside `try/except`.
A failure produces an `unknown` result with quality `BAD`, explanation
`device_inference_error`, and the error text in `diagnostics["errors"]`. The
batch never aborts.

---

## 9. Explanation vocabulary

| Explanation | Meaning |
| --- | --- |
| `direct_ct_measurement` | verified dedicated CT |
| `constrained_nnls_fit` | solved from CT rows |
| `ha_state_transition_activation` / `..._deactivation` | HA state change in window |
| `branch_power_increase` / `..._decrease` | CT step agrees with the transition |
| `historical_model_match` | model and measurement agree within tolerance |
| `measurement_disagrees_with_model` | both exist, they differ |
| `measurements_do_not_identify_device` | structural unidentifiability |
| `ambiguous_concurrent_events` | concurrency cannot be separated |
| `unexplained_load_on_device_circuit` | device OFF but circuit carries load |
| `stale_measurement` / `direct_ct_unusable` | data quality |
| `repeated_state_update` | value unchanged from previous sample |
| `power_left_unattributed` | placeholder zero; real power is in the residual |
| `device_inference_error` | isolated per-device failure |
| `residual_never_redistributed` | reconciliation guarantee marker |
| `negative_power_export_preserved` | negative main power kept |
| `main_measurement_incomplete` | a main CT is missing |

---

## 10. Complexity and determinism

* Matrix build `O(m·n)`; solve `O(m·n²)` for `nnls` / trust-region iterations.
  Typical house: `m ≈ 20` rows, `n ≈ 40` devices — microseconds to low ms.
* SVD `O(m·n²)`, only on the free columns and only usable rows.
* No randomness anywhere in the inference path; seeded RNG appears only in
  tests. Repeated identical input gives bit-identical `x`.
