# Release Notes

## v0.1.6 — 2026-11-09

Home Assistant was showing this integration's version as a **commit hash**
(`eec273d`), not `0.1.5`. Nothing in the tree was wrong: every version field said
`0.1.5` and the matching container image was published. What was missing was
**metadata** — Home Assistant, HACS and the Marketplace resolve the displayed
version from the repository's git tags and GitHub Releases, and this repository
had never created either. With nothing to resolve they fell back to the branch
head and displayed its SHA. No engine or API change (`/api/v1` stays at 1.1.0).

### Added

- **A release pipeline.** Every release now gets an annotated git tag and a
  GitHub Release whose body is taken from `RELEASE_NOTES.md`, so the version a
  user is offered and the version in the tree cannot disagree. `main` publishes
  stable versions as "latest"; the `dev` branch publishes `-dev.N` builds as
  prereleases, which are never offered as "latest".
- **A release-contract check** (`tools/dev/check_release.py`) that runs in CI
  and in the verification harness. It fails if `manifest.json`, `const.py`,
  `addon/config.yaml`, `pyproject.toml` and the engine `__version__` disagree,
  if the release notes do not document the current version, or if the branch
  carries a version of the wrong kind.

### Fixed

- **The version fields had silently drifted.** The new check found two on its
  first run: `pyproject.toml` and `engine/wattmosaic_engine/__init__.py` both
  still said `0.1.3`, two releases behind. Both now track the release.
- **A documentation-only push could turn the App-image run red.** Its stale-tag
  check compared the published commit SHA against `HEAD`, so a commit that
  touched only documentation failed while building a byte-identical image. It
  now compares the paths that actually enter the image.

### Note for the development channel

`dev` builds numbered `0.2.0-dev1` … `0.2.0-dev9` exist and are unrelated to this
stable release. They remain prereleases and are not offered to stable users.

## v0.1.5 — 2026-11-08

An App install now announces itself, so the integration can be added from a
confirmation screen instead of a container hostname typed by hand — and energy
entities can no longer lose their recorded history when they move onto a real
device. No engine or API change (`/api/v1` stays at 1.1.0).

### Fixed

- **Binding an energy sensor to a real device could orphan its statistics.** The
  energy sensor was attached to the user's device *before* Home Assistant
  registered it. Home Assistant derives a `has_entity_name` entity's `entity_id`
  from the device it is created on, so the same load got
  `sensor.<real device>_estimated_energy` when its declaration resolved and
  `sensor.<engine device>_estimated_energy` when it did not. Declaring a device
  after the first start therefore changed the entity ID, and because the recorder
  migrates statistics only on an explicit rename, every kilowatt-hour already
  recorded stayed behind under the old ID. Entities are now always created on the
  WattMosaic device and *moved* afterwards through the registry
  (`device_id` only), which changes placement but never identity. On the live
  installation this would have orphaned up to 0.47 kWh per device already bound.
  A reload also adopts the stored device rather than moving the entity out and
  back, and a no-op move writes nothing.

### Added

- **The App announces itself (App installs).** `addon/discovery.py` tells the
  Supervisor the app's host and port once the engine answers `/healthz`, which
  Home Assistant turns into a **Discovered** card: one confirmation, no address
  to look up. The host is taken from the Supervisor's own record of the app,
  falling back to the container hostname and then the slug rule, because a
  locally installed app's name carries a repository-hash prefix nothing in the
  UI shows. Only a host and a port are announced — no token, no household data.
  The integration still probes the engine before creating an entry, so a stale
  announcement produces the ordinary connection error instead of a broken entry,
  and a discovered and a hand-typed entry for the same engine cannot duplicate.
  Every failure path is non-fatal: on plain Docker there is no Supervisor to
  announce to and the app still boots.
- **Manual setup is unchanged.** Both paths normalise the URL the same way, so
  they produce the same config entry; the installation guide now covers both.

### Known limitation

Discovery needs `ghcr.io/asxzy/wattmosaic-engine:0.1.5`, which is built only by
the release workflow on a push to `main`. Until that image is published the live
App keeps running `0.1.3` and will not announce itself; add the integration by
hand in the meantime.

### Also fixed

- **A deprecated Home Assistant API call.** Multi-engine service routing used
  `device_registry.async_get_device()` and `device.config_entries`, both
  deprecated in Home Assistant 2026.11 and removed in 2027.8 / 2027.10. The
  replacements do not exist in the 2024.11 minimum, so the lookup is an
  iteration of the registry instead; a static test now fails if any of the three
  deprecated registry APIs reappears (the third is using `registry.devices` as a
  mapping, removed in 2027.9).

## v0.1.4 — 2026-11-08

Per-device energy entities stop freezing after an engine reset, can be turned
off per device, and now attach to the physical Home Assistant device they
describe. No engine, API or contract change (`/api/v1` stays at 1.1.0), so the
App image is unaffected.

### Fixed

- **The energy meter froze permanently after any engine-side reset.** The
  integration pinned a `total_increasing` sensor at its previous value whenever
  the engine reported anything lower, on the belief that Home Assistant "adds
  the whole total again" on a decrease. It does not: HA detects a meter reset
  and continues the sum from the new value. The measured effect was worse than
  the problem — a series of `12.6 → 0.0 → 0.1 → 0.2 → 0.5` kWh published `12.6`
  at every step, so the meter never advanced again and every future kilowatt-hour
  was lost. The engine's total is now published unmodified when the reading is
  publishable, so a reset is visible and statistics resume.
- **A degraded reading could corrupt the energy total.** Stale, missing and
  bad-quality readings are now held: the previous total is republished rather
  than forwarding an untrustworthy value to HA's statistics engine.
- **Editing a device no longer re-enables its energy entity.** The opt-in
  checkbox had a fixed default of on, so turning energy off and later editing
  any other field on the same device would silently turn it back on. The form
  now shows the stored choice.
- **Changing the scan interval no longer discards every energy choice.** Home
  Assistant replaces a config entry's options wholesale, and the settings step
  is a separate branch from the per-device form, so it was dropping the whole
  per-device map. Both paths now carry it through.
- **A stored choice can no longer be misread as "enabled".** The opt-in map was
  re-coerced with `bool()`, under which the string `"false"` is true; options
  restored from an older entry could therefore keep creating an entity the user
  had turned off. The merge is now pure logic and coerces consistently.

### Added

- **Energy entity opt-out per device.** "Create an energy (kWh) entity"
  (default on) lets a load be excluded from the Energy Dashboard. Stored in the
  config entry; never sent to the engine.
- **Energy entities attach to the physical device.** When a device's declared
  state entity belongs to a real Home Assistant device, the energy sensor is
  created on that device instead of a synthetic "Inferred load" device. Power
  and diagnostic entities stay on the synthetic device so inference provenance
  is never mistaken for hardware telemetry.

### Known limitation

Because a decrease is forwarded, a *transient* dip that recovers is counted by
Home Assistant as a reset plus new energy, so it is double-counted. This is
HA's `total_increasing` contract (powercalc behaves the same way); the quality
gate is the mitigation.

## v0.1.3 — 2026-11-07

Fixes the reason a fresh install published no per-device power or energy at
all. No API or contract change (`/api/v1` stays at 1.1.0).

### Fixed

- **A fresh install never bootstrapped a topology.** The engine shipped
  `discover_candidate_topology` and the installation docs promised that it
  "discovers CT channels and proposes device mappings", but nothing called it —
  its only importer was its own unit test. With an empty database every
  inference cycle returned `{"skipped": "no topology configured"}`, that return
  value was never logged, and `/health` still reported `ok`. The integration only
  creates per-device entities from `snapshot.results`, so it published no
  per-device power or energy. Discovery now runs at startup when no topology is
  stored, waits up to 30 s for the first Home Assistant subscription, retries at
  most once per 60 s while nothing is configured, and logs the outcome.
- **Discovery produced an unusable topology.** Its classifier treated any
  channel whose id contained `"main"` as an incoming service feed, which on a
  real 36-channel installation classified 21 channels as mains (every
  `main_area_*` load), and it emitted `devices: []`, which the API rejects
  ("topology must contain at least one device"). Roles are now derived from
  position within the monitor: the shortest paired `ct1`/`ct2` stem is the panel
  feed, exactly two legs are one 240 V device, three or more legs are separate
  circuits sharing a name (defect #28), monitor aggregates are never circuits,
  and every non-main channel is owned by a device.
- **A reversed CT was published as a good zero.** Discovery now derives polarity
  from the observed reading. With the default `normal` polarity a negative
  reading is clipped to zero and reported as `direct_measured` 0 W with
  `quality: good`, which also hides that channel's share of the residual.
- **Vendor hardware names were hard-coded in two published modules.** The setup
  wizard filtered a fixed list of monitor brands from device candidates, and the
  audit tool recognised monitors by brand. Both now use the structural rule: a
  monitor is one hardware device publishing several power entities, and the
  wizard filters the token set shared by every channel. On a real snapshot the
  old brand list matched 33 devices where 4 were monitors.
- **A missing topology reported healthy.** `/api/v1/health` now exposes
  `details.topology = {configured, status, version, channels, devices}` and
  returns `degraded`, with the reason attached, when no topology is configured.
  The silent skip in the inference cycle logs a warning once per process.

### Verified

- Against a saved snapshot of the live installation: 38/38 channels and 28/28
  devices matched the stored topology, zero role disagreements, zero duplicate
  ids, matrix 32 × 28 with no unobserved devices. A real inference run produced
  28 per-device results summing to the measured 5 274.7 W, with the 9 770 W
  residual preserved rather than redistributed (reconciliation error 0.23 %).
- 28 new tests in `tests/unit/test_topology_bootstrap.py`; 15 guards each
  confirmed to fail when removed and pass when restored.
- 953 tests pass; all 10 required gates pass, `validate_addon.sh --no-build`
  59/59 with and without an operator `.env`.

## v0.1.2 — 2026-11-07

Fixes a defect that 0.1.1 exposed. No API or contract change (`/api/v1` stays
at 1.1.0).

### Fixed

- **The health probe re-scanned the whole database every 5 s.** `/api/v1/health`
  ran `PRAGMA integrity_check`, which reads the entire file. On a 392 MB
  database that measured ~1.2 s per call (6.8 s cold), synchronously on the
  event loop. This was harmless while the probe could never authenticate
  (0.1.0), but 0.1.1 fixed the probe, so the container `HEALTHCHECK` began
  polling `/healthz` → `/api/v1/health` every 5 s. Each poll blocked ingestion
  for over a second, and on slow App storage could exceed the 8 s probe timeout
  and flip a healthy engine to `unhealthy`, which lets the Supervisor watchdog
  restart it. Health now runs a cheap liveness query (`SELECT 1`) and reports
  the verdict of a single integrity scan performed at startup; the full scan is
  still available on demand from diagnostics. Measured cost per health call
  dropped from ~1200 ms to 0.006 ms.

## v0.1.1 — 2026-11-07

Fixes the defects that stopped a stock Home Assistant App install from
starting. No API or contract change (`/api/v1` stays at 1.1.0).

### Fixed

- **App never became healthy.** The container `HEALTHCHECK` called
  `/api/v1/health` directly with `WM_API_TOKEN`. That variable is set by
  `run.sh` inside PID 1, and a Docker health probe does not inherit it, so the
  request was always unauthenticated and returned 401. The probe now uses the
  gateway's `/healthz`, which performs the same upstream check with the token
  held in the gateway process. Probe timing was also tightened so a terminal
  health state is reached well inside the Supervisor's 120 s startup window.
- **Core-proxy URL was mangled.** `EngineConfig.from_env` appended `:8123` to
  any URL that had no port, turning `http://supervisor/core` into
  `http://supervisor/core:8123`. `urlsplit` reads the latter as host
  `supervisor` with path `/core:8123`, which the Supervisor rejects with HTTP
  401, so ingestion never connected when `ha_token` was left empty. The default
  port is now applied only to a bare host with no path.
- **Empty `api_token` denied every peer.** The gateway refused all clients when
  no token was configured, contradicting its own log line and breaking the
  integration on a default install. It now serves loopback and the Supervisor's
  internal network (`172.30.32.0/23`) and refuses everything else, so a
  published host port still fails closed.
- **Storage write test ran as root.** `run.sh` checked `/data` writability as
  root and then dropped to UID 10001; a volume owned by the wrong user passed
  the check and the engine died opening the database. The test now runs as the
  service user.

## v0.1.0 — 2026-11-05

First engineering-complete release of WattMosaic.

### Added

**Engine** (`wattmosaic-engine`)

- Read-only Home Assistant ingestion over WebSocket (`get_states`, registry
  queries, `state_changed` subscription). No service-call code path exists.
- Per-channel sample alignment with bounded buffers, staleness and
  repeated-value detection.
- Electrical topology model: mains, branches, dedicated CTs, nested circuits,
  L1/L2 phases, double-pole 240 V loads.
- Measurement matrix `y = A x` with constraints and non-negativity.
- Constrained NNLS disaggregation with identifiability analysis and explicit
  ambiguity reporting.
- Residual analysis that preserves unknown power instead of distributing it.
- Engineering confidence scoring (explicitly not a calibrated probability).
- Device model framework: binary, dimmable, variable-speed, multi-state,
  composite, time-varying appliance, residual.
- Learning pipeline: sample collection, rejection with reason codes, training,
  hold-out validation, promotion with margin, freeze and rollback.
- Operating-stage recognition for multi-stage appliances.
- Energy aggregation with trapezoidal integration over actual elapsed time,
  gap suppression, restart-safe bucket recomputation.
- Versioned SQLite schema (WAL) with migrations, integrity checks, retention,
  model history and export.
- Versioned HTTP API `/api/v1` with optional shared-token auth.
- Diagnostics with full secret redaction.

**Home Assistant integration**

- Config flow and options flow for engine connection, CT roles, phases,
  polarity, double-pole pairing and device mapping (candidates require explicit
  confirmation).
- Per-device sensors: power, energy, source, confidence, sample count, model
  validity, operating stage, running status, last inference, data quality.
- Whole-home sensors: L1, L2, total, direct, inferred, unknown residual,
  reconciliation error, quality score, engine status, learning status.
- Twelve services, with `confirm: true` required for destructive operations.
- Diagnostics download (secrets redacted).
- Entities become unavailable when results are stale or the engine is down.

**Deployment**

- Home Assistant OS App packaging (`addon/`) with ingress, watchdog, persistent
  `/data`, options schema.
- Standalone `Dockerfile` and `docker-compose.yml`.
- Health checks and graceful shutdown.

**Tooling**

- Read-only HA environment auditor (entities, devices, areas, CT inventory,
  duplicates, mapping candidates).
- Passive CT sampling collector and update profiler.
- Passive real-HA validation harness.
- Topology candidate builder from the real audit.
- Synthetic household simulator with ground truth and realistic imperfections.
- Verification harness (`tools/dev/verify.py`) and secret scanner.
- GitHub Actions CI.

### Observed characteristics of a typical multi-channel CT install

| Fact | Value |
| --- | --- |
| Home Assistant | 2026.11.0 |
| Power entities | 58 |
| Energy-monitor panels | 2 (panel A, panel B) + one feed CT pair |
| CT power channels | 36 |
| Typical median CT state-update interval | **15.7 s** |
| Distinct update bursts | yes — up to 69 entities in one poll cycle |
| Original ~2 s sampling assumption | **not supported** |

### Known limitations

- Topology is candidate until confirmed by the user. Auto-derived mappings are
  evidence-based suggestions, not facts.
- No per-device ground truth exists in a real home; accuracy figures come from
  synthetic data and are labelled as such.
- Double-pole pairing is inferred from channel naming.
- Confidence is an engineering score, not a probability.
- Deep learning is intentionally absent; the solvers are interpretable.

### Security

- Read-only toward Home Assistant; no actuation path.
- `.env` gitignored; `.env.example` contains placeholders only.
- Tokens redacted from logs, diagnostics and exports.
- Optional shared-token auth on the engine API.

### Upgrade notes

- This is the first release; no migrations from earlier versions.
- Future releases migrate the database forward automatically. Back up before
  downgrading — migrations are forward-only.
