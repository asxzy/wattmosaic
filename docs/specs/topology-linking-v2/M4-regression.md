# M4: differenced-series regression, duplicate meters, hold resampling

Create `engine/wattmosaic_engine/electrical/topology_regression.py` and
`tests/unit/test_topology_regression.py`. Edit no other file.

## Why (live analysis, 2026-11-20, 7 days, 46 CTs)

Busy, noisy mains (sigma 12-27 W, about 30 steps/h) and unsynchronised
16 s sampling make per-event step matching fail for sub-panel feeds: the main
looks flat in 50-78 % of big sub steps. Over many samples, a lag-0 regression
of the parent's sample-to-sample *differences* on the child's differences
separates the true parent (r 0.55, slope 0.62) from the wrong leg (r 0.33,
slope 0.40). A second meter on the same mains (r >= 0.98, small constant
offset) is a duplicate, never a child. HA's recorder writes a row only when the
value changes, so a flat reader has long gaps that must be held, not dropped.

## API (exact names)

```python
Series = Sequence[float | None]   # aligned: index i is the same instant for every series

GRID_STEP_S = 10.0
REG_MIN_CHILD_DIFF_W = 50.0
REG_MIN_SAMPLES = 20
DUP_MIN_CORRELATION = 0.98
DUP_MAX_OFFSET_W = 150.0
DUP_MIN_ACTIVE_SAMPLES = 50

@dataclass(frozen=True, slots=True)
class RegressionFit:
    slope: float   # least squares through the origin: sum(dp*dc) / sum(dc*dc)
    r: float       # Pearson correlation of dp and dc over the used samples
    n: int         # number of used samples

def resample_hold(
    times: Sequence[datetime], values: Sequence[float | None], *,
    start: datetime, end: datetime, step_s: float = GRID_STEP_S, max_hold_s: float | None = None,
) -> list[float | None]:
    """Value at each grid instant start, start+step, ... (< end): the last sample
    at or before the instant (times are sorted ascending). None before the first
    sample, when the last sample is a None, or when max_hold_s is set and the
    last sample is older than max_hold_s. max_hold_s=None means hold forever
    (recorder rows are written only on change)."""

def align(
    history: Mapping[str, Sequence[tuple[datetime, float | None]]], *,
    start: datetime, end: datetime, step_s: float = GRID_STEP_S, max_hold_s: float | None = None,
) -> dict[str, list[float | None]]:
    """resample_hold for every reader (rows sorted first). Every list has the same length."""

def diff_regression(
    parent: Series, child: Series, *,
    min_child_diff_w: float = REG_MIN_CHILD_DIFF_W, min_samples: int = REG_MIN_SAMPLES,
) -> RegressionFit | None:
    """dp[i] = parent[i] - parent[i-1], dc[i] likewise, for i >= 1 where all four
    values are not None. Use only samples with abs(dc[i]) >= min_child_diff_w.
    Fewer than min_samples used samples, or zero variance in dp or dc -> None."""

def rank_parents(
    child: str, candidates: Iterable[str], series: Mapping[str, Series], **kw,
) -> list[tuple[str, RegressionFit]]:
    """diff_regression(series[p], series[child]) for each candidate p != child
    that is in series and gives a fit; sorted by r descending, then parent id."""

def duplicate_meters(
    series: Mapping[str, Series], *,
    min_corr: float = DUP_MIN_CORRELATION, max_offset_w: float = DUP_MAX_OFFSET_W,
    min_active_samples: int = DUP_MIN_ACTIVE_SAMPLES,
) -> list[tuple[str, str, float, float]]:
    """(a, b, r, offset) with a < b where, over samples where both are not None
    (at least min_active_samples of them and neither series constant there):
    Pearson r of the raw values >= min_corr, the least-squares slope of b on a
    (with intercept) is within [0.9, 1.1], and abs(median(b - a)) <= max_offset_w.
    offset = median(b - a). Sorted by (a, b)."""
```

## Tests (tests/unit/test_topology_regression.py), at least

* `resample_hold`: hold forever by default; `max_hold_s` returns None after
  the gap; None before the first sample; grid length = ceil((end-start)/step).
* `align`: unsorted rows are sorted; all lists equal length.
* `diff_regression`: synthetic parent = base noise (sigma 25, random walk
  steps from 3 other loads) + child (on/off 100-400 W loads); fit has slope
  within 0.85-1.15 and r > 0.5; the same child against an unrelated parent
  (independent loads only) has r < 0.2. Too few samples -> None. Constant
  child -> None.
* `rank_parents`: true parent ranks first among three candidates.
* `duplicate_meters`: b = a + 60 + small noise -> reported with offset ~60;
  b = 0.5 * a -> not reported (slope); unrelated -> not reported; parent vs
  child where the parent carries extra independent loads -> not reported.
* All tests use `random.Random(seed)`; deterministic.
