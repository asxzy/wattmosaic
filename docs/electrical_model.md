# Electrical Model

Owner: Agent E (Electrical Modeling and Load Disaggregation).
Code: `engine/wattmosaic_engine/electrical/`.
Authoritative schema: `engine/wattmosaic_engine/contracts.py` (imported, never
redefined). Interface: `docs/interface_contracts.md` §3.3.

---

## 1. Why a topology, not a sum

A house is a tree, and every CT in it measures a *subtree*:

```text
Utility L1 (main)                                   3 000 W
  |
  +-- Panel L1 feed (main)                          1 200 W
  |     |
  |     +-- Kitchen branch CT                         700 W
  |     |     +-- Fridge CT                           200 W   (nested branch)
  |     |     +-- Lights (no CT)                        ? W
  |     |
  |     +-- Laundry branch CT                         120 W
  |
  +-- EV charger (main only)                         1 800 W
```

Summing every CT would give `3 000 + 1 200 + 700 + 200 + 120 + 1 800 = 7 020 W`
for a 3 000 W service. The topology model exists to make that impossible.

**Three channel roles** (`CTType`):

| Role | Measures | Can be a device's attribution CT? |
| --- | --- | --- |
| `main` | an incoming line / panel feed | no (only a containment constraint) |
| `branch` | a circuit containing zero or more devices and possibly child CTs | yes |
| `dedicated_device` | exactly one logical device | yes (Level 1) |
| `unknown` | unverified | no (validation warning) |

**Containment** is `child.parent_ct_id == parent`. `TopologyGraph` indexes it,
detects cycles, and refuses to build rows for inconsistent input.

### 1.1 The double-counting rule

> A device is attributed using exactly **one** most-specific CT: its verified
> dedicated CT if it has one, otherwise its **deepest** enclosing branch CT.

Everything above that CT is a *containment constraint*, modelled explicitly:

* an **ancestor branch row** does **not** contain a second copy of the device.
  Instead the ancestor row carries the child CT in
  `MeasurementMatrix.nested_children`, and the child's measured value is
  **subtracted** when the target vector `y` is built:

  ```text
  y_kitchen = P_kitchen_measured - P_fridge_measured
            = 700 - 200 = 500 W        -> lights + other unmodelled kitchen load
  ```

  If the child value is missing or stale, the parent row is **excluded** and the
  reason is recorded (`nested_child_missing:...`). It is never used raw, which
  would count the fridge twice.

* a **main row** legitimately contains every device below it (that is the
  physical total). Main rows are never *added together* to obtain a total, and
  each device appears in a main row exactly once.

* **nested mains** (`main` with a `parent_ct_id`) are a supported shape, to any
  depth: utility → panel → sub-panel → branch. `main_power_w()` sums only the
  *outermost* feed of each branch of the tree (`TopologyGraph.effective_feeds`),
  so a nested feed is inside its parent's measurement and is never added again:

  ```text
  service 5000 W, panel 2000 W under it   ->  total 5000 W, not 7000 W
  ```

  The excluded feeds are listed in `MainPower.excluded_channels` and named in
  `MainPower.notes` (`nested_feeds_not_summed: panel under service`). This is a
  resolution, not a refusal: the nesting is a wiring fact and the total stays
  available.

* **duplicate feeds** (two CTs clamped on the same conductors, `alias_of_ct_id`)
  measure one point twice. Only the canonical channel contributes; the alias is
  still usable for its own device readings and is named in
  `MainPower.notes` (`duplicate_feeds_counted_once: ...`).

* a **circuit multiplier** (`CTChannel.measurement_multiplier`, default `1.0`)
  scales a reading exactly once, on the way out of `TopologyGraph.scale_w()`.
  It exists for one CT on one leg of a 240 V load; it is never inferred from
  `phase` or `ct_type`, because the monitor commonly applies the vendor's
  circuit multiplier itself and already publishes the full load.

---

## 2. The measurement model

For one instant `t`:

```text
y(t) = A(t) · x(t) + e(t)
```

* `y ∈ R^m` — measured CT power in watts, polarity-corrected, **sign preserved**.
* `x ∈ R^n` — unknown *total* power of each logical device (one column per
  device, never per pole).
* `A ∈ R^{m×n}` — electrical incidence matrix. `A[i,j]` is the fraction of
  device `j`'s power that physically appears in CT row `i`.
* `e` — noise, unmodelled loads, measurement error.

### 2.1 Row construction

| Row kind | `A[i,j]` |
| --- | --- |
| Dedicated CT of device `j` | `1` (phase split for a single-leg row of a double-pole pair) |
| Device `j`'s most-specific branch CT | `1` |
| Any other branch CT above `j` | `0` — the child CT is subtracted from `y` instead |
| Main CT containing `j` | `1` (containment) |

Worked example — the tree at the top of this page with devices
`lights`, `fridge`, `washer` and a dedicated `tv` CT:

```text
            lights  fridge  washer  tv
main_l1  [    1       1       1      0 ]     3 000 W
kitchen  [    1       0       0      0 ]       700 - 200 = 500 W (child subtracted)
fridge   [    0       1       0      0 ]       200 W
laundry  [    0       0       1      0 ]       120 W
ct_tv    [    0       0       0      1 ]        50 W
```

`lights` never appears in the fridge row, and the fridge never appears twice in
the kitchen row. Total attributed power = `x_lights + x_fridge + x_washer +
x_tv`, computed from the vector only.

### 2.2 Uncertainty weights

Each row gets a standard deviation and a weight `w_i = 1/σ_i`, applied as
`min ‖W(Ax - y)‖²`:

```text
σ_i = max(noise_floor, resolution_i / 2) / reliability_i
noise_floor = 1.0 W
```

* `resolution/2` reflects quantisation.
* `reliability ∈ (0,1]` scales the noise of a channel known to be flaky.
* A repeated state update (same value as before) halves that row's weight,
  because it is **not** a new physical sample.

### 2.3 Phase handling (`L1`, `L2`, `BOTH`)

`A[i,j] = phase_coefficient(device_j, row_phase_i)`, where the **device's**
declared phase decides the split:

| Device phase | Row `L1` | Row `L2` | Row `BOTH` |
| --- | --- | --- | --- |
| `L1` | 1 | 0 | 1 |
| `L2` | 0 | 1 | 1 |
| `BOTH` (double-pole) | `α` | `1-α` | 1 |

* `α = phase_split_l1`, default **0.5**, configurable
  (`ElectricalModel(phase_split_l1=0.7)`).
* The split is applied **only** when the topology declares the device
  `double_pole=True` / `phase=BOTH`. A single-phase load is never smeared across
  both legs.
* `x_j` is always the **total** device power. On a `BOTH` row a 2 400 W dryer
  contributes `1 · 2 400`; on separate L1/L2 rows it contributes `1 200` to each.
  The device is still one column and one result.

A `phase=BOTH` device without `double_pole=True` is a validation problem, and so
is a `double_pole=True` device declared `L1`/`L2`.

---

## 3. Double-pole loads and `paired_ct_id`

`CTChannel.paired_ct_id` marks the two physical legs of a single load
(e.g. `water_heater_ct1` ↔ `water_heater_ct2`). Symmetry is validated
(`pairing is not symmetric` is reported). When a device declares both legs as
its branch/dedicated CTs, `TopologyGraph.attribution_ct_ids()` returns **both**,
and each leg receives its own share through `_attribution_coefficient`.

Consequences:

* The two legs are **never** two devices.
* A dedicated double-pole pair sums its legs into one `power_w`
  (`ct_dryer_l1 + ct_dryer_l2 = 2 400 W`, one `InferenceResult`).
* A missing leg yields `partial_leg_measurement` and lowers confidence; it is not
  silently treated as zero.

---

## 4. Solving: bounded NNLS with an explicit unknown-load slack

### 4.1 The problem

```text
minimise  ‖W (A x - y)‖²  +  λ‖x - x_prior‖²
subject to  0 ≤ x_j ≤ u_j
```

* `x ≥ 0` because a *device* cannot consume negative average power. Export is a
  property of the **main** CT and is preserved in `Reconciliation.main_power_w`.
* `u_j` is optional and comes from a validated device model
  (`docs/algorithms.md` §5). It is a prior, not a cap: if the CT rows say more
  than the model and the bound is active, the bound is relaxed and the refit is
  recorded as `model_upper_bound_relaxed`.
* `λ > 0` (default `0`, `model_ridge` configurable) pulls toward the model
  prior; it is off by default so measurements decide.
* Solver: `scipy.optimize.nnls` for the unconstrained-in-upper case,
  `scipy.optimize.lsq_linear(method="trf")` when upper bounds exist. Both are
  deterministic (no random init, bit-identical repeat runs).

### 4.2 The unknown-load slack

Unknown household load is **not** a device. If main rows were fit directly
without a slack, the least-squares optimum would push the unexplainable watts
onto whatever device shares the row — fabricated attribution.

`augment_with_unknown_slack()` therefore appends one non-negative column per
main row:

```text
A_aug = [ A   S ]
S[row_main, k] = 1
```

so a main row constrains

```text
Σ_j A[main, j] · x_j  +  slack_main  =  y_main ,   slack_main ≥ 0
```

which is physically `sum(known devices under main) ≤ main`. The slack is
reported in `diagnostics["unknown_slack"]` and folded into
`Reconciliation.unknown_residual_w`. It is never written back into a device.

#### 4.2.1 Shared rows declared by the user (ADR-0011)

The same argument applies to a **branch** row the user has declared `shared`
with siblings that are not modelled. One lamp on a `lights` circuit does not own
the whole circuit: without a slack, NNLS would hand the entire row to the only
claimant — a 300 W circuit reported as a 300 W lamp, the most damaging possible
error because it looks measured.

`augment_with_unknown_slack(..., shared_rows=[...])` therefore appends a slack
column for each such row exactly as it does for mains. The engine then blocks
level-2 attribution for that device (`shared_circuit_not_individually_measured`)
so the value can only come from a configured prior. The affected rows are listed
in `diagnostics["shared_rows_held_as_upper_bounds"]`.

Crucially, the slack is added **only when the siblings are unmodelled**
(`EffectiveConfig.shared_unmodelled`). A shared row whose other occupants are
all modelled devices stays solvable; adding a slack there would discard a
legitimate multi-device separation.

### 4.3 State constraints (not estimates)

An explicitly OFF device with a model is given the upper bound
`max(off_tolerance, 2 × standby_power)` instead of being left free. This makes a
shared branch identifiable:

```text
shared branch = 500 W, d1 = off (standby ≤ 15 W)
=> d0 is inferred at ≈ 490 W, d1 at ≈ 10 W
```

Pinned devices are removed from the identifiability analysis because they are
fixed by an external constraint, not estimated from the data.

Only states that positively mean "not running" pin a device
(`off`, `idle`, `standby`, `closed`, `paused`, `none`). `unavailable` and
`unknown` are treated as missing information, not as zero power.

### 4.4 Worked numeric example

Kitchen: `lights` and `fridge` share `branch_kitchen`, `fridge` has a child CT.

Measured: `main_l1 = 900`, `branch_kitchen = 900`, `branch_fridge = 200`,
`branch_laundry = 0`, `ct_tv = 0`.

```text
y = [900, 900-200, 200, 0, 0] = [900, 700, 200, 0, 0]

A = [ 1  1  1  0 ]   (main_l1)
    [ 1  0  0  0 ]   (branch_kitchen, child subtracted)
    [ 0  1  0  0 ]   (branch_fridge)
    [ 0  0  1  0 ]   (branch_laundry)
    [ 0  0  0  1 ]   (ct_tv)

NNLS -> x = [700, 200, 0, 0]
residual = 0
```

A naive interpretation of the same data (ignoring nesting) would give
`lights = 900, fridge = 200` → 1 100 W on a 900 W branch. The model returns
700 W and reconciles exactly.

---

## 5. Main power

```python
MainPower = main_power_w(graph, readings)
```

* Sums **parallel** mains only (`P_L1 + P_L2`); typically utility legs.
* **Sign is preserved**: `-1 500 + 400 = -1 100 W` → `has_export=True`.
  Nothing is clamped to zero anywhere in the pipeline.
* Polarity inverted channels are negated first, from `CTChannel.polarity` for
  bare floats and from `Measurement.polarization` / `ChannelReading` otherwise.
* A missing or `NaN` main is `missing_channels` and sets `complete=False`; it is
  **not** treated as `0 W`.
* Nested mains are an **error** (see §1.1), and `total_w` is not produced.
* A `BOTH` main is a full-circuit total; its L1/L2 split is for *reporting* only
  and uses the same configurable `phase_split_l1`.

---

## 6. Identifiability

Take the SVD of the weighted matrix `Aw = U S Vᵀ`. With rank `r`, the null space
is the last `n - r` right singular vectors `N = V[:, r:]`.

* A linear functional `cᵀx` is identifiable iff `c ⟂ null(Aw)`.
* Device `j` is individually identifiable iff `‖N[j,:]‖₂ ≈ 0`
  (`null_share ≤ 1e-8`).
* Devices with non-zero null components are grouped by shared null-space support
  into **ambiguous groups** (union-find over columns with `|N[i,k]| > 1e-6`).
* Condition number `s_max / s_r` measures how noise amplification; it is
  reported, not used to hide values.

`analysis/identifiability` is exposed in `InferenceBatch.diagnostics`
(`rank`, `condition_number`, `null_space_dimension`, `uniquely_identifiable`,
`unidentifiable`, `ambiguous_groups`, `null_share`).

**Interpretation matters.** Two devices on one branch with both ON is *not*
identifiable — no data distinguishes 300+200 from 100+400. Reporting 250/250
would be fabrication. The engine returns `unknown` (or `model_estimated` when a
validated model supplies the split, clearly labelled as not measurement-derived).

---

## 7. Level-1 direct measurement

`resolve_direct(window, graph)` returns a `DirectReading` per device that has a
verified dedicated CT:

* Sums a symmetric double-pole pair into one total (`per_leg_w` retained for
  provenance). Each leg carries its own `measurement_multiplier`, applied once
  by `TopologyGraph.scale_w()`, so a single-CT 240 V load declared as `2.0`
  reports the full load.
* `usable=False` for missing, bad-quality, or older-than-`max_age_s` values, with
  machine-readable reasons (`stale_measurement:ct_x`, `missing_measurement`, …).
* A **branch** CT is never direct measurement for a device, even when only one
  device currently draws power: the branch may carry unmodelled load.
* A direct value is **never** replaced by a model estimate. Conflicts with a HA
  state transition are surfaced (`state_direct_measurement_conflict`) and cap
  confidence, but the measured value is kept.

---

## 7a. Proposing nesting from measurements

`electrical/nesting.py` proposes panel nesting and duplicate feeds from aligned
measurements. It is **candidate generation only**: `parent_ct_id` and
`alias_of_ct_id` are never written by it, and the integration wizard asks the
user to confirm. See `docs/integration.md` for the wizard fields.

### The unit is a measurement node

A comparison is made on a **node**, not a single CT. A node is one physical meter
point:

* a CT plus its reciprocal `paired_ct_id` partner (a declared double-pole
  circuit), or
* the two legs of a split-phase panel feed, `<stem>_l1` / `<stem>_l2`, grouped by
  id stem and flagged `is_assumed` (the grouping is read off the channel ids, not
  declared).

Comparing legs compares different conductors. On the reference installation a
sub-panel's declared loads total 796 W against a 331 W leg (ratio 2.40) because
those loads span *both* legs, and one sub-panel leg is 0.6390 contained by one
panel leg but 0.9950 contained by the *other* panel leg — the declared `phase`
labels are contradicted by the measurements. Summed, panel ← sub-panel is
0.9975. `phase` is therefore used only to make the written-back leg readable,
never to reject a candidate.

### Evidence

* **containment** — fraction of instants the child never exceeds the parent
  (+30 W). Necessary, not sufficient: a branch cannot draw more than its feed.
* **excitation** — the child must reach 5 % of the parent at least once. An idle
  circuit is trivially contained by everything.
* **closure** — the parent's own directly measured loads plus the child must not
  exceed the parent (+100 W). The *median remainder* ranks competing parents: the
  immediate parent explains the child most tightly, while a grandparent always has
  the child's siblings left over. Closure is a level-placement signal, not a
  validity gate, so `CLOSURE_MIN` is 0.95 rather than 1.0.
* **duplicate** — two nodes that are jointly active (≥200 W) for ≥50 % of their
  combined active time, with median relative difference ≤0.15 and median
  magnitude ratio ≤1.25. They are the same conductors measured twice, so summing
  them double counts. Reported as an alias, never as a nest.

### What measurement cannot decide

**A load's level is not inferable.** Measured against 32 declared parents, four
independent rules all failed: tightest-peak 0/32, coverage 0/32 (every container
ties), unique-fit elimination 32/32 ambiguous, windowed step-response 1/25. A 9 W
lighting circuit is contained by every panel in the house at 1.000 while idle.
Only a `main` CT is therefore proposed as a nested child
(`_NESTABLE_CHILD_TYPES = {"main"}`); loads are reported in `not_inferable` and
the wizard asks the user.

### Reported, not resolved

* `ambiguous` — several parents fit and closure cannot separate them. The
  candidate list is reported and nothing is proposed.
* `suppressed` — a declared parent (`parent_already_declared`) or a parent whose
  own loads over-explain it (`closure_rejected`).
* `not_inferable` — a load (`load_level_not_measurable`).

`GET /topology/nesting` returns `candidates`, `candidate_parents`,
`candidate_aliases`, `ambiguous`, `suppressed`, `not_inferable` and `nodes`.
"Same conductor" (`candidate_aliases`) and "fed by" (`candidate_parents`) are
kept separate because they answer different questions and lead to different
writes.

---

Returns human-readable strings, never raises for bad input:

* duplicate CT / device ids
* unknown `parent_ct_id`, `paired_ct_id`, `dedicated_ct_ids`, `parent_device_id`
* self-parent, CT topology **cycles**
* asymmetric pairing, paired leg not L1/L2, paired leg marked BOTH
* `main` with a parent is **not** a problem: it is a nested feed (utility →
  panel → sub-panel), and `effective_feeds()` resolves the outermost feed of each
  branch rather than rejecting the topology.
* dedicated CT with no/unknown device, or missing from `DeviceTopology`
* `phase=BOTH` without `double_pole=True`, double-pole device declared L1/L2
* device declaring branch CTs that are all disabled
* unknown phase (rows treated as `BOTH`)

An inconsistent topology still produces a best-effort matrix, but every affected
row/device carries a diagnostic and confidence is reduced.

---

## 9. Limitations

1. **Unidentifiable configurations are not solved by mathematics.** More CTs,
   different CT placement, or state information are the only real fixes.
2. **Phase split `α` for double-pole devices is an assumption**, not a
   measurement. It is reported and configurable; a per-device `α` is not in the
   contract (see §10).
3. **Unknown-phase channels** are conservatively modelled as `BOTH`, which
   over-attributes to the total but never loses energy.
4. **`rel`/`resolution` metadata is only as good as the audit.** Defaults are
   conservative (`σ = 1 W` floor).
5. **Sign of a device-level negative** is clipped to `0` by the non-negativity
   constraint. This is intentional; a negative device reading is either export
   (a main property) or an unmetered generation source, which must be modelled
   explicitly rather than as a negative appliance.

## 10. Contract notes / gaps

The contract was sufficient for every required behaviour except the following
non-blocking observations (no edits made to `contracts.py`):

* `DeviceTopology` has no per-device phase-split fraction, so `α` is global
  (`ElectricalModel(phase_split_l1=...)`). A per-device override would require a
  contract change.
* `CTChannel.paired_ct_id` is documented as "paired CT for a single double-pole
  load", but `DeviceTopology` has no `double_pole_pair` field. The pairing is
  therefore expressed as two branch/dedicated CT ids plus `double_pole=True`,
  which the graph resolves; this works but requires the two declarations to
  agree, and disagreement is reported rather than silently fixed.
* There is no explicit "unknown load" entity in the contract. It is represented
  as `Reconciliation.unknown_residual_w` plus solver slack, which matches the
  spec's intent (§9.5) but means unknown load has no per-circuit breakdown.
