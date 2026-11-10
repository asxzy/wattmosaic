# Verification Report

Generated: `2026-11-10T07:29:52.316579+00:00`

**Required gates passed: 12/12**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.14 |  |
| secret_scan | **PASS** | 0.41 |  |
| lint | **PASS** | 0.05 |  |
| unit_tests | **PASS** | 8.25 |  |
| integration_tests | **PASS** | 2.33 |  |
| simulation_tests | **PASS** | 12.36 |  |
| end_to_end_tests | **PASS** | 45.82 |  |
| manifest_valid | **PASS** | 0.06 |  |
| release_valid | **PASS** | 0.04 |  |
| docs_fresh | **PASS** | 0.39 |  |
| deployment_valid | **PASS** | 2.11 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **PASS** | 0.52 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
