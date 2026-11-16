# task-10 — Schemas and examples

**Wave:** 5 · **Depends on:** task-04 · **Owner:** `data/schemas/**`, `data/examples/**`

## Objective

Regenerate the public JSON schemas and hand-fix the example fixtures so CI's drift gate passes.

## Context

`.github/workflows/ci.yml:26-29` runs `python tools/dev/export_schemas.py` and fails on any diff. Seven schema files embed the removed enum values plus the removed field. Two example files contain fixture data that tests validate.

Schemas are generated; never hand-edit them. Examples are hand-maintained.

## Scope

Run the exporter; edit examples; verify the index version.

## Files owned

- `data/schemas/*.json` (generated)
- `data/examples/topology.example.json`
- `data/examples/reference_house/topology_candidates.json`

## Steps

1. Run `python tools/dev/export_schemas.py`.
2. Confirm `index.json` reports `2.0.0` and the manifest lists all 19 schemas.
3. Confirm `ct_channel.json`, `topology.json`, `device_topology.json` no longer mention `dedicated_device` or `dedicated_ct_ids`.
4. Confirm `snapshot_response.json`, `inference_result.json`, `device_configuration.json`, `device_configuration_set.json` no longer list `"dedicated"` in `MonitoringLevel.enum`.
5. Edit `data/examples/topology.example.json` and `data/examples/reference_house/topology_candidates.json`: replace `dedicated_device` roles with `branch`, moving ids to `branch_ct_ids` where applicable.
6. Run `git diff --exit-code data/schemas` to prove regeneration is stable.

## Tests

- Existing example-validation tests keep passing.
- Add `test_examples_have_no_dedicated_role`: scan both example files for `dedicated`.

## Acceptance criteria

- AC-9: schemas regenerated, committed, drift-free.
- Examples valid against the new schemas.
