# Verification Report

Generated: `2026-11-11T07:05:09.078143+00:00`

**Required gates passed: 12/12**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.18 |  |
| secret_scan | **PASS** | 0.46 |  |
| lint | **PASS** | 0.04 |  |
| unit_tests | **PASS** | 8.52 |  |
| integration_tests | **PASS** | 2.34 |  |
| simulation_tests | **PASS** | 42.55 |  |
| end_to_end_tests | **PASS** | 70.83 |  |
| manifest_valid | **PASS** | 0.07 |  |
| release_valid | **PASS** | 0.05 |  |
| docs_fresh | **PASS** | 0.43 |  |
| deployment_valid | **PASS** | 2.38 |  |
| hacs_valid | **PASS** | 0.03 |  |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.

## Not exercised in this run

`config_valid` (`python -m wattmosaic_engine.main --check-config`) is the only
optional gate and it requires a live `HA_TOKEN`. This release was prepared
without contacting the real Home Assistant, so that gate was **not run** and is
not counted in the 12/12 above. It asserts nothing about the engine's behaviour;
it only proves the operator's environment is wired to a reachable instance.
