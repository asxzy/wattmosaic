# Agent Assignments

Authoritative workstream ownership. Each agent has a **disjoint file scope** and
imports the shared contracts; no agent may redefine a schema.

| Agent | Role | File scope | Depends on |
| --- | --- | --- | --- |
| Orchestrator | Architecture, contracts, storage, config, audit, integration, review | `contracts.py`, `config/`, `storage/`, `tools/audit/`, `docs/architecture.md`, `docs/data_flow.md`, `docs/interface_contracts.md`, `PROJECT_STATUS.md` | — |
| A | HA Environment Auditor | `tools/audit/*`, `docs/ha_environment_audit.md`, `data/*_inventory.json`, `data/ct_update_profile.json`, `data/initial_mapping_candidates.json` | — |
| B | Software Architect | merged into Orchestrator deliverables (architecture, data flow, contracts) | A |
| C | HA Integration Developer | `custom_components/wattmosaic/**`, `tests/integration/test_ha_integration.py`, `docs/integration.md` | contracts, §2 API |
| D | Python Engine Developer | `engine/wattmosaic_engine/{ingestion,synchronization,api,quality,diagnostics}/`, `main.py`, `tests/unit/test_{ingestion,synchronization,quality,api}.py`, `docs/api.md` | contracts, storage, config |
| E | Electrical / Disaggregation Specialist | `engine/wattmosaic_engine/{electrical,disaggregation}/`, `tests/unit/test_{electrical,disaggregation}.py`, `docs/electrical_model.md`, `docs/algorithms.md` | contracts, topology |
| F | Self-Learning & Model Specialist | `engine/wattmosaic_engine/{learning,models,aggregation}/`, `tests/unit/test_{learning,models,aggregation}.py`, `docs/learning.md` | contracts, storage |
| G | Database Specialist | merged into Orchestrator `storage/` (schema, migrations, retention, recovery) | contracts |
| H | App/Add-on & Deployment | `addon/**`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `scripts/`, `docs/deployment.md` | config, pyproject |
| I | Test & QA Specialist | `tools/simulator/**`, `tests/simulation/**`, `tests/end_to_end/**`, `tests/conftest.py`, `docs/testing.md`, `docs/acceptance_report.md` | contracts, simulator |
| J | Documentation & DX | `README.md`, `CONTRIBUTING.md`, `docs/{configuration,installation,troubleshooting,user_guide}.md`, `RELEASE_NOTES.md`, final report | all |

## Integration protocol

1. Contracts are frozen at `1.0.0` for the initial build. Changes require a
   version bump + regeneration of `data/schemas/` + notification.
2. Each agent runs its own tests before reporting.
3. The orchestrator reviews every delivered file, runs the **full** suite, and
   fixes integration mismatches itself rather than trusting reports.
4. A workstream is `VERIFIED` only after its tests pass in the orchestrator's
   own run, not merely in the agent's session.

## Parallelisation log

- Wave 1 (concurrent): E (electrical/algorithms), D (engine core), F
  (learning/models), C (HA integration), I (simulator/QA), H (add-on).
- Wave 2 (after wave 1): orchestrator integration wiring, full-suite run,
  fixes, docs J.
- Passively in parallel throughout: live HA CT sample collection (45 min).
