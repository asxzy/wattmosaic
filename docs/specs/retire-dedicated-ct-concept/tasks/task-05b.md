# task-05b — Engine reference cleanup (runtime consumers)

**Wave:** 2 · **Depends on:** task-04 · **Owner:** four files outside the direct path

## Objective

Repoint the runtime consumers that an import-only check misses. Without this task the engine imports cleanly and then raises on the first topology read, tree render, device-link resolution, or auto-placement run.

## Context

The review found these surfaces reference `DeviceTopology.dedicated_ct_ids` and are edited by no other task:

| File | Line | What breaks |
| --- | --- | --- |
| `storage/database.py` | `:236` | every `save_topology` |
| `storage/database.py` | `:317` | every `load_topology` |
| `electrical/panel_tree.py` | `:659` | every hierarchy-tree render |
| `device_links.py` | `:166-169` | entity→device link resolution |
| `disaggregation/auto_placement.py` | `:210-223` | placement discovery |

`storage/database.py` was deliberately withheld from task-01 so Wave 1 stays a pure migration seam; this is its owner.

## Scope

Repoint these five sites. Delete no subsystem.

## Files owned

- `engine/wattmosaic_engine/storage/database.py`
- `engine/wattmosaic_engine/electrical/panel_tree.py`
- `engine/wattmosaic_engine/device_links.py`
- `engine/wattmosaic_engine/disaggregation/auto_placement.py`

## Steps

1. `database.py:236`: write `json.dumps([])` (the column is retained for schema compatibility but always empty after migration) or drop the column from the INSERT and set it to `'[]'` in a follow-up migration. Prefer keeping the column and writing `[]`, so the schema stays stable and a rollback path remains.
2. `database.py:317`: pass `dedicated_ct_ids=[]` on load.
3. `panel_tree.py:659`: `_device_ct_ids` becomes `list(device.branch_ct_ids)` plus the existing `channel.device_id` union. The comment above it must be updated.
4. `device_links.py:166-169`: replace `device.get("dedicated_ct_ids")` / `device.dedicated_ct_ids` reads with the branch equivalents. Update the module docstring at `:18`.
5. `auto_placement.py:210-223`: the "already claimed" refusal at `:210-215` loses its role trigger. Re-express as: refuse when another **declared** device already owns the row (read `branch_ct_ids` across devices) and `not assume_owned`. Keep `cts_claimed_by_dedicated` as the shared helper; task-05a repoints it at `branch_ct_ids`.
6. Run `pytest -q` plus a smoke test that saves and reloads a topology.

## Tests (implemented by task-14)

- `test_save_and_reload_topology_after_narrowing`
- `test_panel_tree_builds_after_narrowing`
- `test_device_links_resolve_after_narrowing`
- `test_auto_placement_refuses_owned_row_after_narrowing`

## Acceptance criteria

- `save_topology` / `load_topology` round-trip works against a migrated database.
- Panel tree renders.
- Device links resolve.
- Auto-placement refuses an already-owned row.
