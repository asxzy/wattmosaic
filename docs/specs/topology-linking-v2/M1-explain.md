# M1: explain-away and multi-child step matching

Create `engine/wattmosaic_engine/electrical/topology_explain.py` and
`tests/unit/test_topology_explain.py`. Edit no other file.

## Context

`engine/wattmosaic_engine/electrical/topology_inference.py` has
`_classify_step(parent_move, child_delta, *, sigma_p, sigma_c, beta=1.0)`
returning `"support"`, `"conflict"` or `None`, using `_SIGMA_FACTOR = 3.0` and
`MATCH_RELATIVE_TOLERANCE = 0.20`. Read it; import and reuse those two
constants (do not copy their values).

## API (exact names)

```python
@dataclass(frozen=True, slots=True)
class ChildStep:
    reader_id: str
    delta: float      # signed watts of this child's step
    sigma: float      # this child's noise sigma (watts, > 0)

MAX_SUBSET_SIZE = 3
MAX_SUBSET_CANDIDATES = 8

def residual_after_known(
    parent_move: float, sigma_p: float, known: Sequence[ChildStep], *, beta: float = 1.0
) -> tuple[float, float]:
    """(residual, residual_sigma): the parent move minus beta * sum(known deltas),
    sigma = sqrt(sigma_p**2 + sum((beta * k.sigma)**2))."""

def match_tolerance(total: float, sigmas: Sequence[float], residual_sigma: float, *, beta: float = 1.0) -> float:
    """_SIGMA_FACTOR * sqrt(residual_sigma**2 + sum((beta*s)**2)) + MATCH_RELATIVE_TOLERANCE * abs(beta*total)."""

def best_subset(
    residual: float, residual_sigma: float, candidates: Sequence[ChildStep],
    *, beta: float = 1.0, max_size: int = MAX_SUBSET_SIZE,
) -> tuple[str, ...] | None:
    ...

def classify_with_explanation(
    parent_move: float, sigma_p: float, *,
    known: Sequence[ChildStep], unknown: Sequence[ChildStep], beta: float = 1.0,
) -> dict[str, str | None]:
    ...

def verify_known(
    parent_move: float, sigma_p: float, *,
    known: Sequence[ChildStep], unknown: Sequence[ChildStep], beta: float = 1.0,
) -> dict[str, str | None]:
    ...
```

## Semantics

`best_subset`:
1. If `abs(residual) <= _SIGMA_FACTOR * residual_sigma` return `None` (the
   parent did not move beyond its noise).
2. Keep at most `MAX_SUBSET_CANDIDATES` candidates: the largest `abs(delta)`,
   ties by `reader_id`. Ignore candidates with `delta == 0`.
3. Enumerate every non-empty subset of size `1..max_size`
   (`itertools.combinations` over candidates sorted by `reader_id`). For each:
   `total = sum(delta)`, `tol = match_tolerance(total, [sigmas], residual_sigma, beta=beta)`,
   `err = abs(residual - beta * total)`. The subset *matches* when `err <= tol`.
4. Score a matching subset as `(err / tol, len(subset), members)`; the lowest
   score wins.
5. **Ambiguity:** if another matching subset of the *same size* has a different
   member set and `err / tol` within `0.05` of the winner's, return `None`.
   (Two equal legs of a 240 V load stepping together must not both claim one
   parent.)
6. Return the winner's `reader_id`s sorted, or `None` when nothing matches.

`classify_with_explanation` returns a verdict for every `unknown` child id:
1. `residual, rs = residual_after_known(parent_move, sigma_p, known, beta=beta)`.
2. If `abs(residual) <= 3 * rs` (flat after explanation): each unknown child
   with `abs(beta * delta) > 3 * rs` and
   `abs(residual - beta*delta) > match_tolerance(delta, [sigma], rs)` gets
   `"conflict"`; the others get `None`.
3. Otherwise `members = best_subset(...)`. Members get `"support"`, all others
   `None`. If `members is None`, all get `None`.

`verify_known` returns a verdict for every `known` child id (edge health):
for each known child `k`, compute the residual of the parent move minus all
*other* known children, then call
`classify_with_explanation(residual_move, residual_sigma_as_sigma_p, known=[], unknown=[k, *unknown])`
and report the verdict for `k` only. Here `residual_sigma_as_sigma_p` is the
residual sigma from `residual_after_known` over the other known children.

With `known=[]` and a single unknown child, `classify_with_explanation` must
agree with `_classify_step` (same support/conflict/None) for the cases tested.

## Tests (tests/unit/test_topology_explain.py), at least

* example 1: parent +100 (sigma 2), Y +100 (sigma 2) -> `{"Y": "support"}`.
* example 2: parent +100, Y +30, Z +70 -> both `"support"`.
* example 2 with a distractor W +400 -> Y, Z support, W None.
* known explain-away: parent +150, known A +100, unknown B +50 -> B support.
* known explains everything: parent +100, known A +100, unknown B +60 ->
  B `"conflict"`.
* flat parent: parent +1 (sigma 2), unknown Y +100 -> Y conflict.
* ambiguity: parent +1500, unknown H1 +1500, H2 +1500 -> both None.
* negative steps: parent -100, Y -30, Z -70 -> both support.
* `verify_known`: parent +150, known A +100 and B +50 -> both support;
  parent +50, known A +100 and B +50 -> A conflict, B support.
* agreement with `_classify_step` for single-child support, conflict and
  neutral (parent moved by a different amount: +300 vs child +100 -> None).
* performance: 8 candidates, max_size 3, 1000 calls finish in under 2 s.
