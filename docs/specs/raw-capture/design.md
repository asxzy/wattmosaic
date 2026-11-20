# Design: raw CT capture

## 1. Goals

- G1. A default production install writes **no** rows to `ct_samples`.
- G2. A developer can turn full-rate capture on for N days with one setting.
- G3. An upgraded install drains its existing `ct_samples` rows without stalling the
  asyncio loop and without one giant transaction.
- G4. Production issues can be replayed from HA's recorder history with the existing
  replay tool, and an operator can check up front whether the recorder actually holds
  the CT entities.
- G5. No runtime output changes. Inference, learning, snapshots and the published
  values are identical with capture on or off.

## 2. Non-goals

- No change to `ha_events_v3`. It is read back for provenance of published results
  (`core/runtime.py:1371`, `core/snapshot.py:492`) and keeps `retention_days_ha_events`.
- No change to `device_state_samples` or the other raw-tier tables (`inference_results`,
  `reconciliation_results`, `data_quality_events`, `system_events`). They keep
  `retention_days_raw`.
- No change to `event_evidence.samples_json` thinning (`WM_EVIDENCE_RAW_RETENTION_DAYS`).
- No schema migration. The `ct_samples` table and its indexes stay. Removing the table
  is a possible later cleanup, not part of this plan.
- No `VACUUM`. SQLite reuses freed pages, so the file stops growing but does not shrink.
  Documented for operators (T07); an automated shrink is out of scope.
- No engine code reads HA history. The HA-history replay source lives in `tools/audit/`
  only. The engine stays read-only toward HA and never reads HA registries (ADR-0028).
- No removal of the dead `SamplesRepository` read methods. They are covered by existing
  storage tests and the dev tools may use them; deleting them is optional later cleanup.
- No version bump and no `RELEASE_NOTES.md` edit. CI writes the release notes.

## 3. Behaviour

### 3.1 Capture setting

| `raw_capture_days` | Write path | `ct_samples` retention |
|---|---|---|
| `0` (default) | Readings are not enqueued for persistence. `samples_persisted` stays 0. | Background purge deletes **all** rows (drains legacy data), batch by batch. |
| `N` (1–90) | Readings persist exactly as today. | Background purge deletes rows with `received_at < now − N days`. |

Ingestion, P1–P3 and the in-memory rings behave identically in both modes.

### 3.2 Retention paths after the change

- `Database.prune(raw_days, aggregate_days, capture_days=...)` (on-loop maintenance):
  when `capture_days` is passed (not `None`) it **skips** `ct_samples` and reports
  `removed["ct_samples"] == 0`. When `capture_days` is `None` it keeps the legacy
  behaviour (raw horizon), so direct storage callers and existing tests are unchanged.
- `_prune_evidence` (background) no longer deletes from `ct_samples`. It still applies
  evidence and `samples_json` retention.
- New background job `_purge_raw_capture` is the only runtime path that deletes
  `ct_samples`. It runs on `BackgroundExecutor` like the other prune jobs, deletes in
  bounded batches each in its own short `BEGIN IMMEDIATE` transaction, takes the DB lock
  per batch only, and stops when a batch-count or time budget is reached. The next
  maintenance tick continues.

### 3.3 Diagnostics

`GET /api/v1/health` `details.raw_capture = {"days": N, "enabled": N > 0}`, threaded into
the diagnostics report's `engine_health`. `details` is the additive, schema-free object,
so there is no contract change. The panel
diagnostics view shows one line: "Raw CT capture: off (history comes from the Home
Assistant recorder)" or "Raw CT capture: on, keeping N days".

### 3.4 Replay from HA history (dev tooling)

`tools/audit/replay_capture.py --ha-history [--end ISO] --minutes M --db <copy>`:
- wiring and the version triple come from `--db` (`topology_versions`, active versions),
  as today;
- samples come from HA `history/history_during_period` for the CT entity ids in the
  topology, fetched in time chunks;
- `received_at` is set to HA's `last_updated` and the report notes that arrival
  jitter is not available;
- entities with no history and a recorder start later than the requested window
  produce explicit notes, not silent gaps.

`tools/audit/check_recorder_coverage.py --db <copy> [--days 10]` reports, per CT entity,
row count, first/last timestamp, largest gap and a status (`ok` / `short` / `missing`),
and exits non-zero if any entity is `missing`.

## 4. Acceptance criteria

- AC1. With default config, feeding readings through a real `EngineCore` leaves
  `ct_samples` empty and `samples_persisted == 0`; snapshots and inference results are
  produced as before. (T08)
- AC2. With `raw_capture_days = 2`, readings persist; rows older than 2 days are removed
  by `_purge_raw_capture`; newer rows survive. (T08)
- AC3. With `raw_capture_days = 0` and pre-existing rows, repeated `_purge_raw_capture`
  runs drain the table to 0; a single run never deletes more than its batch budget.
  (T03, T08)
- AC4. `db.prune(..., capture_days=<int>)` never touches `ct_samples`;
  `db.prune(raw_days, aggregate_days)` still prunes it on the raw horizon. (T02)
- AC5. `_prune_evidence` no longer issues any `DELETE FROM ct_samples`. (T08)
- AC6. `WM_RAW_CAPTURE_DAYS` parses, validates to 0–90, appears in `to_dict()`; the App
  option has the same bounds and default and `run.sh` maps it. (T01, T06, parity in T10)
- AC7. The HA-history loader turns a recorded HA history payload into a `Capture` that
  `replay()` accepts, with notes for missing entities and late recorder start; no network
  in tests. (T04)
- AC8. The coverage probe classifies `ok` / `short` / `missing` correctly from a payload;
  exit code reflects `missing`. (T05)
- AC9. `perf_runtime.py --quick` at default config shows `ct_samples` growth ≈ 0 and
  total DB growth reduced by the raw share; numbers recorded in TODO §1. (T10)
- AC10. All CI gates in `CLAUDE.md` pass on Python 3.11 and 3.12. (T10)

## 5. Accepted caveats (documented in T07)

1. **Recorder exclusions.** Users who exclude 1 Hz power sensors from the recorder or
   lower `purge_keep_days` have no HA history. The coverage probe detects it; such users
   can set `raw_capture_days`.
2. **Not byte-identical.** HA history has no engine `received_at`, so a replay loses
   arrival delay and reordering. Timing bugs need `raw_capture_days > 0` to reproduce.
3. **No deep re-detection.** After a detector fix, previously missed events can be
   recovered only within the recorder window (10 days by default). Learning continues
   from live data.

## 6. Risks

| Risk | Mitigation |
|---|---|
| Large legacy purge blocks writers | Per-batch transactions, per-batch lock, batch + time budget (T03) |
| Purge scans without an index | With a cutoff, select by `received_at < ? ORDER BY received_at, id` so `idx_ct_samples_time` (`migrations/legacy.py:302`) answers filter and order and an empty result costs nothing; `ORDER BY id` only when `cutoff is None` (review finding on T03) |
| A hidden reader of `ct_samples` appears | T10 re-greps engine/api/ui/learning; AC1 runs the real runtime with capture off |
| App and engine bounds drift | Parity tests in T10, mirroring `test_derived_retention.py:398-414` |
