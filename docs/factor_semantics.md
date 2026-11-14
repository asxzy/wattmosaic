# Measurement basis and factor semantics (v2)

Reference for `engine/wattmosaic_engine/electrical/factors.py` — how a CT
reading becomes a watt value, and how the engine records *why* it applied (or
did not apply) a multiplier.

This is the contract that closes the Refoss double-count: the same published
digits can mean a single-leg measurement, an appliance estimate the monitor
already multiplied, or a value the user expects the engine to multiply. The
arithmetic differs even though the number does not.

---

## 1. The five bases

`MeasurementBasis` (`contracts.py:187`) answers *"what kind of instrument
produced these watts"*. It is independent of `Source` (which answers *"how was
the value obtained"*): a `DIRECT_MEASURED` result may be `FACTOR_ADJUSTED`, and
a `MODEL_ESTIMATED` result can never be `CT_MEASURED`.

| Basis | Meaning | Engine arithmetic |
| --- | --- | --- |
| `CT_MEASURED` | a physical CT reading of the conductor, used as reported | ×1 (`applied_multiplier = 1.0`) |
| `MONITOR_REPORTED` | the monitor already published the whole load | ×1 — multiplying again double counts |
| `FACTOR_ADJUSTED` | a per-circuit factor was applied by the engine | ×`multiplier`, exactly once |
| `ESTIMATED_APPLIANCE_TOTAL` | a model/prior total, not a reading | ×1; never subtracted from a measurement (see `docs/residual_analysis.md`) |
| `UNKNOWN` | the basis is not established | ×1 **and confidence < 1**; no interpretation is claimed |

`UNKNOWN` is not "no factor". It is "the metadata cannot tell us whether a
factor belongs here", and the engine refuses to guess in either direction —
multiplying and *not* multiplying are both guesses.

Confidence constants (`electrical/factors.py:88`, `:94`, `:99`):

| Constant | Value | Used when |
| --- | --- | --- |
| `ESTABLISHED_CONFIDENCE` | `1.0` | the basis came from explicit evidence |
| `UNKNOWN_CONFIDENCE` | `0.25` | missing/gap/pre-history metadata, or `applied_by="unknown"` |
| `CONFLICT_CONFIDENCE` | `0.0` | metadata is self-contradictory; no arithmetic branch is supportable |

`0.25` is deliberately non-zero: the *reading* may still be a valid conductor
value, it is only the interpretation that is unverified. `0.0` is deliberately
zero: a conflict means applying and skipping both report a wrong total, so
consumers must refuse rather than pick one.

---

## 2. One derivation decides both the basis and the arithmetic

`classify_basis(...)` (`electrical/factors.py:378`) is the public projection that returns
only the basis. `FactorRegistry.resolve(...)` (`electrical/factors.py:533`) adds the CT,
timestamp, interval and explanation. Both are computed by the *same* private
`_derive(...)` (`electrical/factors.py:238`), so the named basis and the applied
multiplier can never disagree.

`classify_basis`'s signature is itself a design rule: `phase`, `ct_type`,
`name` and `nominal_voltage` are **absent** on purpose, so a caller cannot
smuggle an inference in. A double-pole 240 V channel is exactly the case where
inferring ×2 would double count.

### 2.1 `_derive` decision table

First match wins:

| Condition | Basis | `applied_multiplier` | `reported_multiplier` | `reason_code` | confidence |
| --- | --- | --- | --- | --- | --- |
| `conflict` argument set | `UNKNOWN` | 1.0 | 1.0 | `factor_conflict` | 0.0 |
| no factor, `estimated_appliance_total=True` | `ESTIMATED_APPLIANCE_TOTAL` | 1.0 | 1.0 | `estimated_appliance_total` | 1.0 |
| no factor, `requires_factor`, gap in a history | `UNKNOWN` | 1.0 | 1.0 | `factor_gap` | 0.25 |
| no factor, `requires_factor`, before first interval | `UNKNOWN` | 1.0 | 1.0 | `factor_not_yet_declared` | 0.25 |
| no factor, `requires_factor`, no history at all | `UNKNOWN` | 1.0 | 1.0 | `factor_missing` | 0.25 |
| no factor, no `requires_factor`, gap in a history | `UNKNOWN` | 1.0 | 1.0 | `factor_gap` | 0.25 |
| no factor, no `requires_factor`, before first interval | `UNKNOWN` | 1.0 | 1.0 | `factor_not_yet_declared` | 0.25 |
| no factor, no history at all | `CT_MEASURED` | 1.0 | 1.0 | `no_factor_declared` | 1.0 |
| factor `applied_by="unknown"` | `UNKNOWN` | 1.0 | 1.0 | `factor_unknown_ownership` | 0.25 |
| factor `applied_by="monitor"` | `MONITOR_REPORTED` | **1.0** | `multiplier` | `factor_applied_by_monitor` | 1.0 |
| `applied_by="user"` and `multiplier == 1.0` | `CT_MEASURED` | 1.0 | 1.0 | `factor_declared_one` | 1.0 |
| `applied_by="user"` and `multiplier != 1.0` | `FACTOR_ADJUSTED` | `multiplier` | 1.0 | `factor_applied_by_user` | 1.0 |

Two rows are worth reading twice:

* **`monitor` with a declared ×1 still resolves to `MONITOR_REPORTED`**, not
  `CT_MEASURED`. `_derive` returns before the `multiplier == 1.0` branch
  (`electrical/factors.py:317-330` vs `:331`). The two *numbers* coincide; the *claim*
  does not — a monitor-published channel is still a whole-load estimate.
* **`user` with a declared ×1 resolves to `CT_MEASURED`, not
  `FACTOR_ADJUSTED`** (`electrical/factors.py:331`). An explicit ×1 is a deliberate
  "reading is the conductor value", not an adjustment.

### 2.2 "If you see X, the basis is Y"

Use this to read a `/status` or audit payload without the source:

| What you see | What it means |
| --- | --- |
| `basis: "ct_measured"`, `applied_multiplier: 1.0` | plain conductor reading; summable with a sibling leg |
| `basis: "monitor_reported"`, `reported_multiplier: 2.0`, `applied_multiplier: 1.0` | monitor already multiplied; this value already claims the whole appliance |
| `basis: "factor_adjusted"`, `applied_multiplier: 2.0` | user declaration in force; engine multiplies the raw value once |
| `basis: "unknown"`, `confidence: 0.25` | metadata gap or unknown ownership; **no** arithmetic is licensed |
| `basis: "unknown"`, `confidence: 0.0`, `conflict: true` | self-contradictory metadata; refuse a total |
| `basis: "estimated_appliance_total"` | a model/prior, not a measurement |

---

## 3. Intervals: a reading is never scaled with today's declaration

`MeasurementFactor` (`contracts.py:590`) carries `effective_from` (inclusive),
`effective_to` (exclusive, `None` = still in force), `applied_by`
(`monitor`/`user`/`unknown`), `declared_by` (`user`/`import`/`default`) and an
optional `note`.

`validate_factor_intervals` (`contracts.py:642`) rejects **overlapping**
intervals for the same `ct_id`. With two covering intervals a lookup would have
to pick one arbitrarily, and a historical reconstruction would be a guess.
Different CTs may overlap freely.

`FactorRegistry.factor_at(ct_id, timestamp)` (`electrical/factors.py:515`) returns the
single covering interval or `None`. Intervals are sorted by start; the first
interval that starts after the instant ends the search. `None` means "no
metadata at this instant" — never "the next interval", never an implicit ×1.

`FactorRegistry.__post_init__` re-validates the rows (`electrical/factors.py:484`), so a
corrupt store still cannot make the registry answer with an overlapping
interval.

### 3.1 Four distinct "no factor" outcomes

They look the same in a diff and mean different things:

| Situation | `reason_code` | Basis | Why not ×1 |
| --- | --- | --- | --- |
| no factor has ever been declared for this CT | `no_factor_declared` | `CT_MEASURED` | there is nothing to apply; ×1 is the truth |
| channel declared to need a factor, no interval exists | `factor_missing` | `UNKNOWN` | ×1 would silently report half the appliance |
| a history exists, but this instant falls between intervals | `factor_gap` | `UNKNOWN` | the existence of a history proves the channel is factor-managed; a gap is *no metadata* |
| the sample predates the first interval | `factor_not_yet_declared` | `UNKNOWN` | no declaration covers it |
| the interval does not record who applied it | `factor_unknown_ownership` | `UNKNOWN` | multiplying and not multiplying both guess |

A declared ×1 is **not** a gap: `factor_declared_one` → `CT_MEASURED`.

### 3.2 Historical reconstruction

`FactorRegistry.basis_history(ct_id, timestamps)` (`electrical/factors.py:647`) returns
one `BasisSample` per supplied instant, resolved against the interval covering
*that* sample. A later factor change cannot rewrite an earlier reading. This is
the mechanism that makes "no retroactive factor application" structural rather
than a policy.

---

## 4. The single scaling site

`scale_reading(reading_value, ct_id, timestamp, registry)` (`electrical/factors.py:676`)
takes a **raw** reading and returns `(scaled_value, multiplier_applied, basis)`.
It only ever multiplies by `applied_multiplier`, which is always `1.0` when the
monitor already applied the factor. A `None` reading returns
`(None, 1.0, basis)`.

`applied_multiplier` and `reported_multiplier` are never both non-1.0
(`FactorResolution` docstring, `electrical/factors.py:131`). That is what makes double
application structurally impossible rather than merely discouraged: only
`applied_multiplier` is ever consumed.

The module docstring (`electrical/factors.py:52-73`) lists the two other multiplication
sites honestly, because "one place" would be a false claim:

* `disaggregation/association.py::AssociationFinder._classify` scales the CT
  *delta* once and records the multiplier it used; the scaled value never feeds
  back in.
* `models/residual_power.py::ResidualAnalyzer._counted_contributions` applies a
  declared per-device *unit conversion* once and records `conversions`.

None of them accepts the output of another as input, and the registry never
mutates anything.

`TopologyGraph.scale_w` (`electrical/topology.py:646`) delegates to
`scale_reading` when a registry and timestamp are available, and otherwise
falls back to the legacy `CTChannel.measurement_multiplier` scalar — the
documented "no timestamp, so 'now' is ambiguous" behaviour.

---

## 5. Two CTs on one appliance are an overlap, never two loads

`resolve_factor_ownership(...)` (`electrical/factors.py:762`) answers which appliance a
factor belongs to and whether the group may be summed.

`timestamp` is **required and keyword-only**: resolving a historical sample
against "now" is the retroactive application the module forbids.

Peers come from exactly two explicit declarations: the declared
`double_pole_pairs` partner, and any channel wired to the same `device_id`.
Equal deltas alone never create a peer.

`FactorOwnership.appliance_basis` resolution order (`electrical/factors.py:806-870`):

1. `evidence_conflict` supplied → `UNKNOWN` / `evidence_conflict`
2. the factor says `monitor` but `CTChannel.measurement_multiplier != 1.0` →
   `UNKNOWN` / `factor_double_application_risk` (`double_application_risk=True`)
3. any member has self-contradictory metadata → `UNKNOWN` / `factor_conflict`
4. any member is `UNKNOWN` → `UNKNOWN` / `factor_missing` or `factor_gap`
5. any member is `FACTOR_ADJUSTED` → `FACTOR_ADJUSTED` /
   `appliance_factor_adjusted_overlap`
6. any member is `MONITOR_REPORTED` → `MONITOR_REPORTED` /
   `appliance_monitor_reported_overlap`
7. any member is `ESTIMATED_APPLIANCE_TOTAL` → that basis
8. otherwise → `CT_MEASURED` / `appliance_legs_summed` (or
   `no_factor_declared` when there is no peer)

`summing_allowed` is `False` for every basis except `CT_MEASURED`
(`_UNESTABLISHED`, `electrical/factors.py:748`). `explainable` is `False` only for
`UNKNOWN`. Group confidence is the minimum member confidence, forced to `0.0`
when any conflict is present. `relation` is `CT_MEASUREMENT_OVERLAP` when a peer
exists, else `None`.

---

## 6. Worked example: the mandatory Hot Tub

`tools/simulator/hot_tub.py` ships the regression dataset
(`hot_tub.py:1-22`, `CT1_BEFORE_W`…`HA_AFTER_W` at `:62-71`):

| Observation | Before | After | Delta |
| --- | ---: | ---: | ---: |
| Hot Tub HA power | 0 W | 500 W | +500 W |
| CT1 (L1 conductor) | 1000 W | 1250 W | +250 W |
| CT2 (L2 conductor) | 900 W | 1150 W | +250 W |

### 6.1 Reading it correctly (`hot_tub_factor_two`)

CT1 carries a user-declared ×2, CT2 is declared ×1.

* CT1: `applied_by="user"`, `multiplier=2.0` → `FACTOR_ADJUSTED`,
  `applied_multiplier=2.0`. The engine computes `250 W × 2 = 500 W`.
* CT2: `applied_by="user"`, `multiplier=1.0` → `CT_MEASURED`.
  `appliance_basis` is still `FACTOR_ADJUSTED` for the group, and
  `summing_allowed=False`.
* Answer: **500 W**, from `2 × 250`. It matches the HA entity's own +500 W.

### 6.2 Two wrong readings of the same table

| Reading | Arithmetic | Reported | What went wrong |
| --- | --- | --- | --- |
| sum both CT deltas | `250 + 250` | **750 W** | CT1's factor already claims the whole appliance; adding CT2 counts one leg twice |
| multiply the published CT1 value again | `1250 − 1000 = 250`, `250 × 2` treated as raw | **1000 W** | the ×2 was applied a second time |

The fixture names both explicitly: `double_count_if_summed_w: 750.0` in
`hot_tub_factor_two`'s expected details (`hot_tub.py:585`), and
`hot_tub_factor_monitor_published` — the same appliance with the ×2 owned by
the monitor — documents that "a consumer that multiplies again reports 1000 W
for a 500 W appliance" (`hot_tub.py:591-596`).

The important half is that **the arithmetic can still sum to 500 W and be the
wrong answer**. In `hot_tub_factor_missing` there is no factor metadata at all;
`250 + 250` happens to equal the HA step, and the correct verdict is still
`UNKNOWN` / refuse. `tests/unit/test_factors.py:688` asserts the *basis*
refuses rather than the numbers differing — a fixture set containing only the
pass cases could be satisfied by "add every CT", which is exactly the double
count the module exists to prevent.

### 6.3 The ten variants

`HOT_TUB_BUILDERS` / `HOT_TUB_VARIANTS` (`hot_tub.py:933`, `:947`); the table
of what each proves is maintained in `docs/testing.md` §7.3.

| Variant | Declared metadata | Expected basis | Summable |
| --- | --- | --- | --- |
| `factor_one` | ×1 on both legs | `ct_measured` | yes |
| `factor_two` | user ×2 on CT1, ×1 on CT2 | `factor_adjusted` | no |
| `monitor_published` | monitor ×2 on CT1, ×1 on CT2 | `monitor_reported` | no |
| `factor_changes_midhistory` | ×1 then ×2 across the step | `factor_adjusted` | no |
| `factor_missing` | leg conductor, no intervals | `unknown` | no |
| `factor_incorrect_metadata` | user ×2 **and** monitor-published | `unknown` (conflict) | no |
| `multiple_factor_cts` | ×2 on both CTs | `factor_adjusted` | no |
| `independent_physical_cts` | identical numbers, genuinely separate loads | `ct_measured` | yes |
| `noisy_unbalanced_legs` | ×1, observed asymmetry | `ct_measured` | yes |
| `inconsistent_ha_vs_ct` | HA power 500 W vs state `off` | `unknown` (conflict) | no |

`independent_physical_cts` is the necessary counter-example: it has the same
printed numbers as `factor_two` and the opposite correct answer. Without it, a
consumer could pass every test by hard-coding "never sum".

---

## 7. Boundaries worth stating

* The engine never rewrites a stored reading. `scale_reading` is a *read-path*
  transform; `basis_history` reconstructs what would have been applied.
* Factor changes are not retroactive, and a gap is not a silent ×1. Both are
  enforced by `factor_at`, not by convention.
* `UNKNOWN` propagates. It is refused by association
  (`basis_unestablished`), by residual arithmetic (`unknown_scope_basis`), and
  by hierarchy (`basis_unestablished`) rather than being coerced to ×1.
* There is no "confidence-weighted" factor. A basis is established or it is
  not; the `0.25` factor records doubt about the interpretation, it never
  licenses a partial multiplication.
