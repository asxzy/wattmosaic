# WattMosaic

Intelligent multi-CT home energy monitoring, **load disaggregation** and
**self-learning** for Home Assistant.

WattMosaic turns the measurements you already have — incoming L1/L2 CTs, branch
circuit CTs, single-subject endpoints (smart plugs and appliance energy monitors)
and Home Assistant device states — into per-device power estimates, with an
explicit, honest treatment of what it cannot know.

> **Status:** contract **3.0.0** — a real-time, topology-aware engine driven by
> measurement events rather than a fixed timer. It does **not** claim real-world
> per-device accuracy; see [`docs/real_ha_validation.md`](docs/real_ha_validation.md).
> The algorithm design and the breaking-change policy are specified in the
> [technical handbook](docs/specs/realtime-topology-disaggregation/handbook.md)
> and its [implementation spec](docs/specs/realtime-topology-disaggregation/README.md).

---

## What it does

- Reads **all** available power and energy entities plus device state changes from
  Home Assistant (read-only, over WebSocket).
- Models the **measurement topology**: incoming endpoints, branch endpoints,
  smart-plug endpoints, nested circuits, L1/L2 phases and double-pole 240 V loads.
  A CT is a *measurement endpoint*, never a device.
- Publishes each estimate with one of four explicit statuses (HB §8.5):
  1. `MEASURED` — a confirmed single-subject endpoint read one subject directly.
  2. `ESTIMATED` — a joint delta/absolute solve, or a validated model priors.
  3. `ALLOCATED` — a share of a non-separable device group, with an allocation rule.
  4. `UNKNOWN` — insufficient evidence; **no fabricated number and no 0 W placeholder**.
     The point value is `None` and justified bounds are carried as attributes.
- Reports **identifiability** independently of status: a non-identifiable member
  gets `None` and justified bounds, never a divided-out group share (HB §8.4).
- Preserves the **unexplained residual** per reader instead of distributing it.
- Learns device models from **delta (ΔP) evidence** on confirmed observations;
  legacy whole-circuit models are read-only and retrained from delta evidence.
- Tracks appliance cycles, operating stages, runtime and energy.
- Exposes per-device, per-group and whole-home sensors in Home Assistant. Unknown
  values are published as `unknown`, so history shows honest gaps rather than
  fabricated zeros.

## What it deliberately does not do

- It does not switch devices, change settings or control anything.
- It does not guess when loads are ambiguous. If two devices changed at once and
  the meters cannot distinguish them, it says so — or reports the identified
  group sum, not invented per-member watts.
- It does not treat a repeated sensor value as a new measurement.
- It does not report a confidence number as a probability.

---

## Architecture

```text
Home Assistant ──state_changed / reader samples──► Engine ingestion
                                                      │
                     ┌────────────────────────────────┼──────────────────┐
                     ▼                                ▼                  ▼
              fast measurement loop            event loop          background loop
              (O(1) per sample)             (event groups)        (learning, retention)
                     └────────────────────────────────┼──────────────────┘
                                                      ▼
                              topology / mapping / model versions
                                                      ▼
                                       joint delta + absolute inference
                                                      ▼
                                estimates + justified bounds + residual
                                                      ▼
                                           SQLite (WAL, versioned)
                                                      ▼
                                        HTTP API /api/v1 → matrix
                                                      ▼
                           WattMosaic HA Integration ──► sensors & groups
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
| Incoming + smart plug / appliance-monitor endpoints (`SINGLE_SUBJECT` scope) | Yes — `MEASURED` once the user confirms the scope |
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
| `WM_EVENT_QUEUE_MAX` | Bound on ready event groups in the event loop |
| `WM_STEADY_HEARTBEAT_S` | Staleness-only heartbeat period (never a full solve) |
| `WM_UNKNOWN_PENALTY_SCALE` | Multiplier on the unknown-load penalty in the joint solve |

`WM_INFERENCE_INTERVAL_S` is **deprecated and ignored**: the three-loop runtime
runs inference per event group, not on a fixed timer. It is still accepted so
existing App options keep validating, with a startup warning.

---

## Current limitations

- **Topology and endpoint scope must be confirmed by you.** Auto-derived topology
  edges and `SINGLE_SUBJECT` scopes start as candidates (`CANDIDATE`); a single
  event never promotes an edge past `CANDIDATE`, and PROVISIONAL edges are soft.
- **No per-device ground truth in a real home.** Accuracy figures in
  `docs/acceptance_report.md` come from synthetic ground truth, clearly labelled
  as such.
- **Learning needs declared state entities.** A device's runtime entity is named
  in its HA entity links — never guessed from names. Without it, real HA events
  cannot be linked to devices and no learning sample matures.
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
