# Verification Report

Generated: `2026-11-09T20:21:38.999009+00:00`

**Required gates passed: 12/12**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.15 |  |
| secret_scan | **PASS** | 0.37 |  |
| lint | **PASS** | 0.04 |  |
| unit_tests | **PASS** | 8.96 |  |
| integration_tests | **PASS** | 2.55 |  |
| simulation_tests | **PASS** | 12.63 |  |
| end_to_end_tests | **PASS** | 48.11 |  |
| manifest_valid | **PASS** | 0.08 |  |
| release_valid | **PASS** | 0.05 |  |
| docs_fresh | **PASS** | 0.46 |  |
| deployment_valid | **PASS** | 2.16 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **PASS** | 0.64 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
