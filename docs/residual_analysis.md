# Residual analysis (v2)

Reference for `engine/wattmosaic_engine/models/residual_power.py` — the signed
residual as a first-class object: what was measured, whether the arithmetic is
licensed, how the number splits into baseline and dynamic parts, and what a
refusal means.

`Reconciliation.unknown_residual_w` already satisfies the balance
(`accounted + unknown_residual_w == main_power_w`, and
`unmonitored_w + meter_disagreement_w == unknown_residual_w`). What it cannot
answer is *"what kind of number is this, and how has it moved?"*. This module
adds that layer **without touching the engine's balance**.

---

## 1. The core identity

```
Residual(t) = CircuitPower(t) - Σ KnownLoadContributions(t)
```

`_signed_residual(circuit_power_w, known_load_w)` (`residual_power.py:298`) is
the **only** subtraction in the module. It applies:

* no `max(0.0, ...)`,
* no `abs()`,
* no epsilon.

A negative residual is a real observation (accounted power exceeds the measured
total) and keeps its sign. `kind` is always
`contracts.derived_residual_kind(residual_w)` (`contracts.py:521`), so there is
exactly one definition of positive / negative / near-zero in the codebase: a
residual of `1 W` is `POSITIVE`, and only an exactly-zero residual is
`NEAR_ZERO`. A tolerance here would hide a real unexplained load.

`ZERO_TOL_W = 1e-9` (`residual_power.py:112`) is used for **one** purpose:
deciding whether the residual is "resolved" (`abs(residual) <= ZERO_TOL_W`,
`:435`). It never moves a value towards zero.

---

## 2. Validity: three checks before any subtraction

`ResidualAnalyzer.try_compute(scope, circuit_power_w, now)` (`residual_power.py:396`)
runs three gates and returns a `ResidualAttempt` carrying `refused`,
`reason_code` and `evidence`. `compute` (`:374`) is the raising form and picks
the exception class from the reason code
(`IncompatibleBasisError` for `incompatible_basis*` / `unknown_scope_basis`,
else `ResidualNotComputed`).

### 2.1 Input sanity

A non-numeric or non-finite `circuit_power_w` is refused as
`non_finite_circuit_power`. `_strict_float` (`residual_power.py:312`) refuses
numeric **strings**: `float("1000")` succeeds, and a string in a watt slot is a
caller bug whose silent coercion would hide a type error.

### 2.2 `_canonicalise` — one conductor only

`_canonicalise` (`residual_power.py:666`) resolves every channel of the scope
through `TopologyGraph.canonical_channel_id` and groups them by owner.

* More than one group → refused `scope_spans_multiple_channels`. A scope with
  two conductors is not one residual.
* Collapsed aliases are recorded in `evidence["collapsed_aliases"]`;
  `evidence["counted_channels"]` is the resulting set.
* When several raw CTs of one conductor report different values, the spread is
  recorded as `alias_spread_w` and **never added to the total**: it is a meter
  diagnostic, not extra load.

Without a topology graph the canonical id is the declared id and the evidence
says `canonicalization: "declared_only"` (`:404`) — the analyzer can then only
verify what the scope itself declares, and it says so.

### 2.3 `_check_bases` — comparable bases or an explicit conversion

`_check_bases` (`residual_power.py:711`):

* an `UNKNOWN` scope basis → refused `unknown_scope_basis`;
* a contribution with no declared basis **inherits the scope's basis**
  (absence of a distinct claim, not a claim of dimensionless);
* an explicit `conversions[device_id]` makes an otherwise incompatible
  contribution comparable, is applied exactly once in
  `_counted_contributions`, and is recorded in `evidence["conversions"]`
  (`invalid_conversion` if not finite/positive);
* otherwise `bases_are_compatible(scope_basis, basis)` must hold, else refused
  `incompatible_basis` with `evidence["incompatible_contributions"]`.

#### `bases_are_compatible` (`residual_power.py:166`)

Same basis is always comparable. Beyond that exactly two equivalences hold, and
both are physical statements rather than conveniences:

| Pair | Comparable? | Why |
| --- | --- | --- |
| same basis | yes | trivially |
| `CT_MEASURED` ↔ `MONITOR_REPORTED` | yes (`_PHYSICAL`, `:157`) | both are measurements of that conductor |
| `MONITOR_REPORTED` ↔ `FACTOR_ADJUSTED` | yes (`_WHOLE_APPLIANCE`, `:161`) | a monitor that applied the factor *is* the factor-adjusted case |
| `FACTOR_ADJUSTED` ↔ `CT_MEASURED` | **no** | one side claims an appliance total, the other a leg; only a recorded multiplier can bridge them |
| `ESTIMATED_APPLIANCE_TOTAL` ↔ anything measured | **no** | an estimate may be reported, not subtracted from a reading |

Note `CT_MEASURED` ↔ `FACTOR_ADJUSTED` is the asymmetry people get wrong: the
pair is *not* compatible even though `MONITOR_REPORTED` is compatible with each
of them separately. Compatibility here is not transitive, on purpose.

### 2.4 `_counted_contributions` — each conductor once

`_counted_contributions` (`residual_power.py:750`) decides the whole candidate
set first, so the answer does not depend on declaration order. Two structural
rules:

1. **same conductor** — contributions whose CTs resolve to one canonical
   channel are one observation. The kept device is the one declared on the
   canonical channel itself, else the lexicographically first; the others are
   recorded as `duplicate_of:<kept>` in `evidence["duplicate_contributions"]`.
2. **enclosure** — a contribution whose CT sits under another contribution's CT
   is already inside that total (`effective_feeds` rule), so subtracting both
   counts it twice. Recorded as `enclosed_by:<other>`.

Excluded contributions go to `evidence["excluded_contributions"]`;
`evidence["subtracted_contributions"]` is the final map.

---

## 3. Baseline vs dynamic

`_split(residual_w, window, evidence)` (`residual_power.py:842`):

* `window` is `ResidualScope.window_w` — recent **residual observations**,
  oldest first, *not* circuit totals. The baseline is a statement about the
  residual, so mixing the two would make the split meaningless.
* `baseline_w = median(window)`, `dynamic_w = residual_w - baseline_w`. The two
  always add back to the total.
* With no window there is no dynamic claim: `baseline_w = residual_w`,
  `dynamic_w = 0.0`, and `evidence["baseline_estimator"]` says
  `no_window_current_only`.

**Why the median.** The residual window routinely contains a minority of large
excursions (a compressor start, a meter glitch) that would move a mean by
kilowatts while leaving the median untouched. The split is meant to say "this
much is always there, this much is moving", and a robust central tendency is
the only estimator that does not turn one spike into a permanent baseline.

`evidence["window_samples"]` and `evidence["window_span_w"]` record how much the
baseline was based on.

---

## 4. Resolution is a legitimate permanent state

In `try_compute` (`residual_power.py:435`):

```python
resolved = abs(residual_w) <= ZERO_TOL_W
unresolved_since = None if resolved else (scope.unresolved_since or now)
```

`resolved=False` is a **valid, possibly permanent outcome**. An unexplained
residual may stay unexplained forever, and it is never rendered as `0 W`.
`unresolved_since` preserves the age of the question; `ResidualPower`,
`ResidualDecomposition` and `ResidualTrend` all report the signed number, the
resolution state and its age, so a consumer cannot mistake "unresolved" for
"nothing there" (`ResidualPower` docstring, `contracts.py:534`).

---

## 5. `decompose` — explaining part of a residual

`decompose(residual_history, candidate_contributions, now)` (`residual_power.py:456`)
subtracts candidates from the **latest** residual only. Rules:

| Condition | `reason_code` | `conflict` | `resolved` |
| --- | --- | --- | --- |
| no history at all | `no_residual_history` | `False` | `False` |
| latest residual is negative | `residual_is_negative` | `True` | `False` |
| no candidate is a positive finite load | `residual_not_disaggregable` | `False` | `False` |
| candidates total `> before_w + ZERO_TOL_W` | `candidates_over_explain_residual` | `True` | `False` |
| `abs(after_w) <= ZERO_TOL_W` | `residual_resolved` | `False` | `True` |
| otherwise | `residual_partially_explained` | `False` | `False` |

Details worth stating:

* **No history is not `0 W`.** Reporting `0.0` here would be a fabricated
  measurement that reads as "nothing is unexplained" — the most misleading
  possible answer. `before_w`/`unexplained_w` are `None` and `quality_status`
  is `MISSING`.
* **A negative residual cannot be explained by a load.** Accounted power
  exceeds the measured total, so a positive candidate cannot explain a deficit;
  any claim is a contradiction. All explanatory fields are emptied.
* A candidate that is not a number, not finite, or `<= 0` is excluded with
  `reason: "not_a_number"` / `"not_a_load"` in `evidence["excluded"]` rather
  than silently dropped.
* Partial explanation is normal: `residual_partially_explained` keeps the
  remaining `unexplained_w` and merges `prior_explained` with the accepted
  candidates.
* The before/after pair is always in evidence so a shrink is visible.

---

## 6. `trend` — history as a curve

`trend(residual_history, decompositions)` (`residual_power.py:592`):

* `current_w` is the newest residual; `typical_w` = `baseline_w` = median of
  all residuals; `dynamic_w = current - typical`.
* `confidence = min(1.0, n / (n + 8.0))` — monotonically rising with sample
  count, and `None` when there is no history at all, never a fabricated `0.0`
  or `1.0`.
* `curve` is ascending in time even though `Database.residual_history` returns
  newest first, so a reader can plot it without reasoning about storage order.
* `evidence` carries `cause_counts`, `first_at`, `last_at` and `sign_changes`.
* No history → `reason_code="no_residual_history"`, all values `None`,
  `quality_status=MISSING`, `sample_count=0`.

`_ordered_history` (`residual_power.py:858`) normalises DB rows and bare
`ResidualPower` models. A model has no timestamp of its own, so it is given one
second's worth of synthetic ordering at the position it was supplied — never
placed at "now", which would reorder history behind the caller's back. The sort
is stable, so equal timestamps keep their supplied order.

---

## 7. What the analysis pass actually computes

`AuditPass._residuals` (`disaggregation/audit.py:1486`) computes **one residual
per single-CT circuit scope** and refuses the rest with a machine code:

| Condition | `report.refused[...]` |
| --- | --- |
| `len(circuit.ct_ids) != 1` | `multi_ct_scope` (`REFUSED_MULTI_CT_SCOPE`, `audit.py:97`) |
| no current reading for the CT | `no_scope_reading` (`REFUSED_NO_READING`, `:98`) |
| basis is `UNKNOWN` | `unknown_scope_basis` (`REFUSED_UNKNOWN_BASIS`, `:99`) |
| `try_compute` returns no residual | the module's own `attempt.reason_code` |

**A multi-CT circuit is deliberately not summed.** Two clamps may be the two
legs of one 240 V appliance (in which case the total is their sum) or two
monitors on one wire (in which case the sum is a double count), and nothing in
the evidence distinguishes them. The scope is recorded as refused — a number
that would be wrong half the time is worse than a named refusal.

Contributions come from `by_device` results that are in `circuit.device_ids`
**and** whose `source` is not `UNKNOWN` (`audit.py:1522`): a value the engine
itself declined to attribute is not a known load.

A reanalysis window has no live readings (`observe_readings` returns `{}`,
`audit.py:1855`), which is why such a scope is reported `no_scope_reading`
rather than computed from a stale value.

The pass records results both as a count (`report.counts["residuals"]`) and as
the actual objects (`report.residuals`), because a pass that counted residuals
without keeping them would report "160 residuals" and hand back an empty list
(`audit.py:1550-1554`).

---

## 8. Persistence

`persist(results, database, timestamp=...)` (`residual_power.py:1115`) writes
through `Database.insert_residual_power`; `Database.residual_history`
(`database.py:1046`) reads them newest first and decodes
`explained_by_device_w` and the boolean `resolved`.

The `residual_power` table is in `AUDIT_TABLES` (`storage/migrations.py:121`),
so `Database.prune` reports it as `0` and never deletes it: it is part of the
versioned history, not a raw sample. This is the mechanism by which
`unresolved_since` and a long trend survive retention.

---

## 9. Boundaries worth stating

* The module does not classify circuits and does not re-derive the engine's
  balance. It is a **view** over the same arithmetic.
* A residual is never redistributed onto devices. That would invent attribution
  the evidence does not support.
* `PARTIALLY_UNDERSTOOD` is a circuit class, not a residual state; the circuit
  layer consumes the residual, the residual never consumes a class (see
  `docs/confidence_model.md` and `docs/association_and_topology.md`).
* A surplus (negative residual) is a contradiction of the accounting, never
  invented load. It is published with its sign so a reader can see which
  direction the books are wrong in, and it never promotes a classification.
