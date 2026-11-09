# Verification Report

Generated: `2026-11-09T19:53:17.574728+00:00`

**Required gates passed: 11/11**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.17 |  |
| secret_scan | **PASS** | 0.38 |  |
| unit_tests | **PASS** | 8.25 |  |
| integration_tests | **PASS** | 2.17 |  |
| simulation_tests | **PASS** | 13.05 |  |
| end_to_end_tests | **PASS** | 49.16 |  |
| manifest_valid | **PASS** | 0.08 |  |
| release_valid | **PASS** | 0.06 |  |
| docs_fresh | **PASS** | 0.46 |  |
| deployment_valid | **PASS** | 2.3 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **PASS** | 0.65 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
