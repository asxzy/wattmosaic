# Event isolation (v2)

Reference for `engine/wattmosaic_engine/disaggregation/isolation.py` — how the
engine answers "when this device changed, was anything else changing in the same
window?", and why absence of data is never promoted to `ISOLATED`.

The question is not "did an event happen?" but "can the power change be
attributed to one physical device?". A blended score cannot answer that, so the
output is a named `EventIsolation` verdict (`contracts.py:992`) with the window
that was examined and the evidence that decided it.

---

## 1. Two windows, deliberately separate

| Window | Width (default) | Claim |
| --- | --- | --- |
| isolation window | `30.0 s` | tight: this device's own transition is the only physical device change near the event |
| power window | `60.0 s` | wide: the CT delta attributed to this event was not polluted by a later sibling step |

`IsolationWindow` (`isolation.py:159`) enforces one invariant in
`__post_init__` (`isolation.py:172`): `power_window_s` is never *shorter* than
`isolation_window_s` (`max(window, power)`, `:180`). A power window that
excluded part of the isolation window would hide a change the tight check
already saw. Omitting `power_window_s` makes it equal `isolation_window_s`.

`IsolationWindow.shifted(occurred_at)` (`isolation.py:214`) returns
`(window_start, window_end, power_window_end)`; the offset is applied to the
*window*, never to `occurred_at`.

### 1.1 `IsolationConfig` defaults (`isolation.py:228`)

| Field | Default | Meaning |
| --- | --- | --- |
| `analysis_offset_s` | `0.0` | shift the window forward when the physical response lags the HA event |
| `isolation_window_s` | `30.0` | width of the tight window |
| `power_window_s` | `60.0` | width of the wide window; clamped up to `isolation_window_s` |
| `min_baseline_samples` | `3` | usable readings on the baseline side |
| `min_event_samples` | `3` | usable readings on the event side |
| `defaults` | `{}` | device-class label → `IsolationWindow` (middle layer) |
| `per_device` | `{}` | `device_id` → `IsolationWindow` (strongest layer) |

`__post_init__` validates eagerly (`isolation.py:248`): timings must be finite
and non-negative; sample counts must be non-negative integers; every override
must be an `IsolationWindow`. A negative window discovered at classify time
would surface as a *wrong verdict*, not a config error — which is why the
validation is not deferred.

### 1.2 Three-layer resolution

`IsolationConfig.resolve(device_id, device_classes)` (`isolation.py:277`)
returns `(window, source)`:

```
per-device override  >  device-class default  >  global default
```

`source` is `"device"`, `"class"` or `"global"` and is recorded in
`evidence["config_source"]`, so a verdict can be explained without reading the
config. `IsolationWindow.merged(base, overrides)` (`isolation.py:190`) reads
only the three known timing keys, so a typo cannot move an unrelated timing, and
non-finite/negative values are dropped rather than producing a window that
silently never matches.

In production the learning pipeline builds these layers from
`learning/pipeline.py::_isolation_config` (`learning/pipeline.py:157`), mapping
`ThresholdConfig.concurrency_window_s` / `power_analysis_window_s` and the
`isolation_by_class` / `isolation_by_device` maps. The audit pass
(`disaggregation/audit.py:198`) builds a flat `IsolationConfig` from
`AuditConfig` and therefore always answers `source: "global"` — see
`docs/limitations_v2.md`.

---

## 2. Decision table: `EventIsolator.classify`

`classify(event, competing_events, *, now, coverage)` (`isolation.py:619`).
**First match wins**; ordering is the design (module docstring `isolation.py:23`
and the checks at `:711-775`):

| # | Condition | Status | `reason` |
| --- | --- | --- | --- |
| 1 | `occurred_at` has no timezone | `INSUFFICIENT_DATA` | `event_time_naive` |
| 2 | the subject change is not load evidence | `INSUFFICIENT_DATA` | `not_load_evidence` |
| 3 | another **physical device** changed inside the isolation window | `CONTAMINATED` | `concurrent_device_change` |
| 4 | another physical device changed inside the power window only | `CONTAMINATED` | `power_window_device_change` |
| 5 | coverage was not measured (`None`) | `INSUFFICIENT_DATA` | `coverage_unknown` |
| 6 | coverage measured but below the minimums | `INSUFFICIENT_DATA` | `insufficient_samples` |
| 7 | `now` precedes `window_end` | `INSUFFICIENT_DATA` | `window_not_elapsed` |
| 8 | otherwise | `ISOLATED` | `isolated` |

Two ordering decisions are load-bearing:

* **Contamination is checked before coverage.** Seeing a sibling move is
  *positive evidence*: it needs no CT samples to believe. Checking coverage
  first would answer "I don't know" to a question that was already answered with
  certainty.
* **Everything else defaults to `INSUFFICIENT_DATA`.** Absence of data is never
  promoted to `ISOLATED`. Steps 5–7 exist only to prevent a thin window from
  reading as an all-clear.

`REASON_NAIVE_TIME` is first because a naive timestamp cannot be placed on a
timeline at all; `REASON_NOT_LOAD_EVIDENCE` is second because an availability
flip is a real event that says nothing about watts.

Constant names: `REASON_ISOLATED`, `REASON_CONCURRENT`, `REASON_POWER_WINDOW`,
`REASON_COVERAGE_UNKNOWN`, `REASON_INSUFFICIENT_SAMPLES`,
`REASON_WINDOW_NOT_ELAPSED`, `REASON_NOT_LOAD_EVIDENCE`, `REASON_NAIVE_TIME`
(`isolation.py:133-140`).

---

## 3. What counts as load evidence

`LOAD_EVIDENCE_ROLES = {LOAD_STATE, POWER_MEASUREMENT}` (`isolation.py:113`).
`MODE_ONLY` and `ENERGY_COUNTER` are deliberately absent: a mode signal and a
cumulative meter do not tell us the appliance drew power *at this instant*.

`_is_load_change(obj, binding)` (`isolation.py:425`) applies these rules in
order:

1. `event_type == "availability"` → **not** load evidence.
2. state label in `NON_LOAD_LABELS = {"unavailable", "unknown", "none"}`
   (`isolation.py:131`) → **not** load evidence.
3. `binding is None` (entity never declared) → **is** load evidence.
4. `binding.role in LOAD_EVIDENCE_ROLES` → **is** load evidence.
5. `binding.is_load_evidence` → **is** load evidence.
6. otherwise → not load evidence.

Rule 3 is the opposite of the intuitive default and is deliberate: the absence
of a binding is not proof that an entity only reports reachability. Ignoring
everything unbound would make isolation blind to undeclared devices.

Rule 2 exists because the production bridge (`main.py::_bridge_event`) drops
`event_type` when it converts an event to the learning dict, so the state label
is the only surviving signal.

`is_load_evidence` and `contributes_power` live on `HaEntityBinding`
(`contracts.py:665`); at most one binding per device may set
`contributes_power=True` (docstring `contracts.py:668`).

---

## 4. Many entities, one appliance

`physical_device_events(events, bindings)` (`isolation.py:505`) collapses HA
events into one `PhysicalDeviceEvent` per **physical device**:

* non-load-evidence observations are dropped by the same `_is_load_change`
  rules;
* one device yields exactly one event, so three entities firing together are
  **one** competing device, not three;
* ordering is by `occurred_at`, never by arrival; ties keep first-seen order, so
  the result is deterministic for a fixed input (`isolation.py:586`);
* when two entities disagree on polarity, the event keeps both observations and
  sets `conflicting=True` with note `conflicting_observations`;
* two `contributes_power` entities for one device are reported rather than
  double counted (`power_contributor_conflict`, `_contributors_by_device`
  `isolation.py:488`);
* `ct_ids` is the union of every member's declared `supporting_ct_ids`. **Empty
  means "the topology did not declare a scope", which is never read as "cannot
  interfere"** (`PhysicalDeviceEvent` docstring, `isolation.py:448`).

`classify` removes the subject **by physical device, never by identity**
(`isolation.py:679-682`), so a second entity of the same appliance can never
masquerade as a competitor.

---

## 5. Verdict vs guard: who can actually pollute a delta?

`classify` answers the literal question. Whether a competitor *could have moved
a particular measured delta* is a narrower question answered by
`could_share_ct(subject_cts, other)` (`isolation.py:406`):

* an empty scope on **either** side means "not declared" → assumed to interfere;
* only a positive, disjoint pair is harmless (`own & theirs` empty → `False`).

The result is recorded as `evidence["ct_separated_device_ids"]` and
**does not change the status**. A competitor on a different meter is still a
competitor for the purpose of the verdict; the learning guard uses that finding
so a dedicated-CT device is not discarded during an automation scene
(`isolation.py:60-66`).

---

## 6. Late arrival and retention

### 6.1 `reclassify(previous, late_event)` (`isolation.py:778`)

Returns `previous` **unchanged, by identity**, when the late event cannot affect
it: the previous verdict is not `ISOLATED`; the late timestamp is missing or
naive; it belongs to the same physical device; it is not load evidence; or it
falls outside `[window_start, power_window_end)`.

Otherwise it returns a new `EventIsolation` with `status=CONTAMINATED`,
`reason="late_competing_device"` (`REASON_LATE`, `isolation.py:141`),
`power_window_contaminated=True`, and evidence keys `reclassified`,
`previous_status`, `late_event_id`, `late_device_id`. `occurred_at` is copied,
never recomputed.

### 6.2 `split_attribution` / `clean_attribution_inputs`

`split_attribution(isolations)` (`isolation.py:849`) partitions into
`(clean, withheld)`:

* clean = `ISOLATED`;
* withheld = `CONTAMINATED` **and** `INSUFFICIENT_DATA`.

Nothing is deleted. A contaminated event stays in the list because it is a real
observation; it simply cannot support a clean attribution.
`clean_attribution_inputs` (`isolation.py:865`) is the **only** exclusion point
in the codebase.

---

## 7. How the audit pass supplies coverage

`AuditPass._coverage` (`disaggregation/audit.py:1038`) counts samples across
every channel:

* baseline = samples with `start <= stamp < isolation_end`;
* event = samples with `isolation_end <= stamp < power_end`;
* `histories` empty → `None` (not measured), which is step 5 above.

Counting samples that do not exist would be worse than admitting the window was
never observed, which is why `SampleCoverage.baseline_samples` /
`event_samples` are `int | None` and `sufficient()` returns `False` for either
`None` (`isolation.py:312`).

Verdicts are persisted through `persist_isolations` →
`Database.insert_isolation_verdict` (`audit.py:439`, migration 10), because an
`InferenceRecord` may cite `event:<event_id>` and that reference must still
resolve after a restart.

`AuditPass.retained_verdicts()` (`audit.py:973`) keeps the last
`DEFAULT_VERDICT_HISTORY = 512` verdicts in process memory, deduped by
`event_id`, so `/api/v1/isolation` describes a few minutes of analysis rather
than whichever event happened to mature in the most recent cycle (usually none).

---

## 8. Boundaries worth stating

* Isolation adds metadata. It never moves history: `occurred_at` is carried
  byte-identical from input to verdict to record.
* `INSUFFICIENT_DATA` is the default, not the exception. A permanently
  unobserved window stays `INSUFFICIENT_DATA` and is preserved, not deleted.
* Contamination is never decomposed. V1 excludes the event; it does not attempt
  automation-aware disaggregation (see `docs/limitations_v2.md`).
* The verdict is about *time overlap between physical devices*, not about
  arithmetic. Guessing the share of a jointly-moving delta is association's job,
  and it refuses (`ambiguous`) rather than apportioning.
