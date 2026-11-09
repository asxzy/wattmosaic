# Verification Report

Generated: `2026-11-09T07:33:26.601864+00:00`

**Required gates passed: 4/4**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| secret_scan | **PASS** | 0.36 |  |
| manifest_valid | **PASS** | 0.06 |  |
| deployment_valid | **PASS** | 2.18 |  |
| hacs_valid | **PASS** | 0.03 |  |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
