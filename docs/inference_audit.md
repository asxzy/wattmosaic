# Inference audit trail (v2)

Reference for `engine/wattmosaic_engine/disaggregation/audit.py` — the one place
where isolation, association, circuits, residuals and hierarchy run *together*,
and the versioned `InferenceRecord` trail they produce.

The audit answers a question the per-cycle outputs cannot: *why* did the engine
report this watt value, under which algorithm version, and what did it supersede?
It is append-only, deterministic and independent of wall-clock time, so a replay
of the same raw data is byte-identical.

---

## 1. The pass

`AuditPass` (`audit.py:475`) runs six stages in a fixed dependency order
(`audit.py:11`):

```
HA event → isolation → association → circuits → residual → hierarchy → record
```

`AuditPass.analyze` (`audit.py:582`) is the entry point for one incremental
window; `AuditPass.reanalyze` (`audit.py:634`) is the entry point for a stored
history window. Both return an `AuditReport` and never raise for a single bad
input — a failure is recorded in `errors` under a stable key and the pass
continues.

The pass starts no thread, task or timer of its own (`audit.py:47`). It is
called from the existing inference loop in
`integration.py:348` (`_analyze` then `_maybe_reanalyze`).

### 1.1 Design rules (each observable in the code)

| Rule | Where |
| --- | --- |
| Import, never reimplement: every verdict comes from the owning module | `audit.py:19` |
| Nothing is invented on refusal: a scope that cannot be computed is reported with the module's reason code | `audit.py:23` |
| Failure isolation per stage *and* per item | `audit.py:28` |
| Deterministic identity; stage outputs sorted before writing | `audit.py:33` |
| Append-only: a record cites `previous_record_id`, never overwrites | `audit.py:38` |

### 1.2 `AuditConfig` (`audit.py:129`)

| Field | Default | Meaning |
| --- | --- | --- |
| `analysis_offset_s` | `0.0` | shift the analysis window forward from `occurred_at` |
| `isolation_window_s` | `30.0` | tight window ("was anything else changing?") |
| `power_window_s` | `60.0` | wide window whose readings are attributed to the event |
| `event_max_age_s` | `1800.0` | how long an event may wait for its window before it is dropped |
| `min_baseline_samples` | `3` | readings required before the event |
| `min_event_samples` | `3` | readings required after the event |
| `algorithm_version` | `DEFAULT_ALGORITHM_VERSION` = `"disagg@2.0"` (`audit.py:71`) | stamped onto every artifact |
| `write_isolation` | `True` | persist `isolation_verdicts` |
| `write_association` | `True` | persist association relations |
| `write_circuits` | `True` | persist circuits |
| `write_residual` | `True` | persist residual observations |
| `write_hierarchy` | `True` | persist hierarchy relations |
| `write_records` | `True` | persist `inference_records` |

`__post_init__` (`audit.py:157`) rejects a negative `analysis_offset_s`, a
non-positive `isolation_window_s`, a `power_window_s` shorter than
`isolation_window_s`, an empty `algorithm_version` and a non-positive
`event_max_age_s`.

A `write_*` flag gates **storage only**. The algorithms still run, because a
family that is not written may still be an input to one that is (a circuit is
classified from associations), so a flag can never silently change a computed
value (`audit.py:131`). The one exception is the caller: `integration.py:781`
skips the pass entirely when *every* `write_*` flag is off.

`AuditConfig.from_settings` (`audit.py:169`) maps `EngineConfig` onto these
fields, and the master switch `WM_AUDIT_ENABLED` **and**-s each per-artifact
flag. See `docs/configuration.md` for the environment table.

Derived sub-configs are re-versioned, not shared: `association`
(`audit.py:199`), `circuits` (`:203`) and `hierarchy` (`:207`) prefix the audit
version (`f"assoc:{...}"`, `f"circuits:{...}"`, `f"hierarchy:{...}"`), so a
stored relation says which *stage* and which *run* produced it.

### 1.3 `AuditReport` (`audit.py:224`)

One pass's result, kept in `AuditPass.last_report` so the read-only API can
answer without re-analysing (`audit.py:225`).

| Field | Default | Meaning |
| --- | --- | --- |
| `at` | required | the window instant the pass analysed |
| `mode` | `"incremental"` | or `"reanalysis"` |
| `isolations` | `[]` | verdicts from this pass |
| `relations` | `[]` | association + hierarchy edges |
| `circuits` | `[]` | classified circuits |
| `residuals` | `[]` | residual observations |
| `records` | `[]` | records written by this pass |
| `counts` | all `COUNT_KEYS` at `0` | per-family counters |
| `refused` | `{}` | `scope → reason_code` (never prose) |
| `errors` | `{}` | `stage-or-item → "Type: message"` |
| `stages` | `{}` | per-stage diagnostic blocks |
| `written` | `{}` | family → rows actually stored |

`COUNT_KEYS` (`audit.py:108`) is declared once so a reader cannot typo a key:

```
events, isolated, contaminated, insufficient_data, observations,
associations, overlaps, parent_child, circuits, circuits_classified,
residuals, records
```

`as_dict()` (`audit.py:236`) exposes `at`, `mode`, `counts`, `refused`,
`errors`, `stages`, `written` — deliberately not the artifact lists, which
would duplicate every row into `/diagnostics`.

---

## 2. `InferenceRecord` (`contracts.py:875`)

A versioned, append-only inference record. A revision never overwrites its
predecessor: `previous_record_id` links back, `revision` strictly increases, and
the algorithm version travels with the result so a stored value can still be
explained after an upgrade (`contracts.py:876-882`).

`Model` forbids extra fields (`contracts.py:470`), so an unknown key is a
validation error rather than a silently dropped fact.

| Field | Type / default | Meaning |
| --- | --- | --- |
| `record_id` | `str` | deterministic id, see §2.1 |
| `device_id` | `str` | the device the record is about |
| `timestamp` | `datetime` | when the inference was made (the window instant) |
| `algorithm_version` | `str` | e.g. `"disagg@2.0"`; non-empty (`contracts.py:940`) |
| `contract_version` | `CONTRACT_VERSION` = `"1.6.0"` (`contracts.py:32`) | contract the payload was written against |
| `topology_version` | `int` = `1` | topology version the inference ran against |
| `revision` | `int` = `1`, `ge=1` | monotone per device |
| `previous_record_id` | `str \| None` | the record this revision supersedes |
| `previous_power_w` | `float \| None` | superseded power, so a delta needs no join |
| `power_w` | `float` | the inferred device power in W |
| `source` | `Source` = `UNKNOWN` | how the value was obtained |
| `measurement_basis` | `MeasurementBasis` = `UNKNOWN` | what kind of instrument produced the watts |
| `confidence` | `ConfidenceVector` | per-dimension confidence, no aggregate |
| `previous_confidence` | `ConfidenceVector \| None` | the superseded vector, when there was one |
| `confidence_delta` | `dict[str, float]` | `{dimension: delta}` for dimensions that moved |
| `reason` | `str` = `""` | short machine code (see §2.4) |
| `explanation` | `list[str]` | human-readable lines |
| `evidence_refs` | `list[str]` | stable refs (`event:`, `relation:`, `circuit:`, `record:`), never free text |
| `evidence` | `dict` | structured supporting evidence |

### 2.1 Identity: `record_id_for` (`audit.py:415`)

```python
payload = f"{device_id}|{algorithm_version}|{at.isoformat()}|{int(revision)}"
digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
return f"rec_{digest[:24]}"
```

Only facts about *what was inferred* participate. A wall-clock timestamp, a
random UUID or a database row id would make the same replay produce a different
id, which is exactly what the determinism requirement forbids (`audit.py:417`).
`AuditPass._record_id` (`audit.py:1791`) is the thin wrapper that supplies the
configured version.

### 2.2 Chain validation (`contracts.py:948`)

`_version_chain` is a `model_validator(mode="wrap")` and enforces three separate
things:

1. A record whose `source` is not `UNKNOWN` must cite at least one
   `evidence_refs` entry (`contracts.py:955`). An unknown source asserts nothing,
   so it is allowed to cite nothing.
2. When the validation context carries a `records` registry, `previous_record_id`
   must name a known record, of the **same device**, earlier in time, with a
   strictly smaller `revision` (`contracts.py:970-989`).
3. Without a registry the chain cannot be verified, and the record is accepted:
   "accepting is honest; inventing a verdict would not be" (`contracts.py:966`).

Rule 2 is context-dependent, so constructing an `InferenceRecord` inside the
pass does not re-verify the chain against every stored row. The stored
`previous_record_id` still points at a real row because `_record` derives it from
`Database.latest_inference_record` (`audit.py:1673`), which is seeded at
construction so a restart continues the same chain instead of restarting at
revision 1 (`audit.py:537`).

### 2.3 Storage (`database.py:619`)

`insert_inference_record` is a plain `INSERT` with no upsert: a revision never
overwrites its predecessor. All nineteen columns are written explicitly,
`ConfidenceVector`s as `confidence_json`/`previous_confidence_json`, and the
list/dict fields as JSON text.

Reads:

| Method | Order | Notes |
| --- | --- | --- |
| `inference_records_for_device(device_id, since=None, limit=5000)` (`database.py:671`) | `(timestamp, revision)` **ascending** | `limit` keeps the newest rows (query is `DESC LIMIT`, then re-sorted ascending) |
| `latest_inference_record(device_id)` (`database.py:697`) | `revision DESC, timestamp DESC` | `None` for an unknown device |

`_records` (`audit.py:1565`) also skips a replay: when the freshly built
`record_id` equals the stored one, the row is not rewritten
(`audit.py:1601`), because "rewriting it would be an overwrite".

---

## 3. What gets recorded, and what does not

A per-cycle row for every device would append roughly 640k rows/day on a
37-device install for a value that did not change, so the trail records
*changes* plus a heartbeat (`audit.py:97`).

### 3.1 `_changed` (`audit.py:1626`)

| Condition | Result |
| --- | --- |
| no stored previous record | **write** (first record always writes) |
| stored `power_w` missing/unparsable | **write** |
| `abs(power_w - previous_power_w) > RECORD_POWER_TOLERANCE_W` (`1.0` W) | **write** |
| `source` changed | **write** |
| `measurement_basis` changed | **write** |
| any confidence dimension moved by `> RECORD_CONFIDENCE_TOLERANCE` (`0.05`) | **write** |
| stored `timestamp` missing/unparsable | **write** |
| `at - stored_timestamp >= RECORD_HEARTBEAT_S` (`3600.0` s) | **write** |
| otherwise | **skip** |

Two details a reader should not have to infer:

- The confidence comparison only exercises the `power` dimension: it builds
  `ConfidenceVector(power=...)` and iterates `dimensions()`
  (`audit.py:1655`). A change confined to `ct_association` or
  `circuit_classification` alone does not, by itself, trigger a record.
- The heartbeat means a long steady state still proves the pass was alive,
  without turning the trail into one row per cycle (`audit.py:1636`).

`_last_power` is refreshed for **every** device on every pass
(`audit.py:1613`), not only where a record was written, so a steady device does
not keep a stale baseline and report a zero step for a real transition.

### 3.2 Revision assignment

```
revision        = stored_previous.revision + 1      (if a previous row exists)
                = self._last_revision[device] + 1   (otherwise)
previous_id     = stored_previous.record_id or self._last_record[device]
previous_power  = stored_previous.power_w    or self._last_power[device]
```

(`audit.py:1673-1685`). The three `_last_*` maps are seeded from storage in
`_seed_from_database` (`audit.py:536`), which is what makes a restart continue
the chain.

### 3.3 Evidence references (`audit.py:1706`)

The candidate refs are, in order: every isolation verdict of this device
(`event:<event_id>`), every `DEVICE_CT_ASSOCIATION` whose subject is the device
(`relation:<relation_id>`), every circuit naming the device
(`circuit:<circuit_id>`).

They then pass through `_resolvable_refs` (`audit.py:1768`), which keeps only
refs `Database.resolve_evidence_ref` (`database.py:939`) can actually resolve.
Recognised prefixes:

| Ref | Resolves to |
| --- | --- |
| `event:<event_id>` | an `isolation_verdicts` row (migration 10) |
| `relation:<relation_id>` | a `topology_relations` row |
| `circuit:<circuit_id>` | a `circuits` row |
| `record:<record_id>` | an `inference_records` row |
| `sample:<ct_id>@<iso>` | a stored `ct_samples` row |

A reference is a claim that a reader can follow it, so when a family's write is
disabled (or storage is absent) the corresponding rows do not exist and citing
them would be a dangling claim (`audit.py:1769`). Two fallbacks follow:

- `source == UNKNOWN` → `evidence_refs = []`, the contract's own rule.
- resolvable list empty but source known → a self-reference
  `record:<this record_id>`, the one reference that always resolves because this
  pass persists the record. It is weaker evidence than a raw observation and is
  recorded as such (`audit.py:1735`).

### 3.4 Confidence vector (`audit.py:1724`)

`_record` builds exactly four fields: `power` from the `InferenceResult`
(bounded to `[0, 1]`, non-finite → `0.0`), `ct_association` = max association
confidence, `circuit_classification` = max
`circuit.metadata["classification_confidence"]`, and
`method=f"audit:{algorithm_version}"`. `topology` and `residual` are **never
populated** by the pass. See `docs/confidence_model.md` §2.

`confidence_delta` (`audit.py:455`) is `{}` for a first record, and otherwise
only the dimensions whose rounded 6-decimal delta is non-zero — a dimension that
did not move is absent, not `0.0`.

### 3.5 `reason` codes (`audit.py:1795`)

| First matching condition | `reason` |
| --- | --- |
| any verdict `ISOLATED` | `isolated_event` |
| any verdict `CONTAMINATED` | `contaminated_event` |
| the device has an association relation | `associated_measurement` |
| source `DIRECT_MEASURED` | `direct_measurement` |
| source `MODEL_ESTIMATED` | `model_estimate` |
| source `CONFIGURED_PRIOR` | `configured_prior` |
| otherwise | `no_attribution` |

The order makes an isolatable event outrank an association: the stronger claim
wins, and the function is deterministic so a replay's `reason` is identical.

### 3.6 `explanation` and `evidence`

`explanation` always begins with `source=` and `tier=` lines, then up to five
isolation lines, five association lines and five circuit lines
(`audit.py:1823`). The caps keep a record bounded on a device with many
artifacts. `evidence` carries `window_s`, a compact isolation list
(`event_id`/`status`/`reason`), sorted `association_ids`, sorted `circuit_ids`
and the audit version (`audit.py:1748`).

---

## 4. Replay and reanalysis

`AuditPass.reanalyze` (`audit.py:634`) re-derives every artifact from stored raw
history instead of the in-process tracker, which is what lets it reach
`ISOLATED` for an event that was still inside its own analysis window when it
arrived (`audit.py:641`).

| Step | Behaviour |
| --- | --- |
| window | `start = at - max(60.0, lookback_s)` (`audit.py:656`) |
| samples | `ct_samples_between(start, at, keys)` with the **aligner** keys, then re-keyed to topology `ct_id` via `_aligner_keys` (`audit.py:694`, `:679`) |
| events | `recent_device_events(start, limit=2000)` (`audit.py:669`) |
| power baseline | `_seed_power_history` reads `inference_history` per device (limit 2000) so an association is not judged against "unknown step" (`audit.py:1209`) |
| pending queue | bypassed entirely in `mode="reanalysis"` (`audit.py:804`) |
| failure | a read failure lands in `errors` and the pass continues (`audit.py:665-672`) |

Re-keying is not cosmetic: `ct_samples.ct_id` is the aligner key (the entity
object id, e.g. `kitchen_l1_power`), not the topology `ct_id`. Reading it as a
topology id matches no channel and looks exactly like "no history yet"
(`audit.py:335`). Unmapped rows are dropped rather than invented into a channel.

Bumping `algorithm_version` is what makes "replay under v2 and diff against v1"
a meaningful operation (`audit.py:78`):

There is no `WM_AUDIT_ALGORITHM_VERSION`. `AuditConfig.from_settings`
(`audit.py:185`) reads `getattr(settings, "audit_algorithm_version",
DEFAULT_ALGORITHM_VERSION)`, and `EngineConfig` (`config/settings.py:99`) does
not define that attribute — so the effective version is always `"disagg@2.0"`
unless a caller passes an explicit `AuditConfig`.

A replay with the same raw data and the same version produces byte-identical
records: the id is a hash of `(device, version, instant, revision)`, and stage
outputs are ordered before they are written.

Scheduling is owned by the backend, not the pass: `_maybe_reanalyze`
(`integration.py:807`) compares `utcnow()` against
`reanalysis_interval_s` and calls the pass. `interval <= 0` disables it
(`integration.py:817`), and the clock is only *read* there — no timer is created.

### 4.1 What a reanalysis does *not* do

- It does not re-open records of devices with no new `InferenceResult`: `_records`
  iterates `by_device`, which comes from the live results
  (`audit.py:584`, `:1588`).
- It does not rewrite an existing verdict row: `insert_isolation_verdict`
  upserts by `event_id` (`database.py:806`), so re-analysing the same event
  replaces its verdict rather than accumulating a second one.

---

## 5. Retention and the read surface

Audit state is exempt from raw retention by construction
(`storage/migrations.py:121`, `database.py:1305`). `prune` deletes
`ct_samples`, `inference_results`, `device_events`, `device_state_samples`,
`data_quality_events`, `system_events` and `reconciliation_results` past
`raw_days`, then reports every table in `AUDIT_TABLES` with a count of `0`
instead of deleting it.

`AUDIT_TABLES` is seven tables, not four:

```
circuits, topology_relations, inference_records, ct_measurement_factors,
ha_entity_bindings, residual_power, isolation_verdicts
```

`isolation_verdicts` joined in migration 10 precisely so
`inference_records.evidence_refs` entries of the form `event:<id>` still resolve
after a restart (`migrations.py:129`).

Consequence worth stating plainly: `inference_records` and the other audit tables
have **no retention horizon of their own**. They grow for the life of the
database. See `docs/limitations_v2.md` §7.

### 5.1 Read-only endpoints

| Route | Facade method | Doc |
| --- | --- | --- |
| `GET /api/v1/devices/{device_id}/records?limit=` | `inference_records` (`main.py:2104`) | `docs/api.md` §3.19 |
| `GET /api/v1/circuits` | `circuits` (`main.py:2056`) | `docs/api.md` §3.17 |
| `GET /api/v1/relations?kind=` | `relations` (`main.py:2071`) | `docs/api.md` §3.18 |
| `GET /api/v1/residuals?hours=&limit=` | `residual_components` (`main.py:2116`) | `docs/api.md` §3.20 |
| `GET /api/v1/isolation` | `isolation_status` (`main.py:2132`) | `docs/api.md` §3.21 |

All five are read-only: none triggers analysis or a write, so a UI refresh can
never become an unbounded operation on the event loop
(`api/server.py:719`). Each answers `503` when the facade does not implement it
(`NullFacade`), which the end-to-end tests assert
(`tests/end_to_end/test_audit_api_e2e.py:281`).

`/diagnostics` also reports the pass, through `_audit_summary`
(`integration.py:840`): `enabled`, `algorithm_version`, `reanalysis_interval_s`,
`reanalysis_attempts`, `last_reanalysis_at` and `last_pass` (the
`AuditReport.as_dict()` payload).

---

## 6. Worked example: two revisions of one device

Inputs: one device, two inference cycles an hour apart, the second with a 900 W
step and a changed source.

| | revision 1 | revision 2 |
| --- | --- | --- |
| `timestamp` | `12:00:00Z` | `13:00:00Z` |
| `power_w` | `10.0` | `900.0` |
| `revision` | `1` | `2` |
| `previous_record_id` | `None` | `rec_…` of revision 1 |
| `previous_power_w` | `None` | `10.0` |
| `confidence_delta` | `{}` | only the dimensions that moved |

`record_id` for revision 1 is `sha256("dev1|disagg@2.0|2026-11-13T12:00:00+00:00|1")[:24]`
prefixed with `rec_`. Replay at the same instant with the same version yields
the same string; a differing `algorithm_version` deliberately yields a different
one.

This is the shape the end-to-end test asserts: revisions `[1, 2]`,
`body[1]["previous_record_id"] == body[0]["record_id"]`, power preserved
(`tests/end_to_end/test_audit_api_e2e.py:158-159`).

---

## 7. Refusals and errors are data

Two dictionaries on the report, both surfaced by `/diagnostics`:

- `refused[<family>:<id>] = <reason_code>` — an artifact the owning module
  declined to produce (e.g. `residual:<circuit_id> = "multi_ct_scope"`). Codes
  are counted and asserted on; prose is not (`audit.py:97`).
- `errors[<stage-or-item>] = "Type: message"` — a raised exception that was
  contained. Item-level keys are stable
  (`isolation:<device>@<occurred_at>`, `circuit:<id>`, `record:<device>`,
  `write:<family>`) so the same defect lands on the same key across passes.

Neither is ever rendered as a zero result: a refused residual is absent from
`residuals`, and the API's `count` reflects that.

---

## 8. Related documents

- `docs/event_isolation.md` — the verdict cited by `event:` refs
- `docs/association_and_topology.md` — the `relation:` refs and hierarchy edges
- `docs/residual_analysis.md` — the `residual_power` rows and their statuses
- `docs/confidence_model.md` — the dimensions in `ConfidenceVector`
- `docs/factor_semantics.md` — what `measurement_basis` means per reading
- `docs/api.md` §3.17–§3.21 — the read endpoints
- `docs/limitations_v2.md` — what this trail does not prove
