# M2: absolute-value capacity and paired (240 V) legs

Create `engine/wattmosaic_engine/electrical/topology_capacity.py` and
`tests/unit/test_topology_capacity.py`. Edit no other file.

## Physics

A parent CT measures the sum of everything below it. So at every sample
`sum(|child|) <= |parent| + tolerance`. A 240 V load has two legs (two CTs)
on different hot lines; their absolute values are highly correlated, and each
leg has a different parent leg.

## API (exact names)

```python
CAPACITY_TOLERANCE_W = 30.0
CAPACITY_RELATIVE = 0.05
CAPACITY_MAX_VIOLATION = 0.02      # share of comparable samples
LEG_MIN_CORRELATION = 0.95
LEG_MIN_ACTIVE_W = 200.0
LEG_MIN_ACTIVE_SAMPLES = 10

Series = Sequence[float | None]   # aligned: index i is the same instant for every series

def capacity_violation(parent: Series, children: Sequence[Series], *,
                       tol_w: float = CAPACITY_TOLERANCE_W, rel: float = CAPACITY_RELATIVE) -> tuple[float, int]:
    """(violation_fraction, comparable_samples). A sample is comparable when the
    parent and every child are not None. It violates when
    sum(abs(c)) > abs(p) + tol_w + rel * abs(p). No comparable sample -> (0.0, 0).
    Series of unequal length: compare only the common prefix."""

def fits(parent: Series, children: Sequence[Series], **kw) -> bool:
    """violation_fraction <= CAPACITY_MAX_VIOLATION (True when nothing is comparable)."""

def paired_legs(series: Mapping[str, Series], *, min_corr: float = LEG_MIN_CORRELATION,
                min_active_w: float = LEG_MIN_ACTIVE_W,
                min_active_samples: int = LEG_MIN_ACTIVE_SAMPLES) -> list[tuple[str, str, float]]:
    """Pairs (a, b, r) with a < b whose absolute values correlate (Pearson r of
    abs values) >= min_corr, over samples where both are not None. Require at
    least min_active_samples samples where BOTH abs values >= min_active_w, and
    the abs-value series of each must not be constant. Each reader appears in at
    most one pair: greedily take the highest r first. Sorted by (a, b)."""

def assign_parents(
    candidates: Mapping[str, Sequence[str]],
    series: Mapping[str, Series],
    *,
    levels: Mapping[str, float],
    nested: Callable[[str, str], bool],
    pairs: Sequence[tuple[str, str]] = (),
    fixed: Mapping[str, str] | None = None,
    score: Mapping[tuple[str, str], float] | None = None,
) -> dict[str, str]:
    ...
```

## `assign_parents` semantics

Input: `candidates[child]` = parent ids that currently qualify (e.g. confirmed
inferred parents). `nested(a, b)` is True when `a` is an ancestor of `b` (the
caller knows the tree). `fixed` = child -> parent assignments already decided
(user-declared/pinned), included in every capacity check but never changed.
`score[(parent, child)]` = optional evidence quality, higher is better
(e.g. support minus conflict); missing = 0.
Return child -> chosen parent for every child in `candidates` with at least
one candidate.

Algorithm:
1. **Nested reduction.** For each child, drop any candidate that is a strict
   ancestor of another candidate of the same child (`nested(a, b)` True ->
   drop `a`). This replaces the old "lowest level wins" rule only for nested
   parents.
2. Children with exactly one remaining candidate are assigned directly.
3. The rest are *ambiguous*. Enumerate every assignment of the ambiguous
   children to their remaining candidates (product); if the product has more
   than 4096 combinations, fall back to greedy: process ambiguous children in
   sorted order, each picking its best option given the choices so far, using
   the same cost.
4. Cost of an assignment (lexicographic, lower wins):
   a. number of **pair splits violated**: for each pair `(a, b)` in `pairs`
      where both are assigned and their candidate sets share at least two
      parents, +1 if a and b got the same parent;
   b. sum over parents of `capacity_violation(parent, [children assigned to
      it incl. fixed])[0]` (series missing -> 0);
   c. minus sum of `score[(parent, child)]`;
   d. sum of `levels.get(parent, inf)` (lower level = more direct);
   e. the tuple of chosen parent ids (deterministic tie break).
5. Pure and deterministic: same input -> same output.

## Tests (tests/unit/test_topology_capacity.py), at least

* `capacity_violation`: parent [100,100,None], children [[60,60,1],[30,50,1]]
  -> `(0.0, 2)` (sample 0: 90 <= 135, sample 1: 110 <= 135, sample 2 has a
  None parent); a clear violation case (children sum 300 vs parent 100 at
  every sample -> `(1.0, n)`).
* `paired_legs`: synthetic hot tub legs h1 = h2 + small noise switching
  0/1500 W, an unrelated load -> exactly `[("h1", "h2", r)]` with r >= 0.95;
  a constant pair is not reported; a reader is never in two pairs.
* hot tub: parents `l1`, `l2` (siblings, `nested` always False), legs `h1`,
  `h2` both candidates of both. `l1 = 400 + h1 + other1`, `l2 = 250 + h2`
  where `other1` is a 600 W load. Expect h1 and h2 on different parents.
  Then make `l2` too small to carry a leg at some samples so that only one
  assignment fits and expect that one.
* nested: child c with candidates [feed, sub] where `nested("feed", "sub")`
  -> assigned to `sub`.
* `fixed` children count against a parent's capacity.
* > 4096 combinations takes the greedy path and still returns a parent per
  child (13 ambiguous children with 2 candidates each), in under 2 s.
