# task-13 — Home Assistant integration cleanup

**Wave:** 7 · **Depends on:** task-02, task-04 · **Owner:** `const.py`, `config_flow.py`, `flow_logic.py`, `strings.json`, `translations/en.json`

## Objective

Remove the role option, the monitoring level, and the now-dead mapping step from the integration, and re-express the double-pole cross-device guard so it survives without the role.

## Context

`normalise_assignment` (`flow_logic.py:587-591`) coerces an unknown role to `unknown`. The temporary legacy alias added in task-02 maps `dedicated_device` → `branch`; this task **removes that alias** once the config-entry migration (task-02) guarantees no un-migrated entry remains.

The mapping step (`config_flow.py:620-634`) only prompts for unmapped dedicated channels, so it becomes dead. Channel→device mapping stays available via the per-channel `device_id` dropdown (`:594-598`), so removing the step loses no capability.

`pair_crosses_devices` is not a function — the guard is **inline** inside a larger validator at `flow_logic.py:697-708`, and grep finds no `def pair_crosses_devices`. To make it unit-testable and role-independent, extract it into a helper (e.g. `_pair_crosses_devices(pairs) -> list[str]`) and rewrite the rule: fire when two paired CTs each carry a non-null `device_id` and they differ, regardless of role. This preserves the only double-pole cross-device guard.

`strings.json` and `translations/en.json` must stay byte-identical (`tests/integration/test_ha_integration.py:2396`).

## Scope

Integration files listed above. The VERSION constant was already bumped in task-02.

## Files owned

- `custom_components/wattmosaic/const.py`
- `custom_components/wattmosaic/config_flow.py`
- `custom_components/wattmosaic/flow_logic.py`
- `custom_components/wattmosaic/strings.json`
- `custom_components/wattmosaic/translations/en.json`

## Steps

1. `const.py`: drop `"dedicated_device"` from `CT_ROLES` (line 88); delete `CT_ROLE_DEDICATED` (line 91); drop `"dedicated"` from `MONITORING_LEVELS` (lines 129-135).
2. `config_flow.py`:
   - remove the role `SelectOptionDict` at `:130`;
   - remove `_MONITORING_LABELS["dedicated"]` at `:232`;
   - drop the `CT_ROLE_DEDICATED` import at `:60`;
   - reword the module docstring at `:13`;
   - delete `_unmapped_channels` (`:620-634`), `async_step_mapping`, `_mapping_schema`, `_mapping_placeholders`, and route `async_step_channels` straight to `async_step_confirm`.
3. `flow_logic.py`:
   - remove the task-02 legacy alias;
   - drop the `CT_ROLE_DEDICATED` import at `:24`;
   - delete the `dedicated_needs_device` rule (`:685-686`);
   - extract and rewrite the cross-device pairing guard (`:697-708`) as `_pair_crosses_devices`: fire when two paired CTs each carry a non-null `device_id` and they differ, regardless of role;
   - simplify the uncertainty branches at `:780-804`;
   - delete the dedicated mirror path in `build_topology_update` (`:848-930`);
   - drop `"dedicated"` from the `_clean_choice` list at `:1883`;
   - delete the now-unused candidate helpers at `:1730-1784` after checking callers.
4. Strings: edit `:48`, `:51`, `:164` in both `strings.json` and `translations/en.json` identically. Remove the dedicated clauses; keep the surviving enumerations accurate.
5. Verify byte-identity between the two translation files.

## Tests (implemented by task-14)

- `test_role_options_exclude_dedicated`: the rendered role selector offers exactly main / branch / unknown.
- `test_pair_crosses_devices_without_dedicated_role`: two branch channels paired with different non-null device_ids still produce the error via the extracted helper.
- `test_monitoring_options_exclude_dedicated`.
- Update `tests/unit/test_config_flow_logic.py:227` (`monitoring: "dedicated"`) and `tests/integration/test_ha_integration.py:1520-1833`.

## Acceptance criteria

- AC-1 (integration part): options flow offers no dedicated role or monitoring level.
- Cross-device pairing guard still fires for non-dedicated pairs.
- Translation files identical.
