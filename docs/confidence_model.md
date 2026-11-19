# Confidence model (contract 3.0)

Reference for how the engine reports confidence: independent components, no
aggregate, and every number recomputable from published evidence. The
authoritative definition is
[HB §9.6](../docs/specs/realtime-topology-disaggregation/handbook.md) and the
[type definition](../docs/specs/realtime-topology-disaggregation/interfaces.md)
in `contracts/foundation.py` (`ConfidenceVectorV3`).

The defect this replaces is a single blended score. A system that answers
"0.87" to *how sure are you?* cannot be audited: the reader cannot tell whether
the number is about the watts, the endpoint wiring, the topology or the
residual. `ConfidenceVectorV3` makes the dimension explicit.

---

## 1. `ConfidenceVectorV3` (`contracts/foundation.py`)

| Component | Type | Meaning |
| --- | --- | --- |
| `measurement_quality` | `float \| None` (0..1) | quality of the reader samples |
| `event_association` | `float \| None` | confidence the event matches the subject |
| `model_fit` | `float \| None` | validation quality of the model |
| `topology_consistency` | `float \| None` | confidence in the typed edge structure |
| `identifiability` | `float \| None` | confidence the value is uniquely determined |
| `sample_coverage` | `float \| None` | how much history supports the claim |
| `freshness` | `float \| None` | age of the evidence |

`None` means "not assessed" and is distinct from `0.0`. The model has **no
aggregate field, no `__float__` and no arithmetic** — merging the components
into one weighted blend is the defect it exists to stop. A consumer that wants
one number must name the component it means (DEC-24).

## 2. What the inference path populates

Different components are filled by different stages: some come from the joint
solver and event matching, some from topology and model validation, some are
left `None` where no derivation exists yet. A reader must not treat an absent
component as `0.0`. Evidence refs on the estimate point to the samples, events
and edges that justify each assessed component.

## 3. What these numbers are and are not

They are **not** probabilities. Nothing converts them to a frequency, and no
test asserts that a component of `0.9` is right 90 % of the time.

What they *are*:

- **bounded** — every component is `[0, 1]`, enforced by `Field(ge=0, le=1)`;
- **recomputable** — inputs are cited by evidence refs on the estimate;
- **reproducible** — the same inputs produce the same numbers with no randomness;
- **directional** — more independent evidence raises coverage/association;
  contradictions and staleness lower the relevant component.

What they are *not*:

- comparable across components (association and power confidence answer
  different questions and must not be averaged);
- a substitute for confirmation — inferred artifacts carry a lifecycle status
  and require a user act to become confirmed;
- a ceiling on correctness — `0.99` caps *claims*, not measured accuracy.

## 4. Concrete algorithms

Derivations for the individual evidence terms (association confidence, circuit
classification confidence, isolation verdicts) are in the
[design spec](../docs/specs/realtime-topology-disaggregation/design.md) and the
overview in [`docs/algorithms.md`](algorithms.md). The
[handbook §9.6](../docs/specs/realtime-topology-disaggregation/handbook.md)
states the invariants each component must satisfy.
