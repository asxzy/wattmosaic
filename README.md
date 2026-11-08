# WattMosaic

Intelligent multi-CT home energy monitoring, **load disaggregation** and
**self-learning** for Home Assistant.

WattMosaic turns the measurements you already have — incoming L1/L2 CTs, branch
circuit CTs, dedicated appliance CTs and Home Assistant device states — into
per-device power estimates, with an explicit, honest treatment of what it cannot
know.

> **Status:** v0.1.3 — engineering-complete core, validated on synthetic ground
> truth over a read-only Home Assistant ingestion path. It does
> **not** claim real-world per-device accuracy; see
> [`docs/real_ha_validation.md`](docs/real_ha_validation.md).

---

## What it does

- Reads **all** available power and energy entities plus device state changes from
  Home Assistant (read-only, over WebSocket).
- Models the **electrical topology**: main CTs, branch CTs, dedicated CTs, nested
  circuits, L1/L2 phases and double-pole 240 V loads.
- Estimates individual device power using a measurement hierarchy:
  1. `direct_measured` — a verified dedicated CT.
  2. `inferred` — constrained solving against CT measurements and state events.
  3. `model_estimated` — learned device model with supporting state.
  4. `unknown` — insufficient evidence. **No fabricated numbers.**
- Preserves the **unexplained residual** instead of distributing it.
- Learns device models automatically from normal operation, validates them
  against hold-out data, and only promotes a model when it beats the active one.
- Tracks appliance cycles, operating stages, runtime and energy.
- Exposes per-device and whole-home sensors in Home Assistant.

## What it deliberately does not do

- It does not switch devices, change settings or control anything.
- It does not guess when loads are ambiguous. If two devices changed at once and
  the meters cannot distinguish them, it says so.
- It does not treat a repeated sensor value as a new measurement.
- It does not report a confidence number as a probability.

---

## Architecture

```
Home Assistant ──state_changed──► Engine ingestion ──► alignment ──► quality
                                        │
                                        ▼
                              electrical model (y = Ax)
                                        │
                                        ▼
                          disaggregation + residual analysis
                                   │              │
                                   ▼              ▼
                            device models    energy aggregation
                                   │              │
                                   └──────┬───────┘
                                          ▼
                                   SQLite (WAL)
                                          │
                                    HTTP API /api/v1
                                          │
                          WattMosaic HA Integration ──► sensors
```

The Home Assistant integration is a thin HTTP client. All inference and learning
live in the independent engine, so it can also run in Docker or on another host.

Full detail: [`docs/architecture.md`](docs/architecture.md),
[`docs/data_flow.md`](docs/data_flow.md),
[`docs/interface_contracts.md`](docs/interface_contracts.md).

## Supported measurement configurations

| Configuration | Support |
| --- | --- |
| Two mains (L1 + L2) only | Yes — inference with explicit ambiguity |
| Mains + branch CTs | Yes |
| Mains + dedicated appliance CTs | Yes — `direct_measured` |
| Nested branch CTs (main → branch → sub-branch) | Yes — no double counting |
| Double-pole 240 V appliance (`*_ct1`/`*_ct2` pair) | Yes — one logical load |
| Multiple panels (panel A + panel B) | Yes — as separate measurement groups |
| No CTs at all | Not useful; no measurements to disaggregate |

---

## Installation

### Option A — Marketplace / HACS (recommended for the integration)

Since Home Assistant **2026.11**, HACS is replaced by the built-in
**Marketplace**. Add this repository as a custom repository and install
**WattMosaic**:

1. **Settings → Marketplace → Add from link**
2. Enter `https://github.com/asxzy/wattmosaic` and choose type **Integration**
3. Install **WattMosaic**, then restart Home Assistant

<details>
<summary>Home Assistant before 2026.11 (HACS)</summary>

Add `https://github.com/asxzy/wattmosaic` in **HACS → Integrations → ⋮ →
Custom repositories** as type *Integration*, install, and restart.

</details>

The integration is a thin display client. It needs a running engine, so install
the engine first (Option B) — the integration alone produces no estimates.

### Option B — Engine: Home Assistant OS App (recommended)

Add `https://github.com/asxzy/wattmosaic` in **Settings → Apps → Install app →
⋮ → Repositories**, then install **WattMosaic Engine** and set its options (HA
URL, a **long-lived access token**, and an API token). The App pulls a prebuilt
multi-arch image (`amd64`/`aarch64`), so nothing is compiled on your box.

To install from a local copy instead of the store, stage and copy the app
folder — see [`docs/installation.md` §A](docs/installation.md#a-home-assistant-os-app).

### Option C — Engine: Standalone Docker

```bash
cp .env.example .env      # fill in HA_URL / HA_TOKEN / WM_API_TOKEN
docker compose up -d
```

### Option D — Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
wattmosaic-engine --check-config
wattmosaic-engine
```

New here? [`docs/user_guide.md` §0](docs/user_guide.md#0-install-and-first-run)
walks through first-run expectations step by step. Details:
[`docs/installation.md`](docs/installation.md),
[`docs/deployment.md`](docs/deployment.md).

---

## Configuration

All configuration is environment-based and documented in
[`docs/configuration.md`](docs/configuration.md). `.env.example` contains
placeholders only. **Never commit `.env`.**

Core variables:

| Variable | Meaning |
| --- | --- |
| `HA_URL` / `HA_HOST` | Home Assistant base URL (port defaults to 8123) |
| `HA_TOKEN` | Long-lived access token (read-only scope is sufficient) |
| `WM_API_HOST` / `WM_API_PORT` | Engine API bind address |
| `WM_API_TOKEN` | Shared token required from the integration |
| `WM_DATABASE_PATH` | SQLite path (`/data/wattmosaic.db` in the App) |
| `WM_LEARNING_ENABLED` | Enable automatic learning |
| `WM_STALE_AFTER_S` | Age after which a reading is stale |
| `WM_RETENTION_DAYS_RAW` | Raw data retention |

---

## Current limitations

- **Topology must be confirmed by you.** Auto-derived CT↔device mappings are
  candidates (`topology_status=candidate`) and are not treated as facts.
- **No per-device ground truth in a real home.** Accuracy figures in
  `docs/acceptance_report.md` come from synthetic ground truth, clearly labelled
  as such.
- **Learning needs declared state entities.** A device's `switch.*`/`climate.*`
  entity is named in `DeviceTopology.metadata["state_entity_ids"]` — never
  guessed from names. Without it, real HA events cannot be linked to devices and
  no learning sample matures (the sample is still recorded with its rejection
  reason).
- Many CT integrations do not expose a hardware measurement timestamp; staleness
  is computed from the HA state timestamp. A typical polling multi-channel CT
  integration reports at ~15.7 s median cadence, not 2 s.
- Double-pole pairing is inferred from channel naming and must be confirmed.
- Deep learning is intentionally not used; the solvers are interpretable.

---

## Development

```bash
pip install -e ".[dev]"
pytest                      # full suite
ruff check engine tools tests
mypy engine
python -m tools.simulator.cli --seed 42 --minutes 120
```

Project tracking: [`PROJECT_STATUS.md`](PROJECT_STATUS.md) ·
[`docs/agent_assignments.md`](docs/agent_assignments.md) ·
[`docs/risks.md`](docs/risks.md)

## License

MIT — see [`LICENSE`](LICENSE).
