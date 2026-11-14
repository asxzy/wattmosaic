# Confidence model (v2)

Reference for how the engine reports confidence: independent dimensions, no
aggregate, and every number recomputable from published evidence.

The defect this replaces is a single blended score. A system that answers
"0.87" to *how sure are you?* cannot be audited, because the reader cannot tell
whether the 0.87 is about the watts, the CT wiring, the topology or the residual.
`ConfidenceVector` exists to make the dimension explicit.

---

## 1. `ConfidenceVector` (`contracts.py:481`)

| Field | Type | Meaning |
| --- | --- | --- |
| `power` | `float \| None` (0..1) | confidence in a device's watt value |
| `ct_association` | `float \| None` | confidence that a device is observed by a particular CT |
| `topology` | `float \| None` | confidence in the electrical parent/child structure |
| `circuit_classification` | `float \| None` | confidence in a circuit's dedicated/shared/partial classification |
| `residual` | `float \| None` | confidence in the residual decomposition |
| `method` | `str` (default `"unavailable"`) | *name* of the derivation, never a score |

`None` means "not assessed" and is distinct from `0.0`. `method="unavailable"`
means no dimension was derived at all — an honest statement, not an empty one.

`dimensions()` (`contracts.py:512`) returns only the dimensions actually
assessed, so a consumer cannot accidentally treat an unassessed dimension as
`0.0`.

The model has **no aggregate field, no `__float__` and no arithmetic**
(`contracts.py:484-487`). Merging the components into one weighted blend is the
defect the model exists to stop repeating. A consumer that wants one number must
say which dimension it means.

---

## 2. What the audit pass populates today

`AuditPass._record` (`disaggregation/audit.py:1724`) builds:

```python
ConfidenceVector(
    power=_bounded(float(result.confidence)),
    ct_association=_max_confidence(associations),
    circuit_classification=_circuit_confidence(circuits_here),
    method=f"audit:{self.config.algorithm_version}",
)
```

| Dimension | Source | Absent when |
| --- | --- | --- |
| `power` | the `InferenceResult.confidence`, bounded to `[0, 1]` (non-finite → `0.0`) | never (always populated) |
| `ct_association` | `max` over the device's `DEVICE_CT_ASSOCIATION` relation confidences | the device has no association |
| `circuit_classification` | `max` of each circuit's `metadata["classification_confidence"]` | none of the device's circuits has one |
| `topology` | **never populated by the audit pass** | always |
| `residual` | **never populated by the audit pass** | always |

The circuit layer *does* emit its own vector with exactly one dimension set
(`CircuitClassifier.classify`, `circuits.py:658`):

```python
ConfidenceVector(
    circuit_classification=confidence,
    method=self.config.algorithm_version,
)
```

This is a real limitation, stated in `docs/limitations_v2.md`: `topology` and
`residual` are declared dimensions with documented derivations available
(hierarchy evidence, residual trend sample count) but no code path currently
fills them on an `InferenceRecord`. A reader must not read absence as `0.0`.

---

## 3. Association confidence

Derivation and worked example: `docs/association_and_topology.md` §2.4.

```
confidence = min(0.99,
                 repeatability * stability * basis_confidence * lag_term * ambiguity
                 / (1 + 0.15 * n_conflicts))
```

with `repeatability = 1 - 0.5**n_events`, `lag_term = 1 - 0.5*(lag/max_lag)`,
`ambiguity ∈ {1.0, 0.75 (split), 0.25 (ambiguous)}`, and a further clamp to
`0.5` for `single_event`/`ambiguous`.

What it is: a **CT-association** confidence. It answers "is this CT observing
this device?", and it is explicitly *not* a device power score
(`association.py:80-84`).

Two invariants a reader can check:

* every term is visible in `evidence["stats"]`;
* the number can never be `1.0`, because a candidate association is a proposal
  and confirmation is the user's act.

---

## 4. Circuit-classification confidence

`CircuitClassifier._confidence` (`circuits.py:756`). Terms, each answering one
of the questions the class itself depends on:

| Term | Formula | Reading |
| --- | --- | --- |
| `repeat` | `1 - 0.5 ** isolated_events` | one event is 0.5, four are 0.94 |
| `stability` | the min CT-response consistency across devices | does the conductor respond consistently? |
| `fraction` | `clip(explained_fraction, 0, 1)` | how much of the scope the device(s) account for |
| `span` | `clip(span_s / dedicated_min_observation_s, 0, 1)` | has the scope been watched long enough? |
| `breadth` | `1 - 0.5 ** independent_devices` | one device is not shared |
| `residual` | `1.0` checked-and-clean, `0.9` unchecked, `0.5` contradictory | is the unexplained part of the scope small? |

Constants: `MAX_CONFIDENCE = 0.99`, `UNKNOWN_CAP = 0.5` (`circuits.py:130`,
`:134`).

### 4.1 Per-class formulas

| Class | Formula |
| --- | --- |
| `UNKNOWN` | `min(UNKNOWN_CAP, UNKNOWN_CAP * repeat * stability * fraction)` |
| `DEDICATED` | `min(MAX_CONFIDENCE, repeat * stability * fraction * span * residual)` |
| `SHARED` / `PARTIALLY_UNDERSTOOD` | `min(MAX_CONFIDENCE, device_repeat * device_stability * device_fraction * breadth * residual)` where each `device_*` is the **min** over independently-evidenced devices, and `breadth = 1 - 0.5**len(independent)` |

`UNKNOWN` is capped at `0.5` on purpose: if the evidence were stronger than a
coin flip, the classifier would have named a class. The cap keeps a state of
ignorance from looking better than a named verdict.

If a shared classification has no independently-evidenced device, the score is
`0.0` — there is nothing to be confident *about*.

### 4.2 The `PARTIALLY_UNDERSTOOD` exception

For that class alone, `residual_term = max(0.1, min(1.0, fraction))`
(`circuits.py:794`): the residual is penalised by its **measured size**, not by
the fact that it exists.

The reasoning matters: the residual is what makes the class *true*. A
persistent unexplained load is why the circuit is partially understood, not
evidence that the verdict is wrong. Treating the residual as failure would make
the honest verdict less credible than the over-claim (`DEDICATED`) it replaces.

### 4.3 The `UNKNOWN` cap is not a contradiction

A `DEDICATED` circuit can be `CONFIRMED` only when the residual was *checked and
clean*; `DEDICATED` + `CANDIDATE` is the honest state for "everything else
passed but nobody measured the residual" (`circuits.py:605-620`, where
`confirmed = residual_measured and not residual_contradicts`). Absence of a
contradiction is not a contradiction, but it is also not a confirmation.

---

## 5. Other confidence-shaped numbers, and what they are not

Easy to confuse with a `ConfidenceVector` dimension. They are separate fields
with separate meanings:

| Field | Where | What it is |
| --- | --- | --- |
| `MeasurementBasis` confidence (`1.0` / `0.25` / `0.0`) | `factors.FactorResolution.confidence` | confidence that the *factor interpretation* is established |
| `TopologyRelation.confidence` | `contracts.py:790` | per-relation confidence, deliberately not a device power score |
| `ResidualTrend.confidence` | `residual_power.py:592` | `n / (n + 8)`: how much history supports the *typical* residual |
| `Circuit.metadata["classification_confidence"]` | `circuits.py` | the same number as the `circuit_classification` dimension, stored beside the circuit |
| `InferenceResult.confidence` | pre-existing inference output | the input to the `power` dimension |

`TopologyRelation.confidence` and `Circuit.metadata[...]` are the *sources* the
audit pass copies into the vector; they are not additional dimensions.

---

## 6. Calibration: what these numbers are and are not

These are **not** probabilities. Nothing in the codebase converts them to a
frequency, and no test asserts that a confidence of `0.9` is right 90 % of the
time.

What they *are*:

* **bounded** — every dimension is `[0, 1]`, enforced by `Field(ge=0, le=1)`;
* **recomputable** — each term is present in published evidence
  (`evidence["stats"]` for associations, `metadata["evidence"]` for circuits);
* **reproducible** — the same raw inputs and the same `algorithm_version`
  produce the same number, with no randomness in the analysis path;
* **monotone where it should be** — more independent events raise
  `repeatability`/`breadth`; more contradicting observations lower association
  confidence via the conflict denominator; a larger unexplained share lowers
  `PARTIALLY_UNDERSTOOD` confidence.

What they are *not*:

* comparable across dimensions — `ct_association=0.7` and `power=0.7` are
  answers to different questions and must not be averaged or compared;
* a substitute for confirmation — every inferred artifact is
  `status=CANDIDATE` with `requires_confirmation=True`;
* a ceiling on correctness — `0.99` is a cap on *claims*, not a measurement of
  accuracy.

The honest summary: the numbers are an ordered, explainable encoding of how much
evidence supports a specific claim. They are not calibrated probabilities, and
`docs/limitations_v2.md` records that no real-house calibration exists.
