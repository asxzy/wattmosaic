# Release Notes

## v0.2.0-dev6 — 2026-11-09 (development channel)

Publishes the two accounting fixes that `v0.2.0-dev5` announced but never
shipped: the dev5 image was built before they landed, and the release pipeline
had no way to notice. That pipeline defect is fixed too. **This is a
prerelease. It is installed from the `dev` branch and does not replace the
stable integration or App.**

### Fixed

- **A published version could silently contain older code.** `0.2.0-dev5` was
  built from the commit before the history-correlation and double-pole fixes.
  The fix commit pushed to `dev` without bumping `version:` in
  `addon/config.yaml`, so the image workflow found the tag already present,
  published nothing, and still reported success. The live app kept running
  pre-fix code while the Home Assistant update entity advertised `0.2.0-dev5`,
  and the repaired commands were still rejected. Images are now stamped with
  `org.opencontainers.image.revision`, and an existing tag is accepted only when
  that label names the commit being built; otherwise the workflow fails with the
  version-bump instruction instead of quietly doing nothing.
- The two accounting fixes described under `v0.2.0-dev5` — history correlation
  on sparse-publishing channels, and double-pole pairs being counted twice — are
  now actually present in a published image.

`1324` unit and integration tests pass. The stale-tag guard is negatively
verified: removing the revision label or the non-zero exit makes
`tests/integration/test_release_pipeline.py` fail.

## v0.2.0-dev5 — 2026-11-08 (development channel)

Learns which fixtures sit on which circuit from observed co-movement, so a bank
of dimmers on one CT row no longer requires a manual map — and a dimmer records
the watts it draws at each brightness level rather than a single full-on number.
**This is a prerelease. It is installed from the `dev` branch and does not
replace the stable integration or App.**

### Added

- **Circuit membership from history.** The engine can now watch recorded history
  for a switch that repeatedly tracks one CT row and place the fixture there.
  A candidate requires the switch to have moved, the row to move beyond its own
  measured noise in the expected direction, at least three independent on/off
  *pairs*, at least 80 % agreement, and no competing row. Anything less is
  reported as `unknown` with the reason. Rows measuring the whole installation,
  rows already owned by a single device, and devices you configured yourself are
  never touched.
- **Dimmer watts per level.** When brightness is reported, each cycle records
  `(level, watts)` and the engine interpolates the measured curve at
  attribution time. A light at 40 % reports its watts at 40 %, not its full-on
  value. Without a reported level the full-on value is used and the reason says
  so.
- **An opt-in that fails closed.** "Automatically create devices" is off by
  default. Analysing history is always allowed; creating devices requires the
  option *and* `confirm: true` on the new `wattmosaic.discover_placements`
  service.
- **Devices that only a declaration defines are materialised.** A fixture behind
  a circuit has no channel of its own and previously produced no topology row,
  so its configuration was saved and silently did nothing. It now inherits the
  phase from its CT row and disappears when the declaration is removed.

### Fixed

- **History correlation could never run on a real install.** An observation was
  abandoned unless *every* CT row had a reading on both sides of the transition.
  Many channels publish only on change — one had 8 samples in 24 h — so on a
  real installation 1629 observations collapsed to 0 and the feature silently
  did nothing. A row that did not report is now absent from the observation
  rather than fatal to it; only an observation with no measured row at all is
  dropped.
- **Two circuits paired as one double-pole load were counted twice.** A pair is
  the two legs of *one* device, but nothing enforced it: the discovery heuristic
  pairs anything named `..._ct1`/`..._ct2`, and the engine summed the declared
  partner unconditionally. Two devices declared as a pair therefore each reported
  the other's power as well. The live integration's own energy entities showed
  it — two separate lighting devices both reached 5.0904 kWh. A partner is now a
  leg only when it names the same device, and the wizard rejects the pairing at
  entry.
- **Demoting a dedicated CT left a stale owner.** The channel became a branch
  row while the placeholder still declared the CT as its own, which the graph
  rejected as a type mismatch. The mirror is now rebuilt rather than only
  appended.
- **A brightness change was dropped as attribute churn.** A dimmer that stayed
  `on` and dimmed moved the CT row while nothing recorded why, so the level was
  invisible to inference. A level change is now a real event.

`1435` tests pass and `tools/dev/verify.py` reports **11/11 gates**. Twenty-four
new guards were negatively verified for this change; three clauses that no test
could distinguish were removed instead of kept. See ADR-0024.

## v0.2.0-dev4 — 2026-11-08 (development channel)

Adds the missing control that makes the shared-circuit case configurable at all.
The engine already understood it; no form ever sent it. **This is a prerelease.
It is installed from the `dev` branch and does not replace the stable integration
or App.**

### Fixed

- **A shared circuit could not be declared.** The per-device form offered
  `monitoring: shared` but never sent the CT ids that declare it, and the channel
  wizard's device slot holds exactly one id — so the second lamp on a circuit had
  nowhere to go, and a branch channel's device id is inert even for that one
  device. The device form now offers the engine's CT channels as a multi-select
  and sends `monitored_by_ct_ids`. A CT id is validated as an engine token, not
  an entity id: the engine drops an unknown id silently, so a pasted
  `sensor.foo` would save cleanly and change nothing.
- **Entering the device step directly left the CT picker empty.** The channels
  were only normalised when the wizard had already run, so coming back to edit
  one device — the case the feature exists for — offered nothing. The step now
  loads the topology when it is not already loaded.

`1331` tests pass and `tools/dev/verify.py` reports **11/11 gates**. Seven new
guards were negatively verified for this change; the full table is under
v0.2.0-dev3 below (20 guards in total for the feature).

## v0.2.0-dev3 — 2026-11-08 (development channel)

A WattMosaic measurement is only useful where the appliance already is. Power
**and** energy now attach to the user's existing Home Assistant device, and a
declared switch can settle a shared circuit. **This is a prerelease. It is
installed from the `dev` branch and does not replace the stable integration or
App.**

### Added

- **Power and energy on the real device.** The per-device configuration step now
  offers a Home Assistant device. Selecting one moves the load's power *and*
  energy entities onto that device's page. Source, confidence, evidence tier,
  sample count and the other diagnostics stay on the synthetic "Inferred load"
  device: a model's self-assessment must not look like telemetry from the
  appliance.
- **The picker reaches every real device.** Candidates are the devices that own
  a declared state entity first, then the rest of the registry (a hot tub on its
  own CT usually has no switch). Devices owned by WattMosaic are never offered.
- **Clearing a link moves the entity back.** Dropping a link or removing the
  state entity that justified it returns the measurement to its own WattMosaic
  device. Only `device_id` is written; the entity ID never changes, so long-term
  statistics are not orphaned.

### Fixed

- **A declared CT never reached attribution.** `project_configuration` recorded
  the CTs only in `metadata["monitored_by_ct_ids"]`, which the matrix graph never
  reads. Declaring "this CT measures that device" therefore changed nothing — no
  direct measurement, no attribution row. Declared CTs are now mirrored onto the
  device's `dedicated_ct_ids` or `branch_ct_ids`, with exactly what was added
  recorded so removal retracts only that and leaves wizard-assigned CTs intact.
- **A switch-only shared circuit could never be identified.** The OFF pin
  required a learnt model, so a bank of dimmers on one circuit stayed
  permanently unidentifiable even though the state said which lamps were dark.
  A declared load entity is evidence in its own right and now triggers the pin.
- **An OFF device could be credited power it was not drawing.** The cap was
  `max(off_tolerance, 2 × standby)`, which is still positive with no stated
  standby, so the solver's least-norm tie-break split a row 190/10 and gave 10 W
  to a lamp the user said was off. A device with no stated standby is now pinned
  at zero; a stated standby is honoured (196/4 for a 3 W standby).
- **The relaxation pass could widen a state pin.** It exists because a learnt
  model's bound may be wrong; a state declaration is not a prior. State-pinned
  devices are excluded, and `relaxed_bounds` now names only what was widened.
- **`source` could not report `configured_prior`.** The integration's enum
  omitted a value the engine really returns, so a stated value reported
  `unknown`. The options now derive from the contract.
- **"Measured — dedicated CT" was wrong for a shared row.** The engine promotes a
  corroborated shared-channel estimate to tier 3, but the label claimed a
  dedicated CT. It now reads "Measured — CT row or corroborated share";
  `source` stays `inferred`, which is what the "Measurement source" field shows.
- **A stale device link could pin an entity to a vanished device.** Links are
  re-validated on every start and fall back to entity resolution.
- **A shared circuit could not be declared at all.** The form offered
  `monitoring: shared` but never sent the CT ids that declare it, and the channel
  wizard's device slot holds only one id — so the second lamp on a circuit had
  nowhere to go and the feature existed without a way to reach it. The device
  form now offers the engine's CT channels and sends `monitored_by_ct_ids`. A CT
  id is validated as an engine token, not an entity id, because the engine drops
  an unknown id silently.

### Measured

The projection was exercised against the live installation's real topology. The
hot tub is two paired branch CTs; 600 W + 600 W projected to 1 200 W, and a `2.0`
circuit multiplier applied once gave 2 400 W. Declaring the pair as
`dedicated_device` yields a direct measurement; declaring it as `branch` yields
an attribution row and no direct measurement.

### Verification

1331 tests pass; `tools/dev/verify.py` reports **11/11 gates** (10/10 required);
`ruff check custom_components engine tests` is clean. Twenty guards were
negatively verified — each was removed and its test failed:

| Guard removed | Tests that fail |
| --- | --- |
| CT mirror onto the device list | 8 |
| retraction limited to projected CTs | 1 |
| disabled-channel exclusion | 1 |
| OFF pin on a declared load signal | 3 |
| zero pin | 5 |
| state pin excluded from relaxation | 1 |
| `relaxed_bounds` reports what was relaxed | 1 |
| `configured_prior` in the source enum | 1 |
| `BINDABLE_KEYS` | 1 |
| explicit-link precedence | 2 |
| `CONF_DEVICE_LINKS` on a finish path | 1 |
| unlink returns the entity | 1 |
| stale-link validation | 1 |
| the CT list is emitted to the engine | 2 |
| CT-id shape validation | 1 |
| a direct visit loads the CT channels | 1 |
| projection retraction | 2 |
| an unchanged selection states nothing | 1 |
| clearing the selection is an instruction | 3 |
| a stored CT is only pre-filled when offered | 1 |

### Verified live

`0.2.0-dev3` was installed through the Marketplace and Home Assistant restarted.
The double-pole case was then driven end to end through the options flow, not a
scripted shortcut:

- the device picker offered 461 options (460 real devices plus "keep on the
  WattMosaic device");
- submitting a link alone persisted it on the entry;
- power **and** energy moved to the real appliance's page, while all nine
  provenance entities stayed on the synthetic device;
- entity IDs were unchanged and the energy statistic survived the move;
- the two CTs, declared `dedicated_device`, produced `direct_measured` /
  `tier_3_measured`, and the reported watts equalled leg 1 + leg 2 to within
  0.06 W across six samples.

### Not yet verified

The shared-circuit case (many switches, several CTs, one circuit) can now be
configured but has **no live confirmation**, and it cannot be manufactured: the
Home Assistant registry holds no circuit membership for the 157 dimmer devices,
so assigning a fixture to a CT would be a guess. It needs the user to state which
fixtures each CT feeds, or a switching experiment. Per-device accuracy remains
**unverified**: it needs household ground truth.

A device pinned OFF reports `unknown` / `0.0`, not a measured `0 W`, and the
row's power stays in the residual as `unexplained_load_on_device_circuit`.
Reporting a hard zero would claim a measurement the engine did not make.

A separate, pre-existing limitation was found while probing the dimming case:
with two or more lamps on one row the system is genuinely unidentifiable and the
engine falls back to `model_estimated`, which is **not** clamped by the CT row.
Three dimmers whose models say 100/60/40 W report 200 W on a 120 W row. The
value is labelled `model_estimated` (never `inferred` or `direct_measured`) and
`reconciliation_error_w` exposes the mismatch, so it is not hidden — but it must
not be described as a measurement. Clamping it is a separate engine task; it
would change every model-estimated device in the product and needs its own
negative verification.

## v0.2.0-dev2 — 2026-11-08 (development channel)

Installing the dev build on the live installation exposed that the nested-feed
and duplicate-feed fix **could not take effect through the product surface**.
Two independent wiring defects made the declarations vanish. Both are fixed
here, and both were verified by proving the test fails when the guard is
removed. **This is a prerelease. It is installed from the `dev` branch and does
not replace the stable integration or App.**

### Fixed

- **A declared alias or multiplier was silently discarded.** `CTChannel` has
  carried `alias_of_ct_id` and `measurement_multiplier` since contract 1.2.0,
  but `ct_channels` had no columns for them. `save_topology()` writes an
  explicit column list, so both fields were dropped on every write and
  `load_topology()` rebuilt each channel with the defaults. A user could declare
  "these two CTs measure the same conductor", the wizard would accept it, and
  the engine would still count the wire twice. Migration 6 adds both columns.
- **A nested topology could not be saved at all.** `ct_channels.parent_ct_id`
  is a self-referencing foreign key with `PRAGMA foreign_keys=ON`, and channels
  were inserted in payload order. The wizard sends channels in discovery order,
  which puts a sub-panel feed *before* the service feed it hangs from, so the
  write aborted with `FOREIGN KEY constraint failed`. `save_topology()` now
  inserts parents before children (cycle-safe), and the rejected-cycle path was
  proven to roll back atomically rather than emptying the topology.
- **The options flow could never be opened.** `async_get_options_flow` was a
  module-level function, but Home Assistant looks it up on the config-flow
  class: `OptionsFlowManager` calls `handler.async_get_options_flow(entry)` and
  `async_supports_options_flow` decides `supports_options` by comparing it with
  the base method. The entry therefore advertised `supports_options: false` and
  every options request was rejected with `Invalid handler specified` — a user
  could never change CT assignments or energy toggles after installing. It is
  now a `@staticmethod` on `WattMosaicConfigFlow`, with a test asserting the
  registration shape so it cannot drift back.

### Measured

Replayed over the reference installation's 48-hour history, now going through
the **real storage layer** rather than an in-memory topology:

| | feeds summed | median whole-home |
| --- | --- | --- |
| before, no declarations persisted | 6 | 4975 W |
| after, alias + parent stored and reloaded | 2 | 2100 W |

The notes are emitted as `duplicate_feeds_counted_once` and
`nested_feeds_not_summed`. On the live system the same defect read as ~13 kW
while the canonical feed alone was ~4.9 kW.

### Verification

1256 tests pass; `tools/dev/verify.py` reports **11/11 gates** (10/10 required).
Every new guard was negatively verified: removing the write path, the read path,
the migration, the insertion ordering or the options-flow registration each
makes its test fail, and restoring it makes it pass.

### Measured on the live installation

Both artifacts were installed alongside the untouched stable pair and the four
proven declarations were applied **through the options flow** — the surface that
had been unreachable before this release. At one sampled instant, reading the raw
CT sensors and the integration's own entities in the same response:

| Reading | Value |
| --- | --- |
| service feed L1 (raw CT) | 1128.0 W |
| dev entry `l1_power` | 1128.0 W (exact) |
| service feed L2 (raw CT) | 1536.0 W |
| dev entry `l2_power` | 1536.0 W (exact) |
| **dev entry total** | **2664.0 W** = L1 + L2 exactly |
| sub-panel legs (nested) | 1172.1 W — excluded |
| main-panel legs (alias) | 2584.7 W — excluded |
| naive six-feed sum | 6420.9 W |
| stable entry (0.1.5) | 6434.8 W |

The dev total equals the canonical service feed to the watt while the other four
feeds are present and excluded. Both entries now report
`supports_options: true`.

---

## v0.2.0-dev1 — 2026-11-08 (development channel)

The first build on the `dev` channel. It fixes the two defects that made the
live install report wrong numbers: nested panel feeds were summed as if they
were parallel, and a duplicate measurement of the same conductors was counted
twice. **This is a prerelease. It is installed from the `dev` branch and does
not replace the stable integration or App.**

### Fixed

- **Nested feeds were added together.** A sub-panel feed is measured *again* by
  the CT on the feed that supplies it, so adding every `main` channel counted
  the same watts at two levels. `main_power_w()` now sums only feeds that have no
  `main` ancestor (``effective_feeds()``), and reports what it excluded in
  `MainPower.notes`. On the reference installation the whole-home feed count
  dropped from 6 to 2 and the reported total from ~14.9 kW to ~2.1 kW.
- **The same conductors could be counted twice.** Two CTs reading one wire
  (a load-manager tap and the panel feed it sits on) were both summed. Such a
  pair is now a *duplicate* candidate: reported, never silently merged, and only
  proposed when both feeds are active together, agree instant by instant, and
  are not a constant multiple of one another.
- **A circuit multiplier is no longer inferred.** Some CT hardware publishes the
  already-multiplied value. `measurement_multiplier` therefore defaults to `1.0`
  and is applied exactly once, only when the user sets it. Inferring it from
  `phase` or `ct_type` multiplied real readings by two.

### Added

- **Nesting candidates in the wizard.** `GET /topology/nesting` returns parent
  candidates (a `main` CT measured inside another `main` CT) and alias
  candidates (a duplicate measurement of the same conductors). Both pre-select in
  the wizard and neither is ever written without confirmation.
- **Honest "not inferable" reporting.** A load's level cannot be measured from
  power alone, so branch and dedicated-device CTs are listed under
  `not_inferable` instead of being guessed. Ambiguous parents are reported as
  ambiguous rather than resolved arbitrarily.

### Not changed

- No API version change (`/api/v1` stays at 1.1.0). The topology contract moves
  to `1.2.0`, which adds fields and changes no existing one.

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
