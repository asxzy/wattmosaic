# Residual analysis (contract 3.0)

Reference for the residual as a first-class, **signed** quantity: what was
measured, whether the arithmetic is licensed, how it splits into baseline and
dynamic parts, and what a refusal means. The normative definition is HB §3.3
and §9.4; the algorithm design is in the
[design spec](../docs/specs/realtime-topology-disaggregation/design.md) and the
shapes in the
[interfaces spec](../docs/specs/realtime-topology-disaggregation/interfaces.md).

The whole-home balance (`accounted + unknown_residual == main_power`) and the
per-reader residual are both reported. Unexplained load on an endpoint is a
**Reader Residual** row — never a fabricated device. This module adds a typed
view over that arithmetic without re-deriving the engine's balance.

---

## 1. The core identity

```text
Residual(t) = EndpointPower(t) − Σ counted contributions(t)
```

The subtraction is the only one that produces a residual. It applies:

- no `max(0.0, ...)`,
- no `abs()`,
- no epsilon that would hide a real unexplained load.

A negative residual is a real observation (accounted power exceeds the measured
total) and keeps its sign. Only an exactly-zero residual is "near zero"; a 1 W
residual is genuinely positive. There is no tolerance that moves a value toward
zero and hides a finding.

## 2. Validity: checks before any subtraction

A number is produced only after the scope can be computed:

- **finite circuit power** — a non-numeric or non-finite watt value is refused,
  not coerced. A numeric string in a watt slot is a caller bug, not a reading.
- **one conductor** — a scope spanning several channels is refused; two clamps
  may be the legs of one 240 V load (whose total is their sum) or two monitors on
  one wire (a double count), and nothing in the evidence distinguishes them. A
  multi-endpoint scope is therefore reported as a named refusal, not a guessed
  number.
- **known basis** — contributions may be subtracted only if their measurement
  bases are comparable. Same basis is comparable; two physical equivalences
  exist (a monitor's reported total and a factor-adjusted total); an estimate
  may be *reported* but never subtracted from a reading. Compatibility is
  deliberately not transitive.
- **each conductor once** — duplicate endpoints and nested scopes are removed
  before summing, so a doubly-counted clamp cannot inflate the residual.

Every refusal carries a stable reason code, never a zero residual.

## 3. Baseline vs dynamic

The residual's baseline is the **median of recent residual observations**, not
of circuit totals. `dynamic = current − baseline`; the two add back to the total.
With no window there is no dynamic claim: the current value is the baseline and
the dynamic part is `0.0`, with an evidence note saying the estimator was
current-only.

The median is used because residual windows routinely contain a minority of
large excursions (a compressor start, a meter glitch) that would move a mean by
kilowatts while leaving the median untouched. A robust central tendency is the
only estimator that does not turn one spike into a permanent baseline.

## 4. Resolution is a legitimate permanent state

`resolved = |residual| <= 1e-9`; otherwise `unresolved_since` preserves the age
of the question. An unexplained residual may stay unexplained forever — that is
a valid, possibly permanent outcome and is never rendered as `0 W`. A trend
confidence grows with the number of observations (`n/(n+8)`) and is `None` with
no history, never a fabricated `0.0` or `1.0`.

## 5. Explaining part of a residual

A decomposition subtracts candidates from the latest residual only:

| Condition | Result |
| --- | --- |
| no history | refused `no_residual_history` — **not** `0 W` |
| negative residual | refused — a positive candidate cannot explain a deficit |
| candidates exceed the residual | conflict — the claim is a contradiction |
| exact match | resolved |
| otherwise | partially explained, with the remainder kept |

Candidates that are not numbers, not finite, or not positive loads are excluded
with a reason. Partial explanation is normal; the before/after pair is always in
evidence so a shrink is visible. A negative residual is a contradiction of the
accounting, never invented load.

## 6. What the analysis path computes

The audit/analysis pass computes one residual per single-conductor scope and
refuses the rest (`multi_ct_scope`, `no_scope_reading`, `unknown_scope_basis`).
A reanalysis window has no live readings, so such a scope is reported
`no_scope_reading` rather than computed from a stale value. Results are persisted
as versioned evidence so `unresolved_since` and a long trend survive retention;
pruning never deletes them.

## 7. Boundaries worth stating

- The residual module does not classify circuits and does not re-derive the
  engine's balance; it is a typed view over the same arithmetic.
- A residual is never redistributed onto devices — that would invent attribution
  the evidence does not support.
- Unknown load gets its own competitor in the joint solve and its own profile /
  Reader Residual row (HB §7.5, §9.4), not a fabricated device.
- A surplus (negative residual) is published with its sign so a reader can see
  which direction the books are wrong in; it never promotes a classification.
