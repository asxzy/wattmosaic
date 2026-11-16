# task-04 — Contract narrowing

**Wave:** 2 · **Depends on:** task-01, task-02 · **Owner:** `contracts.py`

## Objective

Remove the public vocabulary: `CTType.DEDICATED_DEVICE`, `MonitoringLevel.DEDICATED`, and `DeviceTopology.dedicated_ct_ids`. Bump the contract version.

## Context

`CONTRACT_VERSION = "1.5.0"` (`contracts.py:31`) and `docs/interface_contracts.md:198-200` mandate: bump version → regenerate schemas → update that doc, in that order. Removing enum values is a breaking change, so the major version must move.

This task is safe only because task-01 already rewrites stored data. If it is done first, `load_topology` raises on every startup.

## Scope

- Remove the three members/field.
- Reword prose that asserts the removed semantics (`ConfidenceTier.TIER_3_MEASURED` docstring at `:245-246`).
- Keep `Source.DIRECT_MEASURED` (historical rows).
- Bump `CONTRACT_VERSION` to `2.0.0` in **both** places: `engine/wattmosaic_engine/contracts.py:31` and the integration mirror `custom_components/wattmosaic/const.py:31` (sent as the header from `api_client.py:135,176`).

Do **not** update engine callers here; that is task-05.

## Files owned

- `engine/wattmosaic_engine/contracts.py`
- `custom_components/wattmosaic/const.py`

## Steps

1. In `CTType` (lines 43-47), delete `DEDICATED_DEVICE = "dedicated_device"`.
2. In `MonitoringLevel` (lines 151-167), delete `DEDICATED = "dedicated"` and its docstring clause tying it to the role.
3. In `DeviceTopology` (lines 398-406), delete `dedicated_ct_ids: list[str]`.
4. Reword the `TIER_3_MEASURED` docstring: replace "A dedicated CT measured it" with a capability-neutral phrase such as "A private measurement or corroborated share".
5. Set `CONTRACT_VERSION = "2.0.0"` in `contracts.py:31`.
6. Set the mirror constant to `"2.0.0"` in `custom_components/wattmosaic/const.py:31`.
7. Update the module header comment (`:4-8`) if it describes the fields.

## Tests

- `test_contract_version_is_2_0_0` in `tests/unit/test_contracts.py` (update the existing pin at `:35-42`).
- `test_dedicated_values_are_rejected`: assert `CTType("dedicated_device")` and `MonitoringLevel("dedicated")` raise `ValueError`.
- `test_dedicated_ct_ids_field_removed`: assert `DeviceTopology` rejects a payload containing `dedicated_ct_ids` (given `extra="forbid"`, `:268-269`).
- `test_contract_version_mirrors_engine`: assert the integration constant equals the engine `CONTRACT_VERSION`.

## Acceptance criteria

- AC-6 (part): no write can introduce the role.
- `python -c "import wattmosaic_engine"` still succeeds once task-05 lands.
- Contract tests updated.
