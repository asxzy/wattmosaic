# task-03 — Migration tests

**Wave:** 1 · **Depends on:** task-01, task-02 (test-first: write before/with them) · **Owner:** `tests/unit/test_migrations.py`

## Objective

Provide the failing-first coverage that proves the migration seam works before any enum is removed.

## Context

`Database.__init__` runs migrations immediately (`storage/database.py:76`), so a pre-migration fixture cannot be built by constructing `Database`. Use raw `sqlite3` against a schema-10 layout, then open it with `Database` to trigger v11.

`migrations.py:484-485` skips any migration whose version is already applied, so a naive "run the whole migration twice" test passes regardless of double-add bugs. Idempotency must be tested by invoking the v11 `apply=` callback directly.

## Scope

Test-only. Do not modify production code beyond what task-01/task-02 specify.

## Files owned

- `tests/unit/test_migrations.py` (new)
- `tests/fixtures/` (only if a reusable old-shape DB builder is needed)

## Steps

1. Add a raw-`sqlite3` helper that creates a schema-10 database with:
   - one `main` channel,
   - two `dedicated_device` channels,
   - a device whose `dedicated_ct_ids_json` names them,
   - `metadata_json` containing `projected_ct_ids` with those ids,
   - one `device_configurations` row with `"monitoring": "dedicated"`,
   - one `inference_results` row with `monitoring_level = "dedicated"`.
2. Write the assertions listed in task-01 and task-02.
3. Run the suite; confirm the new tests fail on the unmodified tree and pass after task-01/task-02.

## Test requirements

- `test_v11_demotes_dedicated_channels`
- `test_v11_moves_device_ids`
- `test_v11_preserves_other_columns`
- `test_v11_strips_projected_cts` — demoted ids gone; an id still named by a surviving configuration preserved
- `test_v11_migrates_device_configuration_payload`
- `test_v11_migrates_historical_inference_rows`
- `test_apply_callback_is_idempotent` — invoke the callback directly twice
- `test_pre_change_database_loads_after_upgrade`
- `test_no_attribution_loss` — `graph.attribution_ct_ids(dev) != ()` for every device, using enabled channels only
- HA side: `test_legacy_entry_migrates_roles`, `test_legacy_entry_in_options_is_migrated`, `test_normalise_assignment_maps_legacy_role`, `test_migrated_entry_replays_without_error`

## Acceptance criteria

- All wave-1 tests pass.
- The same tests fail on the unmodified tree (prove they are real).
