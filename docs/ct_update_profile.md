# CT Update Profile

Generated: `2026-01-01T00:00:15+00:00`
Source: `synthetic reference household`.

> **This profile is synthetic.** It is the committed example used by the
> test suite and documentation, not the operator's installation. Run
> `tools/audit/profile_samples.py` against your own recorded samples to
> produce a real one; that output is local-only and git-ignored.

## Method

state_changed subscription; intervals measured from both HA last_updated and local receive time; repeated values flagged separately

- `last_updated` is a Home Assistant state update timestamp. It is **not** a
  physical measurement timestamp.
- Repeated identical values are counted and are not treated as independent
  measurements by the engine.
- Bursts below 0.5 s indicate batched/refreshed entity writes, not faster
  physical sampling.

## Summary

```json
{
  "sample_window_minutes": 20,
  "channels_profiled": 36,
  "median_interval_s": 15.8,
  "p10_interval_s": 14.8,
  "p90_interval_s": 16.6,
  "mean_interval_s": 16.0,
  "min_interval_s": 11.8,
  "max_interval_s": 33.8
}
```

Bursts smaller than the channel cadence are integration poll cycles, not higher-rate measurements. Inference windows must be sized from per-channel cadence and aligned by timestamp.

## Per channel

Per-channel rows are omitted from the published example on purpose: a
channel list with its observed statistics identifies a specific
installation. A local run of `tools/audit/profile_samples.py` prints them.

## Limitations

- A short observation window cannot characterise infrequent channels.
- Home Assistant exposes no per-reading hardware timestamp; only the state
  update time. The engine therefore keeps engine receipt time separately.
- Update cadence observed here is the cadence *after* the integration poll, so
  it is an upper bound on staleness, not the sensor's internal rate.
