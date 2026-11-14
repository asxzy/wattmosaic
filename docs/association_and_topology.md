# Association and topology discovery (v2)

Reference for the two modules that turn repeated events into topology claims:

* `engine/wattmosaic_engine/disaggregation/association.py` — device → CT
  association (which CT observes this device?)
* `engine/wattmosaic_engine/electrical/hierarchy.py` — measurement overlap and
  electrical parent/child (which CT feeds which?)

Both emit `TopologyRelation` (`contracts.py:767`) with
`status=CANDIDATE` and `requires_confirmation=True`. Neither ever promotes a
claim to fact; confirmation is a user act (`docs/configuration.md` §"Topology
confirmation").

---

## 1. Four relation kinds that never imply one another

`RelationKind` (`contracts.py:255`):

| Kind | Claim | Emitted by |
| --- | --- | --- |
| `DEVICE_CT_ASSOCIATION` | a device's power is (partly) observable through this CT | `AssociationFinder.analyze` |
| `CT_MEASUREMENT_OVERLAP` | two CTs measure overlapping conductors — a duplicate or subset, **not** parenthood | `HierarchyFinder.overlaps` |
| `CIRCUIT_MEMBERSHIP` | a device or CT belongs to a circuit | circuit layer (see `docs/residual_analysis.md`, `docs/confidence_model.md`) |
| `ELECTRICAL_PARENT_CHILD` | a confirmed wiring containment relation between two CTs | `HierarchyFinder.infer` |

Storing them in one typed edge is what keeps them separate: nothing can set a
parent pointer and thereby assert membership, and "two CTs on one device" stays
a measurement overlap rather than becoming parenthood.

The practical consequence, and the reason the Hot Tub case is a mandatory
regression: **two legs of one appliance respond identically, and identical
response is still not containment.** `HierarchyFinder.overlaps` emits
`implies_parenthood: False` explicitly (`hierarchy.py:551`).

---

## 2. Association

### 2.1 The evidence object *is* the argument

`evidence` on an emitted association (`association.py:1020`, payload built at
`:1099`):

| Key | Contents |
| --- | --- |
| `verdict` | `supported` / `split` / `ambiguous` / `single_event` |
| `stats` | every number the decision used: event counts, median/MAD deltas, lag, stability, basis, multiplier, contribution, noise |
| `supporting` | each clean isolated event that agreed, one entry per event |
| `conflicting` | each clean isolated event that disagreed, **with its reason** — never discarded, never averaged into the support |
| `overlap` | how this CT relates to the other CTs that also see this device |
| `peer_ct_ids` | every CT emitted for this device |
| `algorithm_version`, `config` | reproducibility metadata |
| `isolation` | `{"clean_status": "isolated", "rule": "isolated_only"}` |
| `split_with` | present only when joint events exist |
| `ambiguous_with` | present only when `verdict == "ambiguous"` |

`stats` is deliberately exhaustive so a reader can recompute the confidence
without trusting it (`association.py:1064-1105`).

### 2.2 The eight evidence rules

Each rule has a failing branch that is recorded, not a silent filter
(module docstring `association.py:26-77`):

1. **Only isolated events are evidence.** A contaminated event is counted as
   `contaminated_event` and excluded — exactly as `clean_attribution_inputs`
   prescribes. Nothing is deleted.
2. **Magnitude consistency.** The CT delta must agree with the device's own
   declared step in sign and size: `0 < scaled/device <= 1 + tolerance`. A CT
   that moves four times as much is a different load that happens to move at the
   same time (`magnitude_overshoot`).
3. **Repeatability.** A relation needs `min_events` independent isolated
   events. One coincidence cannot be told from one real event, so single-event
   support is capped at `SINGLE_EVENT_CAP` and labelled `single_event` even
   when the caller lowers `min_events`.
4. **Lag.** The response is searched over `0..max_lag_s`; late is not wrong, but
   a response at or beyond `max_lag_s` is withheld (`lag_exceeded`).
5. **Noise.** Median and MAD, never mean and standard deviation. One 5 kW spike
   among five 200 W events moves the mean by 960 W and the median by 0 W; the
   spike is kept as a conflicting observation.
6. **Basis compatibility.** A factor-adjusted delta is converted with the
   recorded multiplier before comparison, and the multiplier and basis go into
   the evidence. An `UNKNOWN` basis is refused outright
   (`basis_unestablished`): with unestablished semantics, multiplying and not
   multiplying both guess.
7. **Equal deltas prove overlap, not loads.** Both associations are emitted —
   both CTs really do observe the device — and the pair is marked
   `summing_allowed=False`. No independent device load is invented.
8. **Ambiguity is named.** `TopologyStatus` has no `AMBIGUOUS` member, so
   ambiguity lives in `evidence["verdict"]` plus a confidence multiplier, and
   the relation stays `CANDIDATE`.

### 2.3 `AssociationConfig` defaults (`association.py:245`)

| Field | Default | Effect |
| --- | --- | --- |
| `min_events` | `2` | independent isolated events before a relation may be emitted; `1` is diagnostics-only |
| `min_magnitude_w` | `10.0` | absolute floor for both the device step and the CT step |
| `max_lag_s` | `60.0` | largest device→CT delay searched; `>=` is withheld |
| `magnitude_tolerance_frac` | `0.10` | CT may carry `0 .. 1 + tolerance` of the device step |
| `stability_min` | `0.70` | minimum `1 - MAD(share)/median(share)` |
| `conflict_penalty` | `0.15` | confidence divisor per conflicting observation |
| `algorithm_version` | `"assoc@1.0"` | stamped onto every emitted relation |
| `noise_sigmas` | `3.0` | response significance in units of the CT's own baseline MAD |
| `lag_step_s` | `15.0` | lag-search resolution (the audited CT cadence) |

Thresholds and constants (`association.py:140-160`):
`MAD_TO_SIGMA = 1.4826`, `MAX_CONFIDENCE = 0.99`, `SINGLE_EVENT_CAP = 0.5`,
`SPLIT_MULTIPLIER = 0.75`, `AMBIGUOUS_MULTIPLIER = 0.25`,
`NOISE_SIGMAS = 3.0`,
`APPLIANCE_TOTAL_BASES = {FACTOR_ADJUSTED, MONITOR_REPORTED}`.

### 2.4 Confidence — exactly the documented product

`_confidence` (`association.py:1148`):

```
repeatability = 1 - 0.5 ** n_events           # 1 -> 0.50, 4 -> 0.94, 8 -> 0.996
lag_term      = 1 - 0.5 * min(1, lag_s / max_lag_s)   # 0 s -> 1.0, 60 s -> 0.5
              = 1.0 when max_lag_s == 0
ambiguity     = 1.0 (supported) / 0.75 (split) / 0.25 (ambiguous)
               / 1.0 (single_event)

confidence = min(MAX_CONFIDENCE,
                 repeatability * stability * basis_confidence * lag_term * ambiguity
                 / (1 + conflict_penalty * n_conflicts))
```

Then `_relation` clamps further: if the verdict is `single_event` or
`ambiguous`, `confidence = min(confidence, SINGLE_EVENT_CAP)`
(`association.py:1061`).

#### Worked numbers

The module docstring's `0.95` example (`association.py:99-102`) is a five-event
supported relation:

| Term | Value | Source |
| --- | --- | --- |
| `repeatability` | `1 - 0.5**5 = 0.96875` | `stats["n_events"] = 5` |
| `stability` | `0.98` | `stats["stability"]` |
| `basis_confidence` | `1.0` | established basis |
| `lag_term` | `1.0` | `stats["lag_s"] ≈ 0` |
| `ambiguity` | `1.0` | verdict `supported` |
| `n_conflicts` | `0` | `stats["n_conflicting"] = 0` |

`0.96875 × 0.98 = 0.949375` → reported as `≈ 0.95`. The docstring's shorthand
`0.9688 * 0.98 = 0.949` is that same product at four decimals.

Two checks a reader can run immediately:

* `n_events` is the count of **every** event the pair was judged on
  (`n_supporting + n_conflicting + n_joint`), not just the supporting ones
  (`association.py:1065`), so a device with many contradictions gets a high
  `repeatability` and a large denominator — the product still falls.
* `basis_confidence` is the **weakest** accepted event's factor confidence
  (`_note_basis`, `association.py:837`): one `UNKNOWN`-basis event cannot be
  laundered by four established ones. In practice `UNKNOWN` is refused during
  partitioning, so the term is `1.0` on any emitted relation — it is a guard,
  not a tuner.

The number is *never* 1.0: `MAX_CONFIDENCE = 0.99`, because a candidate
association is a proposal.

### 2.5 Verdicts

`_relation` (`association.py:1040-1052`):

| Condition | Verdict |
| --- | --- |
| no `supporting` rows at all (only shared events) | `ambiguous` |
| `split_with` non-empty | `split` |
| exactly one supporting event | `single_event` |
| otherwise | `supported` |

`split` means solo events exist *and* the joint response can be apportioned;
`ambiguous` means they never do, so the relation is a proposal that must not be
used as an independent power estimate.

### 2.6 Overlap between a device's own CTs

`_overlap_report` (`association.py:941`) returns `(overlap, budget_exceeded)`
with four distinct outcomes, in this order:

| Condition | `overlap.reason` | `summing_allowed` |
| --- | --- | --- |
| any candidate's basis is in `APPLIANCE_TOTAL_BASES` | `appliance_total_estimate_overlaps_sibling` | `False` |
| any candidate has no `supporting` rows | `contribution_never_isolated` | `False` |
| `_has_duplicate` — same median level **and** same share **and** matching per-event deltas | `duplicate_measurement_of_one_device` | `False` |
| plain contributions sum to `> 1 + tolerance` | `budget_exceeded` → **nothing emitted** | — |
| otherwise | `independent_contributions` | `True` |

`_has_duplicate` (`association.py:991`) requires all three conditions on
purpose: same level alone is legitimate for two legs of a balanced load, and
same share alone is legitimate for two unrelated circuits.

### 2.7 Withheld report

`withheld(observations, registry, now)` (`association.py:1210`) returns
`device -> reason -> n` covering the *whole* decision, including events excluded
during partitioning. Reason codes (`association.py:173-183`):
`contaminated_event`, `no_ct_evidence`, `too_few_events`, `basis_unestablished`,
`opposite_direction`, `magnitude_overshoot`, `response_within_noise`,
`lag_exceeded`, `insufficient_magnitude`, `unstable`, `budget_exceeded`.

Counts are observations for observation-level reasons and decisions for
pair-level ones, so `budget_exceeded` is `1` for a device whose CTs collectively
disprove a relation while `opposite_direction` is the number of contradicting
events.

### 2.8 Identity, persistence, withdrawal

* `_relation_id(device_id, ct_id, algorithm_version)` → `dca_` + 24 hex of
  `sha256("device_ct_association|<device>|<ct>|<version>")`
  (`association.py:1246`). Name-free, so `persist` is idempotent and a re-run
  updates rather than duplicates.
* `persist(relations, database)` (`association.py:1259`) writes through
  `insert_topology_relation` and returns the count. A withdrawn relation is
  stored with its `valid_to` and `REJECTED` status.
* `withdraw(relation, reason, now)` (`association.py:1174`) requires a non-empty
  reason, sets `status=REJECTED`, `valid_to=now` (never `valid_from`), and keeps
  the evidence readable, adding `evidence["withdrawal"] = {reason, at,
  previous_status}`.

### 2.9 Orientation

A backwards clamp reports `-delta` where the device reports `+delta`. The module
does **not** take an absolute value: the reversed step is withheld with
`opposite_direction`. Orientation is corrected upstream, exactly once, by the
declared `Polarity.INVERTED` passed to `observe(..., polarity=...)`
(`association.py:645-652`). Silently flipping the sign would hide a wiring fault
and make "the device turned on and the CT fell" indistinguishable from a
neighbouring load switching off.

---

## 3. Hierarchy

### 3.1 The evidence bar

`HierarchyFinder.infer` (`hierarchy.py:568`) proposes a parent/child edge only
when **all** of the following hold. Each can be measured and each can fail
(`hierarchy.py:33-52`):

| Requirement | Check | Refusal reason |
| --- | --- | --- |
| measurement scope supplied | `series` non-empty | `no_measurement_scope_evidence` |
| device evidence exists | at least one association supplied | `no_device_evidence` |
| not an alias of itself | canonical ids differ | `alias_of_same_conductor` |
| basis established on both CTs | `require_scope_evidence` + `_basis_unknown` | `basis_unestablished` |
| independence | `>= min_independent_devices` distinct devices | `insufficient_independent_devices` |
| comparable instants | `>= MIN_COMPARABLE_INSTANTS` = 30 | `insufficient_comparable_instants` |
| containment | child never exceeds parent beyond tolerance | `no_scope_containment` |
| one direction only | not mutual | `ambiguous_scope_containment` / `mutual_containment` |
| operating conditions | `conditions >= 2` and `child_levels >= 2` | `single_operating_condition` |
| response consistency | `consistency >= min_response_consistency` | `inconsistent_response_across_conditions` |
| not one conductor | ratio spread `> 0.02` | `scaled_copy_of_one_conductor` |

`infer` accepts `circuits` and `residual_history` for interface stability and
then `del circuits, residual_history` (`hierarchy.py:586`): neither can establish
containment. Measurement scope is the only evidence that can place a level.

### 3.2 `HierarchyConfig` defaults (`hierarchy.py:112`)

| Field | Default | Meaning |
| --- | --- | --- |
| `min_independent_devices` | `2` | distinct devices responding through both CTs; `>= 2` enforced in `__post_init__` |
| `min_response_consistency` | `0.9` | fraction of operating conditions whose containment must hold |
| `max_power_violation_frac` | `0.02` | fraction of comparable instants where the child may exceed the parent |
| `require_scope_evidence` | `True` | an `UNKNOWN` basis blocks inference |
| `algorithm_version` | `"wp8-hierarchy-1.0.0"` | recorded on every emitted relation |

Geometry constants (`hierarchy.py:80-110`): `MIN_PAIR_ACTIVE_W = 200.0`,
`VIOLATION_TOL_W = 30.0`, `MIN_COMPARABLE_INSTANTS = 30`,
`MIN_CONDITION_INSTANTS = 5`, `CONDITION_QUANTUM_FRAC = 0.05`,
`RATIO_MIN_PARENT_W = 200.0`, `SCALED_COPY_RATIO_SPREAD_MAX = 0.02`.

Note the `algorithm_version` default is the module's own version string; the
audit pass overrides it (`audit.py:216`) — see `docs/limitations_v2.md`.

### 3.3 Series geometry

`_scope_geometry(parent, child, ...)` (`hierarchy.py:225`):

* comparable instants are those where **either** reading reaches
  `MIN_PAIR_ACTIVE_W` (idle circuits prove nothing and are excluded, not
  treated as zero);
* a violation is `child > parent + VIOLATION_TOL_W`; an instant where the child
  exceeds an active parent is a *violation*, never a skipped sample;
* operating conditions are coarse load bins of the parent
  (`CONDITION_QUANTUM_FRAC × peak`), each needing `MIN_CONDITION_INSTANTS`
  samples;
* per-condition ratio spread below `0.02` means the two series are one conductor
  with a factor, not a nest.

`_choose_direction` (`hierarchy.py:693`): both directions acceptable → mutual
containment (one conductor measured twice, or series that cannot separate) →
never guess a direction; neither → `no_scope_containment`.

### 3.4 Immediate parent only

`_emit` (`hierarchy.py:761`) keeps, per child, the candidate with the smallest
median remainder (the tightest container) and records every other one as
superseded with `not_the_immediate_parent`. A grandparent still contains the
child; it is simply not the wiring edge.

A pair already declared by the user is skipped (`declared_wins`); a declared
reverse direction produces a `REJECTED` edge with `contradicts_declared` rather
than an accepted one.

### 3.5 Emitted parent/child evidence

`_parent_child_relation` (`hierarchy.py:832`) records `devices`,
`independent_devices`, `comparable_instants`, `conditions`,
`response_consistency(_pct)`, `power_violation_frac(_pct)`,
`median_remainder_w`, `median_parent_w`, `median_child_w`,
`child_to_parent_ratio_spread`, `accounting` and `reason`.

`accounting` is explicit and is the point of the whole module: a parent already
contains this child, so adding the parent row and the child row together would
double count the child's power (`adds_totals: False`).

### 3.6 `validate` never drops a bad edge

`validate` (`hierarchy.py:897`) returns both `valid` and `rejected`
(`ValidationResult`), because "why was this parent refused" is part of the
answer. Rejection reasons:

`self_edge`, `unknown_ct`, `alias_self_edge`, `contradicts_declared`,
`duplicate_edge`, `cycle: a -> b -> c`, `declared_cycle: ...`.

A self edge is impossible to *construct* through `TopologyRelation` (the
contract rejects `subject_id == object_id`), so it is built with
`model_construct` (`_construct_rejected`, `hierarchy.py:388`) rather than lost.

### 3.7 Incomplete topology is a valid output

`infer` may return `[]` with a stated `report["reason"]`:
`no_device_evidence`, `no_measurement_scope_evidence`,
`insufficient_evidence`. An empty result with a reason is preferred to a guess.
`report` also carries `refused`, `undeterminable`, `superseded`,
`declared_wins`, `explanation` and `found`.

---

## 4. The Hot Tub case, stated as the regression it is

`tests/unit/test_hierarchy.py:332` (`test_hot_tub_equal_deltas_yield_overlap_only`)
supplies *perfectly nested-looking* series for CT1/CT2 on the tub: CT1 +250 W,
CT2 +250 W, ×2 declared. The correct answer is `CT_MEASUREMENT_OVERLAP` only —
no parent, no child — because there is **one** independent device, and one
device observed twice is an overlap, never parenthood.

`tests/unit/test_circuits.py:402` states the same fact from the circuit side:
both legs observe the tub, the factor already claims the whole appliance, so
they are one circuit (`ct_ids == {ct_hot_tub_l1, ct_hot_tub_l2}`) and not two
loads — and three cycles seen on two legs are three events, not six.

---

## 5. Boundaries worth stating

* A candidate is a proposal. `MAX_CONFIDENCE` is 0.99 and
  `requires_confirmation` is always `True` on an inferred relation.
* Correlation is not topography, and a bigger reading is not upstream. Both
  statements are enforced by a measurable check, not by a comment.
* Nothing here sums. `summing_allowed` is an *output*, never an assumption, and
  the default for any unestablished basis is "do not sum".
* Topology candidates persist across passes and are withdrawn, not deleted, so a
  historical reader can still ask what was claimed before the withdrawal.
