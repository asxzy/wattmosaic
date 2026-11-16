# task-02 — HA config-entry migration

**Wave:** 1 · **Depends on:** none · **Owner:** `custom_components/wattmosaic/__init__.py`, `config_flow.py` (VERSION line only)

## Objective

Rewrite stored `ct_assignments` roles `dedicated_device` → `branch` during config-entry upgrade, so a legacy entry does not fight the engine after the role disappears.

## Context

There is no `async_migrate_entry` in the integration today (`VERSION = 1` at `config_flow.py:780`; repo-wide grep finds only the unrelated `async_migrate_entity_ids`). The design's hook signature is `async_migrate_entry(hass, entry)`.

Failure mode if omitted: `assignment_matches_channel` (`flow_logic.py:1052-1072`) compares the stored role against the migrated `ct_type`; a mismatch makes `plan_topology_push` return `declaration_missing` (`:1226-1236`), and `build_topology_update` then writes `"ct_type": normalised["role"]` (`:833`) before `client.put_topology` (`:1544-1550`). Once the engine enum drops the value, that PUT returns 400 (`api/server.py:305-310`). There is also a worse branch: if `const.py` simply drops the literal while the alias shim is absent, `normalise_assignment` coerces the stored role to `unknown` and silently strips attribution.

`CONF_CT_ASSIGNMENTS` is a **dict keyed by `ct_id`** (`flow_logic.py:1796-1800`), not a list. Entries may live in either `entry.data` or `entry.options`; `__init__.py:72` merges both, so the migration must update whichever mapping holds them.

## Scope

- Add `async_migrate_entry(hass, entry)` in `__init__.py`.
- Bump `VERSION` to 2.
- Add the temporary compatibility alias in `normalise_assignment` so a legacy entry that has not yet migrated cannot be silently coerced to `unknown`. **This alias is owned by task-02 but its removal is task-13's job** (the two edits are separated by the whole of Wave 2, so they do not collide).

## Files owned

- `custom_components/wattmosaic/__init__.py`
- `custom_components/wattmosaic/config_flow.py` (VERSION constant only)
- `custom_components/wattmosaic/flow_logic.py` (`normalise_assignment` alias, lines 587-591)

## Steps

1. In `config_flow.py`, change `VERSION = 1` → `VERSION = 2`.
2. In `__init__.py`:

```python
async def async_migrate_entry(hass, entry):
    if entry.version == 1:
        data = dict(entry.data)
        options = dict(entry.options)
        changed = False
        for store in (data, options):
            assignments = store.get(CONF_CT_ASSIGNMENTS)
            if not isinstance(assignments, dict):
                continue
            for ct_id, assignment in assignments.items():
                if isinstance(assignment, dict) and assignment.get("role") == "dedicated_device":
                    assignment["role"] = "branch"
                    changed = True
        if changed:
            hass.config_entries.async_update_entry(
                entry, data=data, options=options, version=2,
            )
        else:
            hass.config_entries.async_update_entry(entry, version=2)
    return True
```

3. Persist the version with `async_update_entry(..., version=2)`; assigning `entry.version` alone does not survive.
4. Preserve every other key on the assignment: `device_id`, `phase`, `polarity`, `paired_ct_id`, `parent_ct_id`, `alias_of_ct_id`, `enabled`.
5. In `flow_logic.py:587-591`, add the legacy alias before the whitelist check:

```python
if role == "dedicated_device":     # legacy entries; removed once task-13 lands
    role = "branch"
if role not in CT_ROLES:
    role = CT_ROLE_UNKNOWN
```

Comment it as temporary and reference task-13 for removal.

## Test requirements (implemented by task-14)

- `test_legacy_entry_migrates_roles`: schema-1 entry whose `ct_assignments` is a `{ct_id: {...}}` dict with a dedicated assignment; assert `role == "branch"`, other keys preserved, `version == 2`.
- `test_legacy_entry_in_options_is_migrated`: same, with assignments under `entry.options`.
- `test_normalise_assignment_maps_legacy_role`: assert `"dedicated_device"` → `"branch"`, never `"unknown"`.
- `test_migrated_entry_replays_without_error`: replay against a migrated engine topology; assert no `declaration_missing` and no 400.

task-14 owns these files; this task supplies the assertions so they can be written test-first alongside the implementation.

## Acceptance criteria

- AC-5: a HA config entry storing `role = "dedicated_device"` survives upgrade and replays without a 400.
- No silent `unknown` coercion of legacy assignments.
- Existing entry-migration tests keep passing.
