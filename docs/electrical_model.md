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

`branch` is the honest default for a single-channel CT. One CT measures one
*conductor*, not one appliance, so discovery cannot assert `dedicated_device` —
that requires knowing the circuit feeds exactly one load, which only a person
can state. `dedicated_device` is therefore only ever produced from a
declaration (ADR-0025).

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
  `MainPower.notes` (`duplicate_feeds_counted_once: ...`). In the measurement
  matrix the alias gets **no row of its own** — two copies of one conductor
  would be two equations for one physical quantity — but it becomes a
  *fallback target* for the canonical row: if the canonical sensor is missing,
  the alias's reading is used and the row is annotated
  `alias_fallback:<alias>`.

* **parallel mains without a declared relation.** A stored topology may declare
  six independent `main` CTs for what is physically one service split across a
  main panel and a sub panel (the reference install does exactly this). The
  engine cannot re-parent a stored topology, and summing same-leg feeds would
  count most of the house two to three times. `main_power_w()` therefore groups
  the feeds into *measurement nodes* (a `<stem>_l1`/`<stem>_l2` split-phase pair
  or a declared `paired_ct_id` pair is one node), partitions them by declared
  leg, and where two nodes measure the **same** leg it resolves the relationship
  **from the measurements**: the outermost node is the one whose total contains
  the others (within `_CONTAINMENT_TOL_W` = 30 W or 2 %). The choice is never
  silent —

  ```text
  notes: undeclared_feed_relation_resolved_by_measurement:
         load_manager_l1+load_manager_l2 is the outermost measured feed;
         excluded main_panel_l1, main_panel_l2, sub_panel_l1, sub_panel_l2
  ```

  Only when containment cannot be decided (the totals are indistinguishable, or
  none contains the others) does the function refuse: it reports
  `parallel_mains_without_declared_relation` and `usable=False`, because picking
  one would fabricate a panel tree and summing them would double count.

  A refusal is **per instant, not per topology**. On the reference capture the
  three panel feeds sit inside the 30 W / 2 % containment tolerance on 24 of 459
  windows of a 120-minute replay; on those instants no feed provably contains
  the others and the total is refused, while on the other 435 the load-manager
  pair does contain them and is chosen. The engine is behaving as designed. See
  §5 for how a refusal must be aggregated: it is a missing measurement, never
  `0 W`.

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

The subtraction in the third row is decided by a **coefficient test**, not by
`CTType`: a container row subtracts a child CT only when the container's own
row carries *none* of the devices below that child (`_row_excludes_subtree`). A
branch row holds only the devices whose most-specific attribution is that
branch, so a child's power really is missing from it and must be subtracted. A
main row contains every device beneath it — including those of its nested sub
feeds — so subtracting a child there would remove power the row still models.
Testing coefficients keeps both cases right and stops a future container type
from silently double counting.

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

#### A device whose phase nobody declared

`DeviceTopology.phase` defaults to `UNKNOWN` and discovery never guesses it, so
on a real install most devices start there. The *channel* a device is measured
through does state its own leg, and that is a declaration about the same
conductor, so `TopologyGraph.device_phase()` derives the device's phase from its
attribution CTs:

| Attribution CT legs | Derived device phase |
| --- | --- |
| all `L1` | `L1` |
| all `L2` | `L2` |
| both `L1` and `L2` (a declared pair, or a `BOTH` CT) | `BOTH` |
| anything else, or no attribution CT | `UNKNOWN` |

When the phase stays `UNKNOWN`, `phase_coefficient(..., solo=True)` returns
**1.0** on the device's own row. The reasoning is the same as for a `BOTH` row:
a CT dedicated to one device is an observation of that device, and halving it
because nobody wrote down which leg it is on would fabricate a missing 50 %. On
a *shared* row (a branch or main carrying several devices) the total must still
be apportioned, so the configured split applies there.

A `phase=BOTH` device without `double_pole=True` is a validation problem, and so
is a `double_pole=True` device declared `L1`/`L2`.

---

## 3. Double-pole loads and `paired_ct_id`

`CTChannel.paired_ct_id` marks the two physical legs of a single load
(e.g. `water_heater_ct1` ↔ `water_heater_ct2`). Symmetry is validated
(`pairing is not symmetric` is reported). When a device declares both legs as
its branch/dedicated CTs, `TopologyGraph.attribution_ct_ids()` returns **both**,
and each leg receives its own share through `_attribution_coefficient`.

A pair is only a leg pair when the partner is enabled **and** names the same
`device_id`. The field is free text and discovery pairs channels heuristically,
so two independent circuits named `..._ct1`/`..._ct2` can end up pointing at each
other; summing such a pair would make each device report the other's power.

Consequences:

* The two legs are **never** two devices.
* A dedicated double-pole pair sums its legs into one `power_w`
  (`ct_dryer_l1 + ct_dryer_l2 = 2 400 W`, one `InferenceResult`).
* A pair is usable **only as a pair**. If either leg is stale, missing, bad
  quality, or the two legs were sampled more than `max_leg_skew_s` apart
  (default **20 s**), *both* rows are excluded from the solve with reason
  `partial_leg_measurement:<ct>` or `leg_skew_exceeded:...`, and the direct path
  reports `usable=False`. One live leg reads one leg's power, not the load's,
  and no coefficient in the model knows the difference; using it would halve the
  row target and leave the missing half looking like unknown load. Two legs
  polled a minute apart describe two different instants, so using both mixes
  them into one equation.
* A partial pair is not silently treated as zero either: the power stays in the
  residual.

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

#### 4.1.1 Conditioning: preconditioning, iteration bound, and non-convergence

The trust-region path is not safe on real captures as handed to scipy. Two
defects combined to make `DisaggregationEngine.infer` **hang indefinitely** on
the reference install rather than fail:

1. A channel declared as one half of a `paired_ct_id` pair whose partner has no
   `device_id` produced **two rows with identical coefficients** and different
   measured values. The system then had rank 6 out of 8 rows and a condition
   number around `2 × 10^17`.
2. scipy's `lsq_linear` performs an *unbounded* backtracking line search
   (`_lsq.trf_linear.backtracking`). Its loop exits only when
   `cost_change > -0.1 · α · p·g`. On a numerically-zero step both sides are
   zero (`α ≈ 8.7 × 10^-19`, `cost_change = -0.0`, threshold `1.4 × 10^-50`),
   so it halves `α` forever. The outer `max_iter` is checked *outside* the line
   search, so no iteration budget can end it.

`solve_constrained_nnls()` now does three things before and around the solve:

* **`_precondition_rows`** drops zero-weight/all-zero rows and merges
  numerically identical rows by quadrature. This is exact, not an
  approximation: two rows sharing a unit direction `u` with norms `n₁, n₂`
  contribute `Σ nᵢ²(u·x - yᵢ)²`, which is one row of norm `√(n₁² + n₂²)` and
  value `(Σ nᵢ² yᵢ)/√(Σ nᵢ²)` up to a constant the solver ignores. Merged rows
  and dropped rows are reported in `SolveResult.merged_rows` /
  `SolveResult.dropped_rows`.
* **zero-norm columns** are dropped from the solve (nothing constrains them, so
  the lower bound is the only supported answer) and spliced back.
* **`tol = SOLVER_TOL` (1e-10)** and an explicit **`max_iter =
  SOLVER_MAX_ITER` (1000)** are passed to `lsq_linear`, and its status is mapped
  honestly: `1/2/3` = converged, `0` = iteration limit, `-1` = no progress.

`SolveResult.converged` is `False` for status `0`/`-1`, in which case the
returned `x` is the best point reached, **not** a solution, and must not be
presented as a measurement. The warning is `bounded_solver_iteration_limit` /
`bounded_solver_no_progress`, plus `solve_not_converged`.

On the captured 8×13 system this turns a >10-minute hang into 2 ms with
`status=1`, `converged=True`, and merges `{3: 2, 5: 4}`.

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

The guard is also **not** a "one claimant" rule. It fires for a declared fixture
on a row it shares with another declared fixture when that fixture states no
wattage, because a circuit may carry fixtures nobody declared and the engine
cannot tell a complete fixture list from a partial one (ADR-0026). Before that
was fixed the condition was `bool(observed) and not siblings`, so declaring a
*second* fixture is what unlocked whole-row credit: on one 881.72 W lighting row,
one declared fixture with no wattage produced `unknown`, and two declared
fixtures — one ON, one OFF, neither wattaged — produced **881.72 W credited to
the single ON lamp at `tier_3_measured`**.

#### 4.2.2 A declared wattage bounds its device (ADR-0025)

A slack protects the **sum** on a row. It does nothing for a row whose claimants
are all configured, and that is where the most damaging error appears: a row
reading far more than its claimants declare.

Measured on an instrumented installation, one lighting row peaked at 1 036 W
while its three fixtures declared 243.6 / 112.3 / 112.7 W. With one fixture ON
and its siblings positively OFF, the solver handed the whole row to that one
lamp — 4.25× its declared draw — and graded it `tier_3_measured`.

Adding a slack unconditionally is not the answer: it collapses a legitimate
separation (three 156 W lamps on a 468 W row became 117 W each, and the solver
reported an infeasible problem). The row is genuinely separable while the
reading is within what the claimants could draw.

So a **declared wattage is a non-relaxable upper bound** on a shared row:

* `EffectiveConfig.config_ceiling_w` (`expected_on_w + tolerance`) is applied as
  the solver's upper bound for every configured claimant that is not alone on
  its row and not state-pinned;
* the relaxation pass — which exists so a stale *model* bound can be widened —
  never widens it. Like a state pin, a declaration is user evidence, not a
  revisable prior;
* a solo row is exempt, because there the CT is a direct observation of the
  device and the measurement wins.

When the bound *binds and leaves power unexplained* the result is downgraded:
the number is the user's declaration, not an observation, so it is reported as
`configured_prior` / `tier_1_modelled` with
`declared_wattage_bounds_row_attribution` in the explanation. Reaching the
ceiling with the row fully explained is still a real measurement and stays
`tier_3_measured`. Diagnostics record `declared_ceiling_applied` and
`declared_ceiling_hit`.

#### 4.2.3 The measured step on a shared row (ADR-0026)

A slack and a ceiling both protect the row, and both leave the fixture with no
per-fixture value on a shared circuit. The evidence that survives that is the
**movement**: the row changed by *N* watts at the instant one device on it
changed state, and nothing else on the row changed.

That is a measurement of the device, and it does not require the siblings to be
OFF — only to be unchanged. A second lamp may be running throughout; it cannot
explain a step it did not cause.

The rule is a conjunction of refusals. Any failure leaves the device `unknown`
and its watts in the residual:

| Condition | Why it is required |
| --- | --- |
| The row is **shared** | A solo row already reports the whole row correctly; the step would be a worse answer there. |
| The step is on an **attribution row** | An enclosing main may have moved for any other circuit in the panel, so a main step says nothing about this device. |
| **No sibling changed state** | Two simultaneous changes make the movement unattributable (`ambiguous_concurrent_events`). |
| A **declared load entity** exists | An undeclared state key is not evidence (ADR-0012). |
| The transition is an **activation** | A deactivation says what the device drew a moment ago, not what it draws now. |
| The step **direction agrees** | An activation that lowers the row is not this device turning on. |
| The reading is **not stale**, the device not **OFF** | A stale or contradicted value is not a measurement. |
| The step is **≤ the row** | A change cannot exceed the value it changed; a breach means a mis-scaled or mis-signed channel. |
| The step is **≤ the declared wattage** | A 400 W movement on a row whose fixture declares 50 W is not that fixture; the surplus belongs to something else (`step_exceeds_declared_wattage`). |

A step that passes is reported at `TIER_3_MEASURED` with reason
`shared_circuit_step_measured`, while `source` stays `inferred`: the row is still
shared, so no CT measured the device privately, but the evidence is a
measurement rather than a share of a row. A deactivation step is recorded as
`shared_circuit_step_measured_on_deactivation` and is **not** reported as current
power — the device is off, and the row has already dropped.

Between transitions a shared row has no per-fixture answer, so a fixture that has
not changed state recently reads `unknown`. That gap is deliberate: a plausible
permanent number that no measurement supports is exactly the fabrication ADR-0006
exists to prevent.

### 4.2.4 The residual split: meter disagreement vs unmonitored load

`Reconciliation.unknown_residual_w` is `main_power_w - accounted`. It is one
honest number and it keeps exactly that meaning (contract 1.5.0 does not change
it). But when the home is metered **twice on the same conductors** — a canonical
feed plus an aliased second panel — two different *kinds* of number are in play,
and contract 1.4.0 made the mistake of treating them as parts of one whole.

| Quantity | Field | What it is |
| --- | --- | --- |
| Meter spread | `meter_spread_w` | `\|canonical total − alias total\|`. An **independent, unbounded** property of two instruments on the same wire. Diagnostic only. |
| Meter disagreement | `meter_disagreement_w` | That spread's **contribution to the residual**, clipped into `[0, unknown_residual_w]`. |
| Unmonitored load | `unmonitored_w` | `unknown_residual_w − meter_disagreement_w`. The remainder not attributable to a device. |

Measured on the reference install: the residual (`unknown_residual_w`) has a
median of **166.4 W**, and the independently computed meter spread
(`|canonical − alias|`) has a median of **348.9 W**. The two meters disagree by
*more than twice the whole residual*.

That is why 1.4.0's identity could not hold. It reported

```text
meter_disagreement_w = |canonical − alias|              # 348.9 W, unbounded
unmonitored_w        = max(0, residual − disagreement)  # max(0, 166.4 − 348.9) = 0
```

so `unmonitored_w + meter_disagreement_w = 348.9 ≠ 166.4 = unknown_residual_w`,
an error of 182.5 W on that cycle and 233–660 W across the capture. The residual
is a **bounded accounting remainder** for one meter; the spread is an
**unbounded measurement difference** between two. A bounded quantity cannot
contain an unbounded one, and forcing them to add is a category error. Even when
the spread happens to be smaller than the residual, "unmonitored" means "not
attributable to a device", **not** "not metered".

Contract 1.5.0 separates the concepts:

```text
meter_spread_w       = |canonical feed total − aliased feed total|   (never guessed)
meter_disagreement_w = min(meter_spread_w, max(0, unknown_residual_w))  (clipped contribution)
unmonitored_w        = unknown_residual_w − meter_disagreement_w
```

so

```text
unmonitored_w + meter_disagreement_w == unknown_residual_w     — exactly, always
accounted + unknown_residual_w == main_power_w                 — unchanged
```

and `unknown_residual_w` is never rewritten to either part. When
`meter_spread_w > unknown_residual_w` the whole residual is attributable to the
instruments, so `meter_disagreement_w == unknown_residual_w` and
`unmonitored_w == 0`.

In that case the contract sets `meter_spread_exceeds_residual = true` and adds
`meter_spread_exceeds_residual:<spread>W_vs_<residual>W` to `explanation`. This
is never hidden: `unmonitored_w == 0` next to a spread larger than the residual
would otherwise read as "everything is metered", which is the opposite of the
fact. It means *the residual is not evidence of unmonitored load*.

`meter_spread_w` is `0.0` — and no claim is made — unless **all** of these hold:

* there is at least one enabled alias channel (`alias_of_ct_id`);
* the alias legs form a complete meter point (every leg of every node they
  appear in is aliased, and all are fresh: not stale, missing or bad quality);
* the canonical channels those legs alias are exactly the enabled
  `effective_feeds()` of the home, so both totals cover the same conductors.

An unmeasured, stale or half-measured meter is never evidence of disagreement,
and a second meter that watches only some conductors is not a check on the
whole-home total. When the split applies, the explanation says which part is
which: `residual_includes_meter_disagreement:<W>_of_<total>W` and
`unmonitored_residual_excludes_meter_disagreement:<W>W`, and
`diagnostics["meter_disagreement"]` names the channels compared and carries both
`spread_w` and the clipped `disagreement_w` plus `exceeds_residual`.

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

A model that has no parameter for a state must not fall back to its aggregate
`estimated_power_w` when that state says "off": "no model value for this state"
is the honest answer, and substituting the estimate reported an OFF device at its
full running power. A stated standby remains the only non-zero value an OFF
device can carry (ADR-0026).

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
  **not** treated as `0 W`. This holds for a feed inside a conflict group too:
  a member with no reading may carry a disjoint leg (which would add) or may be
  the outer feed the measured member nests inside, and neither can be decided
  without the value. It is named in `notes` as `feed_group_unmeasured` and
  forces `complete=False` rather than being dropped from the group.
* Nested mains are excluded from the sum (see §1.1), not an error.
* Feeds are grouped into **measurement nodes**: a `<stem>_l1`/`<stem>_l2`
  split-phase pair, or a declared reciprocal `paired_ct_id` pair, is one meter
  point. Two nodes are added only when their declared legs are disjoint. Same-leg
  nodes with no declared `parent_ct_id` are resolved by measurement (§1.1) and
  the resolution appears in `notes`; if it cannot be decided the total is
  refused (`parallel_mains_without_declared_relation`, `usable=False`).
  A refusal is a **missing** total: `complete=False`, `quality_status=MISSING`,
  and — like any other missing main — it must not be aggregated as `0 W`. A
  consumer that averages windows (a replay tool, a daily summary) has to exclude
  the instant and report how many it excluded, or it fabricates a collapse to no
  load whenever the wiring is momentarily undecidable.
* A `BOTH` main is a full-circuit total; its L1/L2 split is for *reporting* only
  and uses the same configurable `phase_split_l1`.
* A node missing one of its legs sets `complete=False`, and an incomplete node
  never wins over a complete one when both measure the same conductor.

### 5.1 L1/L2 versus the total: same-snapshot, not one publish behind

`l1_power_w` / `l2_power_w` and `main_power_w` are produced **in the same call,
from the same `main_power_w()` over the same window** (`engine.py::_reconcile` →
`main_power_w(...)`, then `l1_power_w=main.l1_w`, `l2_power_w=main.l2_w`,
`main_power_w=main.total_w`). There is no cached phase value and no separate
publish path, so the phases cannot lag the total by a cycle.

The invariant `l1_power_w + l2_power_w == main_power_w` holds **to within
floating-point summation order**, not bit-exactly: `total_w` is
`sum(per_channel.values())` while each phase is its own `sum(...)` of a subset,
and IEEE-754 addition is not associative. On the reference capture (11 389 rows
with both phases) the largest gap is `1.8e-12 W` — a relative error of ~1e-16,
which is why a validator comparing with `!=` sees a non-zero "error". A
genuine one-snapshot lag would show the *previous* snapshot's total against
this snapshot's phases: on the same data that is off by a median ~12 W and up to
~10 kW. `tests/unit/test_disaggregation.py::test_l1_l2_match_the_total_in_the_same_snapshot`
pins both facts. Consumers must compare with a tolerance (or round to the
reported precision), never with exact equality.

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
* `usable=False` when a double-pole pair's two legs were sampled more than
  `max_leg_skew_s` apart, with reason `leg_skew_exceeded:...`. This is the
  **same** limit and the **same** per-leg time source (each reading's `age_s`
  against the window instant) that the solver path uses in
  `_exclude_partial_legs`; the constant is defined once, as
  `electrical/direct.py::DEFAULT_MAX_LEG_SKEW_S`, and `DisaggregationEngine`
  defaults to it. Before dev.12 the direct path had no skew check, so one missed
  poll on a 15.7 s panel could put a pair's legs 31 s apart and the direct path
  would sum two vintages into one device watt while the solver excluded the same
  row. The pair is never repaired by averaging, by taking the newer leg, or by
  falling back to one leg.
* A **branch** CT is never direct measurement for a device, even when only one
  device currently draws power: the branch may carry unmodelled load.
* A direct value is **never** replaced by a model estimate. Conflicts with a HA
  state transition are surfaced (`state_direct_measurement_conflict`) and cap
  confidence, but the measured value is kept.

### The gate: 0 W direct is allowed only when explained

`direct_measured_count` alone cannot say whether a "0 W direct" batch is a
legitimate install or a broken Level-1 path. Two very different houses produce
the same count:

* every circuit is a `branch` (this is the honest default, ADR-0025/0026), so
  nothing is *capable* of a direct reading;
* the dedicated CTs exist but no reading reached the window, so the path is
  producing nothing.

The batch therefore carries a verdict, computed from the graph and the readings
rather than from the explanation loop's own bookkeeping:

| `direct_availability` | Meaning |
| --- | --- |
| `no_dedicated_ct` | the topology has no `DEDICATED_DEVICE` channel at all |
| `measured` | at least one capable device produced a usable reading |
| `explained_unavailable` | capable, but every device is named in `missing_direct` |
| `unexplained_unavailable` | **a defect**: a capable device is neither measured nor named |

`direct_capable_count`, `direct_unavailable_explained` and
`unexplained_direct_devices` accompany it, and
`Reconciliation.explanation` names the missing count by reason
(`no_direct_measurement:N device(s) (no_dedicated_device_ct×k, ...)`). The
release gate is exactly: `direct_measured_count > 0` **or** every capable device
is named in `missing_direct`; i.e. `direct_availability` is never
`unexplained_unavailable`. This is pinned on the real capture by
`tests/simulation/test_real_topology.py::test_the_real_install_has_a_live_direct_measurement_path`.

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

Contract **1.3.0** added one additive field, `Reconciliation.topology_confirmed`
(`bool`, default `false`), so a consumer can tell whether the balance was
produced from a user-confirmed topology or from a `candidate` hypothesis. Older
readers ignore it.

Contract **1.4.0** added two more additive fields, `Reconciliation.unmonitored_w`
and `Reconciliation.meter_disagreement_w` (both `float`, default `0.0`), which
decompose the existing `unknown_residual_w` without changing it (§4.2.4). An
older reader that ignores them still sees the same residual it always saw.

Contract **1.5.0** corrected that decomposition and added two fields:
`Reconciliation.meter_spread_w` (`float`, default `0.0`) is the raw, unbounded
`|canonical − alias|` diagnostic, and
`Reconciliation.meter_spread_exceeds_residual` (`bool`, default `false`) says the
spread is larger than the whole residual. `meter_disagreement_w` keeps its name
but now means the spread's *contribution to the residual*, clipped into
`[0, unknown_residual_w]`, so `unmonitored_w + meter_disagreement_w ==
unknown_residual_w` holds exactly (§4.2.4). Storage carries both new fields in
the existing schema-7 migration; schema 7 was never deployed, so no version 8 is
needed.

The remaining observations are non-blocking:

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
