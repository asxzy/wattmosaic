# task-01 — Engine schema v11 migration

**Wave:** 1 · **Depends on:** none · **Owner:** `storage/migrations.py` only

## Objective

Add schema migration v11 that makes every stored surface survivable once the enum members are removed:

1. demote `ct_channels.ct_type` from `dedicated_device` to `branch`;
2. move device ownership from `dedicated_ct_ids_json` into `branch_ct_ids_json`;
3. strip demoted ids from `metadata_json.projected_ct_ids` where appropriate;
4. migrate `device_configurations.payload_json` so a stored `"monitoring": "dedicated"` does not get its whole configuration row dropped;
5. rewrite historical `inference_results.monitoring_level = "dedicated"` so old rows still validate.

## Context

Three independent load paths break if this migration is missing:

- **Topology channels.** `load_topology` validates `ct_type` through Pydantic at `storage/database.py:294` with no fallback; `CTType.DEDICATED_DEVICE` removal makes every startup raise.
- **Device configurations.** `load_device_configurations` (`database.py:371-374`) wraps `DeviceConfiguration.model_validate(...)` in `try/except Exception: continue`. Once `MonitoringLevel.DEDICATED` is gone, a stored `"monitoring": "dedicated"` raises and the **entire configuration row is silently skipped** — losing state entity ids, expected watts, light/dimming config and controller maps. The "tolerant path" sometimes cited for this case (`disaggregation/configuration.py:345-350`) is never reached, because it operates on topology metadata, not on the stored payload.
- **Historical inference rows.** `row_to_inference_result` passes the raw string at `main.py:462`; `InferenceResult.monitoring_level` is a `MonitoringLevel` enum (`contracts.py:818`). Old rows carrying `"dedicated"` would fail validation.

Migrations run inside `Database.__init__` (`database.py:76`) before any load, and the `apply=` callback runs inside the same `BEGIN IMMEDIATE` transaction as the SQL statements (`migrations.py:480-497`), so this is the required ordering seam.

Leaving an id in `dedicated_ct_ids_json` after demoting its channel produces validation problems (`electrical/topology.py:246-276`) and strips attribution, because `attribution_ct_ids` falls through to `most_specific_branch_group`, which reads only `branch_ct_ids` (`topology.py:351-367`).

## Scope

`storage/migrations.py` only. Do **not** touch `storage/database.py` (task-05b owns it) or contract enums (task-04).

## Files owned

- `engine/wattmosaic_engine/storage/migrations.py`

## Steps

1. Add `Migration(11, "retire_dedicated_device_role", [...], apply=_retire_dedicated_rows)` following the existing idiom at `:16-27` and `:480-497`.
2. Bump `LATEST_VERSION` via the existing `max(...)` expression at `:473` (it derives from the tuple, so adding the entry is sufficient).
3. SQL statement (unconstrained; `ct_channels.ct_type` has no CHECK constraint, `migrations.py:130-147`):

   ```sql
   UPDATE ct_channels SET ct_type = 'branch' WHERE ct_type = 'dedicated_device';
   ```

4. Callback step B — for each row of `devices`:
   - parse `dedicated_ct_ids_json` and `branch_ct_ids_json`;
   - `union = sorted(set(dedicated) | set(branch))`;
   - write `union` to `branch_ct_ids_json`;
   - write `[]` to `dedicated_ct_ids_json`;
   - leave `device_id` and all other columns untouched.
5. Callback step C — `projected_ct_ids`. Read the key `PROJECTED_CTS_KEY = "projected_ct_ids"` (literal confirmed at `configuration.py:93`; written only there). For each device:
   - compute `demoted = set(old dedicated_ct_ids)`;
   - remove those ids from `metadata_json["projected_ct_ids"]` **only if** they are not also named by any surviving device configuration's `monitored_by_ct_ids`. Reason: `project_configuration` retracts previously projected ids before re-adding them (`configuration.py:727-736`, `:784-807`), so a stale entry is corrected on the next projection. Ids from the **channel wizard** never appear in `projected_ct_ids` (`flow_logic.py:807-931` writes device lists directly), so this rule cannot wrongly strip a user declaration made there.
6. Callback step D — `device_configurations`: for each row, parse `payload_json`; replace `"monitoring": "dedicated"` with `"shared"`; write back. Leave every other key intact.
7. Callback step E — `inference_results`: `UPDATE inference_results SET monitoring_level = 'shared' WHERE monitoring_level = 'dedicated';`
8. Do **not** insert a `topology_versions` row. The returned version is the AUTOINCREMENT rowid (`database.py:242-248`); a migration must not fake a client-visible bump. Note that `topology_versions.payload_json` retains historical `dedicated_device` values; the engine never parses those (`database.py:255-260` reads only `status`/`version`/`created_at`), but audit tools do (`tools/audit/replay_capture.py:207`, `tools/dev/replay_nesting.py:68`). Record this as a known limitation in task-11's release notes rather than rewriting history.

## Tests

`tests/unit/test_migrations.py` (new file, owner task-03):

- Build a schema-10 database with **raw `sqlite3`** — constructing `Database` runs migrations in `__init__`, so it cannot produce a pre-migration fixture.
- `test_v11_demotes_dedicated_channels`
- `test_v11_moves_device_ids`
- `test_v11_preserves_other_columns` (`device_id`, `phase`, `polarity`, `paired_ct_id`, `parent_ct_id`)
- `test_v11_strips_projected_cts` — assert demoted ids no longer appear in `metadata_json["projected_ct_ids"]`, and that an id still named by a surviving configuration is preserved
- `test_v11_migrates_device_configuration_payload` — assert `monitoring` becomes `shared` and other keys survive
- `test_v11_migrates_historical_inference_rows`
- `test_apply_callback_is_idempotent` — invoke the v11 `apply=` callback **directly twice** on one connection; assert `branch_ct_ids` unchanged on the second pass. (A full second migration run is a no-op because `migrations.py:484-485` skips `version <= start`, so it cannot detect a double-add bug.)
- `test_pre_change_database_loads_after_upgrade` — `Database(path)` then `load_topology()` succeeds.
- `test_no_attribution_loss` — for every device in the fixture, `graph.attribution_ct_ids(dev) != ()`.

## Acceptance criteria

- AC-2: a pre-change database opens, migrates and loads without error.
- AC-3: after migration every device retains ≥1 attribution row.
- AC-4: the apply callback is idempotent under direct invocation.
- Stored configurations and historical inference rows survive.
- `ruff check engine tools tests` clean; new tests pass.
