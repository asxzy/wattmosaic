# Architecture Decision Records

Format: ID · date · status · context · decision · consequences.

---

## ADR-0001 — Engine owns all inference

**Status:** Accepted · 2026-11-05

**Context.** Two implementations of the math (one in HA, one in the engine) would
diverge and both would be untrustworthy.

**Decision.** The HA integration is a thin REST client. All inference, learning
and persistence live in the engine.

**Consequences.** Integration is restart-cheap and stateless beyond entity
restoration. The engine must be reachable for entities to be available, which is
acceptable and explicit.

---

## ADR-0002 — Read-only HA access via WebSocket

**Status:** Accepted · 2026-11-05

**Context.** REST `/api/states` polls lose cadence information and cannot observe
transitions between polls.

**Decision.** Subscribe to `state_changed` over WebSocket; use read-only
registry commands. No service-call path exists in the codebase.

**Consequences.** True transition timestamps are captured. Requires robust
reconnect logic. Production safety is structural, not policy-based.

---

## ADR-0003 — `last_updated` is not a measurement timestamp

**Status:** Accepted · 2026-11-05 · **Evidence:** `data/ct_update_profile.json`

**Context.** Median observed CT state-update interval is **15.7 s**, not the
assumed ~2 s. Some channels exceed 30 s. Home Assistant exposes no hardware
sample timestamp for a polling power integration.

**Decision.** Every measurement carries `state_timestamp`, `received_at` and an
optional `physical_timestamp`. Staleness is computed from `state_timestamp`.
Repeated identical values are flagged `repeated=True` and are not counted as new
evidence for learning.

**Consequences.** Event windows are derived from observed cadence
(configurable), never a hardcoded 2 s. Learning samples from stale channels are
rejected with `stale_measurement`.

---

## ADR-0004 — SQLite with WAL and versioned migrations

**Status:** Accepted · 2026-11-05

**Context.** HA OS App containers have modest resources; a separate database
server would be disproportionate.

**Decision.** SQLite in WAL mode, `PRAGMA user_version` migration tracking,
atomic per-migration transactions, bounded retention.

**Consequences.** Single-writer model; all writes serialized through one
`Database` instance. Crash recovery via WAL replay. Future migration to
PostgreSQL is possible behind the repository boundary.

---

## ADR-0005 — Interpretable solvers, no deep learning

**Status:** Accepted · 2026-11-05

**Context.** False attribution is worse than admitting ignorance.

**Decision.** Start with robust baselines, constrained NNLS, residual analysis,
identifiability checks. Use probabilistic/deep methods only with demonstrated,
measured improvement.

**Consequences.** Every estimate can be explained. Ambiguity is an explicit
output. Accuracy may be lower than a black box but is honest.

---

## ADR-0006 — Unknown residual is preserved, never distributed

**Status:** Accepted · 2026-11-05

**Context.** Forcing device estimates to sum to mains creates plausible but
fabricated values.

**Decision.** `Reconciliation.unknown_residual_w` is a first-class output.
Devices with insufficient evidence get `source=unknown` and no invented power.

**Consequences.** Users see honest accounting. Acceptance tests assert the
residual is preserved.

---

## ADR-0007 — Confidence is an engineering score

**Status:** Accepted · 2026-11-05

**Context.** A number in [0,1] invites interpretation as probability.

**Decision.** Confidence combines measurement quality, supporting CT count,
model validation, event isolation, topology certainty and residual magnitude.
`confidence_is_calibrated` is always `false` until a calibration study exists.

**Consequences.** UI labels it "confidence score", not "probability".

---

## ADR-0008 — Topology is candidate until confirmed

**Status:** Accepted · 2026-11-05

**Context.** Name-token matching produced plausible but unverified CT↔device
links (e.g. "Bar Fridge 1/2").

**Decision.** Auto-derived topology is `topology_status=candidate`. Inference may
run with reduced confidence but the user must confirm relationships. Confirmed
topology bumps a version.

**Consequences.** No silent commitment of wrong electrical assumptions.

---

## ADR-0009 — Separate main-panel measurement groups

**Status:** Accepted · 2026-11-05

**Context.** A typical multi-panel install exposes two energy-monitor units
plus a separate feed CT pair. Their nesting is not documented anywhere
accessible read-only.

**Decision.** Model them as separate measurement groups. Do **not** sum all six
main channels. Provide configurable containment for when the user confirms.

**Consequences.** Prevents double counting the most consequential quantity in the
system. Documented as an open ambiguity.

---

## ADR-0010 — Double-pole loads are one logical device

**Status:** Accepted · 2026-11-05, amended 2026-11-09

**Context.** Several loads appear as channels named `*_ct1` / `*_ct2`, one leg
per phase. Naming alone does not prove they are a pair.

**Decision.** A paired CT set is one `DeviceTopology` with `double_pole=true` and
`phase=BOTH`. Its power is the sum of both legs, modelled once.

**Amendment — a pair belongs to one device, and only the engine may assume it.**
`paired_ct_id` is free text: the discovery heuristic pairs any two channels whose
ids end `_ct1`/`_ct2`, and a user can pair anything. Summing a declared pair
unconditionally meant that two *independent* circuits, each already its own
device, each reported the other's power as well. That happened on a real install
and was visible in the integration's own energy entities: two separate lighting
devices both reached 5.0904 kWh over the same window.

A partner is therefore the other leg only when it exists, is enabled, and names
the **same `device_id`** (`TopologyGraph.double_pole_leg`). `attribution_ct_ids`,
`most_specific_branch_group` and `resolve_direct` all consult it, and
`validate_assignments` rejects the pairing at entry with `pair_crosses_devices`.
The alternative — dropping pairing altogether — would halve a genuine 240 V
load, so the rule is ownership, not the absence of a declaration.

**Consequences.** Prevents treating a 240 V appliance as two appliances and
prevents double counting. A cross-device declaration is now an error the user
sees, rather than silent duplicate attribution.

---

## ADR-0011 — Three orthogonal device axes

**Status:** Accepted · 2026-11-06

**Context.** Devices were previously described by a single implicit dimension
(how they were measured). That is not enough to decide what can honestly be
claimed. A non-smart garage opener on a dedicated CT is *fully measured* but has
no control signal; a smart dimmable lamp visible only through the home total is
*controllable but barely measured*; a thermostat emits a control signal that
frequently does not correspond to its own burner. Collapsing these into one
enum forces a false choice and loses the information needed to attribute power.

**Decision.** Every device is described by three independent axes:

1. **HA connectivity** — `connected` (HA has an entity and may be able to start
   it) / `not_connected` (non-smart; only the CT sees it) / `unknown`.
2. **Energy monitoring** — `dedicated` (a CT measures it and nothing else) /
   `shared` (a sub-meter carries it plus others) / `home_total_only` (no
   sub-meter, only the home energy monitor) / `unmonitored` / `unknown`.
3. **Power behaviour** — `fixed` (constant watts while running or idle:
   always-on lamp, garage opener operating) / `variable` (power follows a control
   value: dimmable light, variable-speed fan) / `complex` (multi-state controller
   where the emitted signal may not prove load, e.g. thermostat fan/ventilation/
   humidifier).

Monitoring is derived from **co-occupancy of attribution CT rows**, not from
`CTType`: a device that owns a branch CT alone is dedicated in fact even when
that CT is nominally a "branch" channel. Only when another topology device
shares the rows is a device `shared`.

Configuration is **projected into topology metadata** rather than stored in a
parallel channel, so ingestion, inference and learning cannot disagree. The
projection bumps the topology version. `state_entity_ids` is evidence-owned and
is never rewritten: user declarations live in `config_load_entity_ids`.

**Consequences.**

* Removing a configuration restores the device metadata exactly.
* A user-declared `shared` overrides the CT graph. The row then becomes an
  **upper bound**, not this device's measurement: level-2 inference is blocked
  for it, the solver receives a slack column for the row, and a value may only
  come from a configured prior (Tier 1).
* A `shared` row whose siblings are all modelled stays solvable — the slack is
  only added when siblings are unmodelled, otherwise a legitimate multi-device
  separation would be thrown away.
* All axes default to `unknown`, so a partially filled configuration is honest
  rather than silently defaulted.

---

## ADR-0012 — Mode entities are context, never load evidence

**Status:** Accepted · 2026-11-06 · **Evidence:** 7-day recorder history

**Context.** Treating any entity attached to a device as proof of runtime
fabricates load. Typical observations from a real install:

| Entity kind | Observation | What it actually is |
| --- | --- | --- |
| `climate.*` thermostat | setpoint mode (`heat_cool`) in the overwhelming majority of rows | mode, not runtime |
| `water_heater.*` tank | `comfort` in ~96 % of rows | controller mode |
| `water_heater.*` on a spa/pool | `Cooling` 106 / `Heating` 60 / `Idle` 24 | real multi-state controller, but `Cooling` ≈ 22 W while `Heating` ≈ 4.5 kW |
| `binary_sensor.*_zone_heating` | tracks its CT | actual runtime |

The global `ON_STATES` set also wrongly contains `cooling`, which would turn a
22 W standby state into a 4.5 kW heater.

**Decision.**

1. A **mode** entity never contributes load and never proves runtime. It only
   selects which profile/stage applies.
2. Runtime must come from a **declared load entity** (`state_entity_ids` or
   `controller.load_entity_ids`) or from the CT. If neither exists, the device
   falls through to Tier 1/0 rather than inventing watts.
3. A controller label mapped with `role=load` **and** `confirmed=true` *may*
   supply a watt value — an explicit opt-in, because it is an assertion rather
   than a measurement.
4. Domain rules are conservative: `climate`, `water_heater`, `select`,
   `input_select`, `scene`, `script`, `automation`, `button`, `event` and
   `update` are mode-only; `binary_sensor` (with `running`/`heat`/`cool`/
   `occupancy`/`power`-style device classes), `light`, `switch`, `fan`,
   `humidifier`, `media_player`, `vacuum`, `valve`, `cover` and
   `input_boolean` are runtime.
5. A `climate`/`water_heater` entity declared as a state entity is not rejected.
   It is **deterministically reclassified** to mode with a
   `mode_entity_reclassified:<entity>` reason. This is the natural user mistake;
   an error would help less than a transparent correction.

**Consequences.** A state value in `device_states` is only evidence when the
topology declares a load entity for that device (`has_declared_load_signal`).
Without it, Tier 4 (`verified`) is unreachable — verified by hand before pinning.

---

## ADR-0013 — Confidence tiers grade evidence; the score measures trust

**Status:** Accepted · 2026-11-06

**Context.** ADR-0007's numeric score ranks two devices well but is a poor label:
`0.62` does not tell a user whether the number came from a dedicated CT or from
an unvalidated model.

**Decision.** Report an **ordered evidence grade** alongside the score:

| Tier | Meaning | Typical score |
| --- | --- | --- |
| `tier_0_unknown` | no evidence supports a device-level value | 0.00–0.25 |
| `tier_1_modelled` | a configured prior or a model only | 0.20–0.65 |
| `tier_2_inferred` | separated from CT rows, uncorroborated | 0.45–0.85 |
| `tier_3_measured` | a CT measured it, or a corroborated share | 0.30–0.97 |
| `tier_4_verified` | measured **and** independently corroborated, fresh | 0.60–1.00 |

Tier 4 requires: a declared load signal or a model agreeing within tolerance,
**and** a fresh sample, **and** no contradiction between the CT and the declared
state.

The tier and the score are **independent outputs**. The score is never rewritten
to fit the tier: clamping it into a per-tier band erased the difference between a
fresh and a repeated sample within one tier, which is a real distinction. When
the two diverge (a stale dedicated CT keeps its evidence grade while its score
collapses) a `tier_score_divergence:<score>_outside_typical_<low>_<high>` reason
is attached instead of hiding it.

Neither value is calibrated. `confidence_is_calibrated` remains `false`, and
`GET /api/v1/confidence_tiers` reports `calibrated: false` per tier.

**Consequences.**

* The tier answers *how the value was obtained*; the score answers *how much to
  trust the number right now*.
* Tiers are persisted on `inference_results` (migration 5), so history keeps its
  evidence grade rather than silently degrading to `tier_0_unknown`.
* Reports stay stratified by tier, device type and monitoring level; a single
  aggregate accuracy figure is still forbidden (the underlying truth set does
  not exist).

---

## ADR-0014 — A power entity is measurable, not state-bearing

**Status:** Accepted · 2026-11-06 · **Evidence:** live `device_events` table

**Context.** An entity↔device link serves two different purposes, and conflating
them is destructive. A learning sample needs to know **which CTs measure** a
device, and that includes the CT's own power entity (`sensor.water_heater_power`). But
the device's *runtime* state (`on`, `off`, `Heating`) is a separate question,
answered by a `switch`/`binary_sensor`/`light`.

The engine kept one link map and wrote any linked entity's value into
`device_states[device_id]`. Numeric watt readings do not reach that path —
`HASource` consumes them as measurements and returns early — but availability
changes do. A CT bridge blip (`unavailable`, `unknown`, `restored`) replaced the
real state, and the next genuine reading of the runtime entity looked like a
transition *back*. One blip therefore manufactured two phantom transitions. The
live `device_events` table holds 16 such rows across 16 CT power entities.

A second conflation made per-device configuration inert: user-declared runtime
entities were correctly projected to `config_load_entity_ids` (ADR-0011), but the
link map only read the evidence-owned `state_entity_ids`, so a configured switch
was never observed at all.

**Decision.**

1. Links carry **both** kinds of entity, because measurement needs both.
2. Only **runtime state entities** — `state_entity_ids` ∪
   `config_load_entity_ids` ∪ `link_state_entity_ids` (ADR-0028) — may be written
   into `device_states`.
3. A CT power entity's event is still persisted (it is a real observation) but is
   counted under `power_events_not_state` and never becomes device state.
4. `state_key` follows the same rule: an entity that is not a runtime source is
   passed through under its raw entity id, never mapped onto a device key.

**Consequences.** Runtime state survives CT churn, so state-transition inference
and learning see real transitions. The held-out count is visible in `/status` and
Prometheus, so the distinction is auditable rather than silent. A device with no
runtime entity honestly has no state — it is never given one by a power reading.

---

## ADR-0015 — A silent channel is stale, not zero

**Status:** Accepted · 2026-11-07 · **Evidence:** live `ct_samples`, `state_changed`
cadence

**Context.** Most power integrations are **push-on-change**: they publish a new
state only when the value changes. A circuit that is genuinely idle therefore
stops publishing, and its last state sits unchanged at exactly `0.0 W`. In a
real installation 8 of 38 CT channels behaved this way, with
`min == max == 0.0` over 47 samples while changing channels accumulated ~2 570
samples at a 15.7 s cadence. Home Assistant's `last_updated`, `last_changed` and
`last_reported` were all equally stale, so the silence is upstream and real —
not an engine or bridge fault. Two of the eight recovered later, when the
circuit drew power, which proves the sensor was alive the whole time.

After the buffer retention (240 s) elapses, `SampleAligner` prunes the channel
entirely and the window reports `missing`. Six devices that depend on those
channels consequently report `quality_status=missing`, and the integration marks
their power entities `unavailable`.

The tempting fix is a periodic `get_states` resync, which would re-observe the
held `0.0` and restore a value. That fix is wrong for a reason worth recording:
the value is not new. Re-observing it does not make it a measurement.

**Decision.**

1. **A channel that has not published is `stale`/`missing`, never `0 W`.** The
   engine does not manufacture a reading from silence, and it does not publish a
   held value as a current measurement.
2. **Age is measured from `state_timestamp`, never from arrival** (ADR-0003). A
   resync would therefore produce `age_seconds ≈ 7 200`, `quality=stale`, and
   `is_usable() == False` — so a resync changes nothing user-visible and is not
   implemented.
3. **A stale or missing channel does not zero the device.** The device reports
   `quality_status=missing` with `power_w=0.0` flagged
   `power_w_is_placeholder: True`; the real power stays in
   `Reconciliation.unknown_residual_w` (ADR-0006).
4. **A fresh `0.0` is still a measurement.** Push-on-change is not the only
   behaviour: a sensor that polls on a timer reports `0.0` with a current
   `last_updated`. Freshness is decided by the timestamp, never by the
   value.

**Consequences.** An idle circuit shows `unavailable` rather than a confident
`0 W`. That is honest but visually noisy, so the cause is documented
(`docs/troubleshooting.md` §3) and the channel is named in the quality report
(`missing_sample (no sample in window)`). A device whose circuit genuinely
switches between off and a fixed load can be configured instead of relying on
the CT. The engine keeps no "held value" concept: there is one rule, and it is
the timestamp rule.

---

## ADR-0016 — Health is judged by the process that holds the credential

**Status:** Accepted · 2026-11-07

**Context.** The App ships a Docker `HEALTHCHECK` and a Supervisor watchdog. The
engine API is token-protected, and the token reaches the container as an App
option that `run.sh` maps to `WM_API_TOKEN` *inside PID 1*.

A Docker health probe is a separate `exec` built from the container
configuration, not from PID 1's runtime environment. In 0.1.0 the probe called
`/api/v1/health` directly and read `WM_API_TOKEN` from its own environment,
which is always empty. Every probe was therefore unauthenticated, the gateway
returned 401, and Docker never reported the container healthy. The Supervisor
waited 120 s, logged `Timeout while waiting for app … to start`, and its
watchdog restarted the app on every `UNHEALTHY` event — indefinitely.

**Decision.**

1. **The probe asks the gateway, not the engine.** `healthcheck.py` calls the
   gateway's unauthenticated `/healthz`, which already performs the real check
   (engine `/api/v1/health` with `ok|degraded`, database integrity, HA
   connection state) using the token the gateway holds.
2. **Secrets are not duplicated into the container environment.** The
   alternative fix — declaring `WM_API_TOKEN` in `config.yaml`'s `environment:`
   — would copy the secret in plaintext and put it in `docker inspect`. It is
   rejected.
3. **The probe must reach a terminal state inside the startup window.** The
   Supervisor waits 120 s. `start_period + retries x (interval + timeout)` must
   stay below it; 0.1.0's 90 + 3 x 30 = 180 s could never report in time.

**Consequences.** One credential holder per concern: the engine holds the HA
token, the gateway holds the API token, and the probe holds nothing. A probe can
no longer pass by being more privileged than the process it is checking. The
timing bound is asserted by a test, so a future tweak that reintroduces a
too-slow probe fails CI rather than a user's install.

---

## ADR-0017 — An empty API token narrows the audience; it does not disable the API

**Status:** Accepted · 2026-11-07

**Context.** `api_token` is optional. 0.1.0 logged
"the engine API is unauthenticated (acceptable only inside the Supervisor
network)" but the gateway did the opposite: with no token configured it returned
401 to *every* peer that was not the Supervisor ingress proxy. The integration,
which runs in Home Assistant Core, could not reach the engine at all on a
default install — and the health probe could not either (ADR-0016).

**Decision.**

1. **No token means "no credential to check", not "deny everything".** The
   gateway serves peers that can only be inside the App's own Docker network:
   loopback and the Supervisor network (`172.30.32.0/23`). Everything else is
   refused.
2. **A published host port still fails closed.** Those networks do not include
   a host-published mapping, so exposing the port without setting `api_token`
   does not open the API.
3. **The log line must describe what the code does.** It now names the actual
   audience instead of claiming the API is unauthenticated everywhere.

**Consequences.** The default install works without a token, which is the
documented behaviour, and the security posture is unchanged for anyone who
exposes the engine. `api_token` remains the mechanism for trusting clients
outside the Supervisor network.

---

## ADR-0018 — Liveness probes must not do verification work

**Status:** Accepted · 2026-11-07

**Context.** ADR-0016 made the container probe ask the gateway's `/healthz`,
which proxies `/api/v1/health`. That endpoint called
`Database.integrity_check()`, which issues `PRAGMA integrity_check` — a scan of
the *entire* database file. On a 392 MB database it measured 1.2 s warm and
6.8 s cold, executed synchronously on the asyncio event loop.

The two defects composed: while the probe could never authenticate (0.1.0) the
expensive path was dead code; the moment 0.1.1 fixed authentication, the probe
began running a whole-file scan every 5 s. Measured in the real 0.1.1 container
against a real database, `/healthz` took **1.1–2.3 s per call**. That blocks
ingestion and inference for the same duration, and the 8 s probe timeout leaves
little margin on slow App storage — a timeout reports `unhealthy`, which the
Supervisor watchdog answers by restarting the app.

**Decision.**

1. **Separate liveness from verification.** `Database.ping()` is `SELECT 1`;
   that is what `health()` calls. `Database.integrity_check()` remains the full
   scan and is never called from a probe path.
2. **Verify once, report always.** `EngineCore.verify_integrity()` runs at
   startup, logs the verdict, and caches it. `health()` reports the cached
   verdict, so a corrupt file still surfaces as `error` instead of looking
   healthy because nothing re-scans it.
3. **Expensive work is opt-in.** Diagnostics may still run the full scan on
   demand; nothing scheduled may.

**Consequences.** Health cost dropped from ~1200 ms to 0.006 ms per call in
process (26 ms over HTTP, dominated by the gateway hop). A probe failure now
means the process is genuinely unwell, not that it happened to be scanning its
database. The rule generalises: anything the Supervisor polls on a timer must be
O(1), because a slow probe is indistinguishable from a dead app.

---

## ADR-0019 — A fresh install must bootstrap a candidate topology, and health must say so

**Status:** Accepted · 2026-11-07

**Context.** The engine shipped `discover_candidate_topology` and
`docs/installation.md` promised that "WattMosaic discovers CT channels and
proposes device mappings". Nothing called the function: its only importer was
its own unit test. On an installation with an empty database the engine started,
connected to Home Assistant, ingested every sample, and returned
`{"skipped": "no topology configured"}` from every inference cycle. That return
value was never logged and `/health` reported `ok`.

The user-visible result was exactly what a bug report described: the integration
existed and the CT-level entities existed, but no per-device power or energy was
ever published. Every probe was green because nothing the engine *reported* was
false — the engine simply had nothing to attribute power to and said nothing
about it.

Wiring discovery up as written would not have helped. Its classifier treated any
channel whose id contained `"main"` as an incoming service feed, which on a real
36-channel installation classified 21 channels as mains, and it emitted
`devices: []`, which the API's own validation rejects ("topology must contain at
least one device").

**Decision.**

1. **Startup bootstraps a candidate, once.** `EngineCore.bootstrap_topology()`
   runs after ingestion starts, only when the database holds no topology, and
   only ever writes a **candidate**. It never overwrites an existing topology,
   never confirms one, and never invents device state links.
2. **Wait for the first state snapshot.** `HASource.start()` only *schedules* the
   connection and returns, so an immediate read sees an empty cache. Startup
   therefore waits up to `TOPOLOGY_BOOTSTRAP_WAIT_S` (30 s) for the first
   subscription.  An empty state list is reported as **deferred**, not as
   "no monitor": the two conditions have different remedies.
3. **Retry while nothing is configured.** If Home Assistant is slow, restarting,
   or down at startup, the inference cycle retries discovery at most once per
   `TOPOLOGY_RETRY_INTERVAL_S` (60 s).  Without this the engine would sit inert
   until the next process restart — the same silent no-op, just narrower.
4. **Discovery is structural.** The anchor is a hardware device exposing several
   power entities (a multi-channel monitor), never a brand name and never a bare
   smart plug. Roles come from position inside that monitor: the shortest paired
   `ct1`/`ct2` stem is the panel feed. A stem with exactly two legs is one
   240 V device; three or more legs are separate circuits sharing a name
   (defect #28); monitor aggregate entities (`_total_power`, `_sum`) are never
   circuits. Every non-main channel is owned by a device, so no measured circuit
   is left unattributable.
5. **A missing topology is not `ok`.** `/health` reports
   `details.topology = {configured, status, version, channels, devices}` and
   returns `degraded` when `configured` is false, with the reason attached.  The
   silent skip in `run_inference_once` logs a warning once per process instead
   of returning a value nobody reads.

**Consequences.** A fresh install is useful immediately after it connects: the
candidate topology is enough to publish per-device power and energy, and the
integration creates those entities dynamically as results appear. The user still
confirms roles, phases, pairings and state links; a candidate is never presented
as fact. Health now distinguishes "running and working", "running with nothing to
do", and "broken", which is the distinction the original report could not make.

Discovery remains heuristic and is labelled as such. It cannot know that a
circuit is a *named* appliance, cannot resolve a device with no state entity, and
cannot split two appliances behind one branch CT — that is a topology the user
must author, and `run_inference_once` preserves the residual rather than guessing.

---

## ADR-0020 — Energy publishes the engine's total; it does not pin it

**Context.** The integration published `InferenceResult.energy_kwh` through
`util.monotonic_energy()`, which returned the previous value whenever the engine
reported anything lower. The stated reason was that Home Assistant "would add the
whole total again" on a decrease. That reason is wrong.

Home Assistant's statistics compiler (`components/sensor/recorder.py`,
`_compile_statistics`, `TOTAL_INCREASING` branch) calls `reset_detected()`, which
treats a drop below 90 % of the previous value as a meter reset, then continues
the sum from the new value. It does **not** re-add the old total. Publishing the
raw total after a database reset therefore adds only the post-reset energy.

Pinning was worse than the problem it claimed to solve. Because the engine's new
total can never climb back to a value that was pinned during a reset, the sensor
stops advancing permanently. A measured series (12.6 → 0.0 → 0.1 → 0.2 → 0.5 kWh)
publishes `12.6` at every step: the meter is frozen for the life of the entity,
and all future energy is silently lost.

**Decision.**

1. **The engine owns the total; the integration publishes it verbatim** when the
   reading is publishable, including a decrease. A reset becomes visible in Home
   Assistant and statistics resume.
2. **Publishability gates the value.** A stale, missing or bad-quality reading is
   not forwarded; the previous total is republished unchanged. Without this gate
   one degraded sample could hand a bogus total to the statistics engine.
3. **`util.published_energy()` is the only rule.** It is pure and tested for
   increasing, equal, reset, rollback, degraded and missing readings.

**Consequences.** A genuine reset no longer freezes the meter. The trade-off is
stated rather than hidden: a *transient* dip that recovers is counted by Home
Assistant as a reset plus new energy, so it is double-counted. This is Home
Assistant's `total_increasing` contract and is exactly what powercalc does; the
quality gate is the mitigation. A future revision could smooth dips, but only
with evidence about which dips are trustworthy — and none is available today.

**Also decided here.** Energy entities are opt-out per device (`create_energy_sensor`,
default on, matching powercalc) and bind to the user's real Home Assistant device
when a declared state entity resolves to one. Power, source and diagnostic
entities stay on the synthetic device so inference provenance never masquerades
as hardware telemetry. Entities owned by this integration are never binding
targets: that would make an inferred load the host of its own sensor.

---

## ADR-0021 — Entity identity must not depend on where the entity is displayed

**Status:** Accepted · 2026-11-08

**Context.** v0.1.4 attached the energy sensor to the user's real Home Assistant
device by setting `entity.device_entry` in `__init__`, before Home Assistant had
registered the entity. That is what powercalc does for its *power* sensors, and
it is the only way to influence placement at creation time.

It has a consequence powercalc does not have to care about. Home Assistant
derives a `has_entity_name` entity's `entity_id` from **the device the entity is
created on**: `entity_platform._async_get_entity_device()` resolves the device
before `entity_registry._async_generate_entity_id()` runs, and
`_async_get_full_entity_name()` prefixes the device name. So the same engine
device produced two different IDs depending on whether its declarations happened
to resolve:

* bindable → `sensor.<real device>_estimated_energy`
* not bindable → `sensor.<engine device>_estimated_energy`

A device that became bindable *after* its first start therefore changed its
entity ID. The recorder migrates long-term statistics only on an explicit rename
(`components/recorder/entity_registry.py`, `_async_entity_id_changed` →
`async_update_statistics_metadata`), never when an ID is re-derived while the
registry entry is created. Statistics are keyed by `statistic_id == entity_id`,
so every existing energy statistic for that device was silently orphaned and the
meter restarted from zero under a new name.

**Decision.**

1. **Create every entity on the WattMosaic-owned device.** `_attr_device_info`
   is always the synthetic "Inferred load" device, so the derived ID depends only
   on engine device names, which are stable.
2. **Move the energy entity afterwards**, in `async_added_to_hass`, via
   `entity_registry.async_update_entity(entity_id, device_id=…)`. Changing
   `device_id` updates placement only; the entity ID and its statistics are
   untouched.
3. **`entity_values.binding_update()` returns `{"device_id": …}` and nothing
   ever carries a rename.** It returns `None` when the entity is already where it
   belongs, so a reload does not rewrite the registry or fire registry events.
4. **Adopt an already-bound device on reload.** `_adopt_registered_device()`
   looks up an existing registry entry by unique id and, only when one exists,
   sets `device_entry` so the entity starts on the right device instead of
   flapping through the synthetic one on every restart. A brand-new entity has no
   entry and is therefore never placed by the binding result.

**Consequences.** One entity ID and one statistics series per engine device, for
the life of the installation, regardless of whether, or when, a real device is
declared. The energy sensor still appears on the plug/thermostat/charger page
that describes it. Placement can change between restarts without any recorder
migration, which is correct: the recorder has nothing to migrate.

The trade-off is one extra registry write on the first start of a newly
bindable device. That write is idempotent and observable in the registry log;
silently orphaning statistics is not.

**Verification.** `tests/integration/test_entity_id_stability.py` transcribes the
relevant HA 2026.11.0 functions (`_async_derive_object_ids`,
`_async_get_entity_device`, `_async_get_full_entity_name`,
`_async_generate_entity_id`, `async_get_available_entity_id`, `slugify`) and
models the registry plus statistics keys. It reproduces the orphan
(`sensor.water_heater_estimated_energy` vs `sensor.water_heater_thermostat_estimated_energy`,
0.47 kWh left behind) and proves the shipped ordering keeps one ID and one
series.

A transcription alone could drift from the code it is meant to describe, so
`TestTheModelMatchesTheShippedSource` reads `sensor.py` and asserts the premise
holds: `__init__` sets `_attr_device_info` and never assigns `device_entry`;
`_bind_real_device` is called from `async_added_to_hass` and not from
`__init__`; and `_adopt_registered_device` looks the registry entry up before it
ever assigns `device_entry`. The model is then driven with the actual choice.

All of this is negatively verified. Reinstating the `__init__`-time binding fails
`test_init_creates_on_the_synthetic_device_only` and
`test_binding_happens_after_registration_not_in_init`; removing the
`_bind_real_device()` call from `async_added_to_hass` fails
`test_the_move_is_inside_async_added_to_hass`; unguarding adoption fails
`test_adoption_is_guarded_by_an_existing_registry_entry`; and adding a rename to
`binding_update()` fails the rename and idempotency tests.

---

## ADR-0022 — An App install announces itself to Home Assistant

**Status:** Accepted · 2026-11-08

**Context.** A user who installs the App has to type a container hostname into
the integration's connection step. That hostname is not fixed: the Supervisor
registers each App on its private network under the slug with `_` replaced by
`-` (`supervisor/apps/model.py`, `hostname = slug.replace("_", "-")`), and a
locally installed App is additionally prefixed with a repository hash —
`f48de52e-wattmosaic-engine` in the observed installation. Nothing in the App's
own UI tells the user that string, and getting it wrong is the point where setup
stalls.

Home Assistant has a purpose-built mechanism for this. An App lists the
integration domains it can feed under `discovery:` in `config.yaml`
(`supervisor/apps/model.py`, `App.discovery`), then POSTs
`{"service": <domain>, "config": {...}}` to the Supervisor
(`supervisor/api/discovery.py`, `SCHEMA_DISCOVERY`). The Supervisor rejects a
service the app did not declare (403). Home Assistant receives the message via
`hassio_discovery.py` and starts a config flow with
`async_step_hassio(discovery_info: HassioServiceInfo)`. `POST /discovery` is in
the Supervisor's `api_bypass` pattern, so no `hassio_api: true` is needed — only
`homeassistant_api`, which the App already has.

**Decision.**

1. **The App publishes discovery from `addon/discovery.py`**, on start, after the
   gateway's `/healthz` answers. Failure is never fatal: with no
   `SUPERVISOR_TOKEN` (plain Docker, CI, laptop) it logs and exits 0, and a
   Supervisor refusal is retried a bounded number of times and then abandoned.
2. **Announce a host and a port only.** No token, no household data. The message
   is stored by the Supervisor and readable within the installation.
3. **Resolve the host in a defined order:** `WM_DISCOVERY_HOST`, then
   `GET /addons/self/info` `hostname` (authoritative, already includes a local
   install's hash prefix), then `socket.gethostname()`, then the slug rule.
   Each candidate must be a bare host name and must not be loopback; a candidate
   that fails is skipped and the next one is tried, so a bad override degrades
   to the Supervisor's record instead of announcing a message no one can use.
4. **Pin the scheme to `http`.** Discovery only ever comes from the Supervisor's
   private network, where plain HTTP is the only transport. Honouring an
   announced `scheme` would let a message redirect an installation.
5. **Both sides apply the same host rule.** The integration rejects an announced
   host that carries URL syntax (`user@evil.example`, `host/path`,
   `http://host`, `host:9000`, `host?q`) rather than repairing it — repairing is
   how `user@evil.example` becomes `http://evil.example` and how `host/path`
   folds the port into a path. It also rejects loopback. A parametrised test
   asserts that the publisher and `discovered_base_url` agree host by host.
6. **The integration probes before creating anything.** `async_step_hassio`
   normalises the announced URL through the same `normalise_base_url` the manual
   step uses, sets the unique id from it, and calls `_probe_engine`. A stale
   announcement (app removed, engine down) therefore yields the ordinary
   `cannot_connect` error, never a broken entry, and a discovered and a
   hand-typed entry for the same engine cannot be duplicated.
7. **A malformed announcement hands over to the manual step.** It does not abort
   the flow, because the user still has a working path.
8. **Discovery is not the only path.** The manual step remains, both produce the
   same entry, and the strings explain what to do when the card does not appear.

**Consequences.** A fresh App install offers a one-screen confirmation with the
URL filled in. The App image must be published for this to reach an installation
(the Supervisor pulls `image:<version>` from `addon/config.yaml`), so the live
App discovers only after `0.1.5` exists in the registry.

**Verification.** `tests/unit/test_discovery_publisher.py` covers the message
shape, the host-resolution order, loopback rejection, skipping a candidate the
integration would reject, the slug fallback, publish success/403/unreachable,
the bearer header, `main()`'s non-fatal paths, `--dry-run`, and — host by host —
that the publisher and `discovered_base_url` accept and reject the same values.
`TestDiscoveredBaseUrl` covers string/numeric/absent/invalid ports, rejection of
URL syntax in the host field, equality with the manual normalisation, and that
an announced scheme cannot downgrade the URL.
`test_app_manifest_declares_the_discovery_domain` fails when `discovery:` is
removed from `config.yaml`; `test_config_flow_has_the_hassio_discovery_step`
fails when the step is renamed; the publisher's `SUPERVISOR_TOKEN` test fails
when it raises instead of skipping outside the Supervisor; and the shared-host
tests fail if either side's rule changes without the other.

---

## ADR-0023 — Attach measurements to the existing device, and let a declared switch settle a shared row

**Status:** Accepted · 2026-11-08 · Supersedes the "energy only" part of ADR-0020/0021
· **The "single ON claimant owns the row" rule and its verification list are superseded by ADR-0026**

**Context.** Two ordinary installations showed that a measurement is not useful
unless it lands where the appliance already is.

*One CT, one appliance.* A 240 V hot tub has two hot legs and a monitor that
reports the circuit twice (two CTs, or one circuit multiplier). Once the user
declares "these CTs measure that device", the device's page should show its
watts and kWh. Before this change only the kWh entity moved, so the hot tub page
showed an energy meter with no power reading, and the W reading sat on a
synthetic "Inferred load" device next to it.

*Many appliances, one circuit.* A bank of smart dimmers shares one lighting
circuit. Each dimmer is a device in Home Assistant with a `light` entity whose
on/off state and brightness are known. The CT measures the circuit, not the
lamp. Two lamps on at once are genuinely not separable by linear algebra, but a
lamp that is *positively off* is drawing nothing: the single lamp that is on owns
the row.

powercalc, the reference model, resolves an existing `DeviceEntry` and sets
`entity.device_entry` (or patches the registry after registration); it never
creates a device. Precedence is `sensor_config[CONF_DEVICE]` →
`config_entry.data[CONF_DEVICE]` → `source_entity.device_entry`. Its "measured"
strategy is a passthrough of an already-measured sensor; it has no strategy that
uses a measured power sensor as ground truth for a virtual sensor.

**Decision.**

1. **Power and energy both attach to the real device.** `BINDABLE_KEYS` is
   exactly `{power, energy}`. Every other entity — `source`, `confidence`,
   `confidence_tier`, `sample_count`, `model_validity`, `operating_stage`,
   `last_inference`, `data_quality` — stays on the synthetic inferred-load
   device. Provenance is a statement about the inference, not about the
   hardware; showing a model's self-assessment on the appliance page would
   present it as telemetry from the appliance.
2. **An explicit link wins, entity-derived resolution is the fallback.** The
   link is `{engine_device_id: ha_device_id}` in the config entry
   (`CONF_DEVICE_LINKS`). It never travels to the engine: the engine has no
   concept of a Home Assistant device id, and sending one would make a display
   decision look like electrical knowledge. An installation that never opens the
   new step is unchanged, because the entity-derived path stays.
3. **The link is validated on every start.** A stored id can name a device that
   was deleted or replaced, or one this integration owns. An unusable link falls
   back to entity resolution, and clearing a link moves the entity back onto its
   WattMosaic device — binding is the only thing that writes `device_id`, so
   "no target" must mean "return", not "leave it where it last was".
4. **Declaration is projected into attribution.** `DeviceConfiguration.
   monitored_by_ct_ids` previously wrote only `metadata[monitored_by_ct_ids]`,
   which the matrix graph never reads. It now mirrors the CTs onto
   `DeviceTopology.dedicated_ct_ids` (a `dedicated_device` role) or
   `branch_ct_ids` (a branch or unclassified role), records exactly what it added
   under `PROJECTED_CTS_KEY`, and retracts only that on removal. A CT assigned
   through the channel wizard is untouched. Disabled and unknown channels are
   never mirrored.
5. **The declaration must be reachable from the product.** A channel carries one
   `device_id`, so a bank of lamps on one circuit could not be described by the
   channel wizard at all: the second assignment overwrote the first, and a branch
   channel's `device_id` is inert even for that one device (attribution reads
   `DeviceTopology.branch_ct_ids`, which only `monitored_by_ct_ids` writes).
   The per-device form therefore offers the engine's CT channels as a multi-select
   and sends `monitored_by_ct_ids`. A CT id is validated as an engine token, not
   an entity id: the engine drops an unknown id silently, so a pasted
   `sensor.foo` would save cleanly and change nothing. Because the picker is
   pre-filled with the stored declaration, resubmitting it unchanged sends
   nothing (so opening a device to edit an unrelated field does not create a
   configuration row), while *changing* it does — including clearing it, which is
   the only way to undo a wrong declaration.
5. **A declared load signal makes an OFF state a constraint.** The solver already
   pinned an observed-OFF device at its standby draw, but only when a learnt
   model existed, so a switch-only shared circuit stayed permanently
   unidentifiable. A *declared* load entity is evidence in its own right
   (ADR-0012), so the pin fires on that too. An undeclared state key still proves
   nothing.
6. **An OFF device with no stated standby is pinned at zero.** The old cap was
   `max(off_tolerance, 2 × standby)`, which let the solver's least-norm tie-break
   invent a share: two lamps on one row split 190/10 and credited 10 W to a lamp
   the user declared off. With no evidence of any draw the cap is the smallest
   positive float. A stated standby is honoured and still measured rather than
   assumed.
7. **A state pin is never relaxed.** The model-bound relaxation pass widens a
   bound only when the CT rows disagree with a *model*. A state constraint is
   evidence, not a prior, and widening it would reintroduce the fabricated
   attribution the pin exists to remove. The diagnostics report only what was
   actually relaxed.

**Consequences.** A hot tub with a declared CT pair now reports its measured
watts on the hot tub page, and its `source` stays `direct_measured`. A bank of
dimmers on one circuit reports per-lamp watts when exactly one is on and the rest
are positively off; `source` is `inferred` and the tier is 3 ("a declared load
signal corroborates a shared-channel estimate"), never 4. Two lamps on at once,
or a sibling that is `unknown`/`unavailable`, still reports `unknown` and leaves
the row in the residual. The `unknown` state deliberately blocks the rule: an
offline sensor might be drawing.

The shared case is only usable because the declaration now reaches the engine
from the product. Nothing was missing in the engine: it accepted
`monitored_by_ct_ids` and its tests covered the single-ON rule while the
integration never sent the field, so the feature existed with no way to configure
it — the same class of defect as the alias and multiplier columns that were
dropped on every write.

Amended wording: ADR-0020/0021 said only *energy* moves to the real device. That
is superseded. The identity rule from ADR-0021 is unchanged and still load-
bearing: every entity is created on the WattMosaic device and moved afterwards,
so a link change never re-derives an entity ID and never orphans statistics.

**Verification.**

* `tests/unit/test_device_attachment.py` — declared dedicated CT becomes a
  direct measurement (`direct=500.0`); declared branch CT becomes an inference
  row and not a measurement; removal retracts the projection and returns
  `attribution_ct_ids == ()`; a wizard-assigned CT survives a removal; a disabled
  channel is never attributed; a single ON claimant owns the row at 200.0 W with
  `source=inferred`; the OFF device receives under 1e-6 W; a stated 3 W standby
  yields 196/4; two ON, both OFF, one `unavailable` and one missing state each
  report `unknown`; removing the declared signal restores `unknown`; and a stale
  100 W model bound is relaxed for the ON lamp while the OFF lamp's pin is not.
* `tests/integration/test_device_link.py` — `BINDABLE_KEYS`, provenance
  exclusion, explicit-link precedence and validation, unlink returning the
  entity, per-device form helpers, that the CT list reaches the engine command,
  that a CT id is not accepted in entity-id form, that the detail step offers the
  engine's channels and loads them on a direct visit, and that every options
  completion path rebuilds the whole link map (Home Assistant replaces `options`
  wholesale).
* Negative verification: each guard was removed and the corresponding test
  fails. Removing the zero pin fails five tests; removing the declared-load
  signal fails three; skipping the CT mirror fails seven; retracting everything
  instead of only what was projected fails the retraction and wizard-assigned
  cases; accepting disabled channels fails the disabled case; dropping
  `CONF_DEVICE_LINKS` from a finish path fails the persistence case; narrowing
  `BINDABLE_KEYS` fails the bindable-key case; ignoring the explicit link fails
  the precedence case; removing the unlink return fails the return case; dropping
  the CT list from the command fails two; removing CT-id validation fails one;
  and not loading the channels on a direct visit fails one.
* Live environment facts used: 518 devices in the registry, 460 not owned by
  WattMosaic, 81 `light.*` entities, 58 WattMosaic-owned entities excluded from
  the picker. The hot tub is two paired branch CTs (`l1`/`l2`), and the
  projection was exercised against that exact shape (1 200 W from 600 + 600).

---

## ADR-0024 — Circuit membership may be learned from co-movement, but only when it is unambiguous and the user asked

**Status:** Accepted · 2026-11-08 · Extends ADR-0023

**Context.** ADR-0023 let a *declared* switch settle a shared row: the user says
which circuit a lamp is on, and exactly one lamp being positively ON while its
siblings are positively OFF credits the row to that lamp.

That still requires the user to enumerate the fixtures. A real installation has
81 `light.*` entities across three floors, and the bank of dimmers on a single
circuit is a fraction of them. Asking a person to map every one to a CT row
is not a workflow; it is a data-entry job, and the half-finished result is worse
than nothing because an unlinked dimmer is simply invisible.

The tempting shortcut is to let the engine guess from co-movement. That is also
the fastest way to attribute a real household's power to the wrong appliance and
never notice: a correlation that is wrong produces a plausible, self-consistent
number forever. powercalc takes the other road and never creates devices at all.

**Decision.**

1. **Correlation is a pure function with no side effects.**
   `ingestion/correlation.py` maps observations to candidates and writes
   nothing: no registry, no names, no I/O, no database. A test can read a verdict
   without a topology, and the write path cannot accidentally acquire evidence.
2. **Every clause is a refusal, not a score.** A candidate exists only when all
   of these hold, and each failure is reported with its own reason:
   * the switch **actually moved** — a row that wanders while the entity sat
     still proves nothing about that entity;
   * the row moved beyond `max(MIN_RESPONSE_W, MIN_RESPONSE_SIGMAS × row noise)`,
     where the noise is that row's own median absolute difference, so a quiet row
     needs less absolute movement than a busy one;
   * the **direction agrees** — turning on must raise the row;
   * at least `MIN_CORROBORATIONS = 3` independent on/off **pairs** agree;
   * at least `MIN_AGREEMENT = 80%` of the entity's usable transitions are
     explained by that row;
   * **no competing row** responds on `AMBIGUITY_SHARE = 50%` or more of the same
     transitions;
   * observations older than `MAX_AGE_S` (14 days) are ignored (wiring changes,
     bulbs get replaced);
   * only `light`/`switch`/`fan`/`input_boolean` are candidates — a mode-only
     domain proves nothing about load (ADR-0012) — and `unavailable`/`unknown`
     are not transitions (ADR-0015).
3. **A corroboration is one on/off pair, not two edges.** Counting the rising and
   falling edge separately would let a *single* toggle satisfy a minimum of 3 and
   defeat the guard entirely. A pair counts when either of its two transitions
   was explained by the row.
4. **The measured step is the load, and the baseline is never attributed.** A
   transition that raised the row by 19.57 W places a 19.57 W load on it; any
   standing load on the same row stays in the residual (ADR-0006). This is what
   keeps a CT's own unmodelled third load out of the fixture.
5. **A dimmer records watts per level, not one full-on number.** When the
   control entity reported `brightness`, each cycle contributes
   `(level, watts)` — measured only on the **off** edge, so one cycle is one point
   and the two halves of a single toggle cannot corroborate each other. `on` with
   no brightness yields *nothing*, which is "no dimming evidence", never "100%".
   The curve is held in `LightConfiguration.dimming_curve` as a fraction of
   full-on (a fixture description) and converted to watts once, in
   `_measured_curve()`, using the stated full-on value. Without that value the
   curve is dropped rather than reporting fractions as watts.
6. **A created device claims a *prior*, not the row.** The placement carries the
   correlated step as `expected.on_w`, is `MonitoringLevel.SHARED`, and reports
   `configured_prior` / `tier_1_modelled`. It deliberately does not become the
   row's owner, so the whole-row rule cannot over-report several fixtures on one
   circuit. Live dimming interpolates the measured curve; with no reported level
   the flat full-on value is used, and the reason says which.
7. **Creation is opt-in, separate from acceptance, and never overrides a person.**
   `plan_placements` refuses: a `main` row; a row owned by a dedicated
   measurement (the whole row is already one device — creating a second claimant
   double-counts the circuit, and the reported remedy is to convert it to a shared
   circuit, never an automatic demotion); an entity the user configured
   (`is_user_asserted`); and a non-positive step. `configured_by="auto"` means an
   auto placement is never user knowledge and never raises a tier.
   `merge_placements` replaces auto placements wholesale on each run — so a
   fixture that stops correlating is withdrawn — while touching no user row.
   Auto placements are excluded from the "already configured" check, or a second
   run would treat its own output as a user declaration and withdraw the device
   it just created.
8. **The write happens only on explicit consent.** The engine flag
   (`auto_placement`, `WM_AUTO_PLACEMENT`) defaults to **off**; the integration
   stores the user's choice (`CONF_AUTO_PLACEMENT`) and states `apply` on every
   call; `apply: true` additionally requires `confirm: true` at the service
   layer. Analysis without `apply` is always allowed because it changes nothing.
   An explicit `apply` in the request beats the stored option, so a caller asking
   to analyse only never gets writes.
9. **History is enough for a first build; live events carry the rest.** The
   recorder is queried read-only. Home Assistant's recorder stores only
   `device_id`, `zone_id` and `friendly_name` per row for these lights even with
   attributes requested, so a dimmer's level→watts curve cannot be learned from
   history on this installation — it is learned from live `state_changed` events,
   which do carry `brightness`. A brightness-only change is therefore a real
   event: dropping it as "attribute churn" made the level invisible while the row
   moved for no recorded reason.

**Consequences.** A bank of eight dimmers on one circuit can be mapped from a
day of history instead of a manual form, and each lamp's energy entity lands on
its own real device (ADR-0023). Every attribution remains labelled `inferred`;
the tier stays 1 and the provenance is visible. Two fixtures measured on a real
circuit recovered their steps exactly at 100 % agreement, and a dimmer measured
at 40 % → 8.1 W and 100 % → 19.9 W reports 8.10 W at 40 %, 14.00 W at 70 %
(interpolated) and 19.90 W when the level is not reported.

What this does **not** establish: per-device accuracy against household ground
truth, which is unverifiable here and stays unverified. Six lighting rows in
the live installation are declared as dedicated single-device measurements, so
auto-creation refuses them until each is converted to a shared circuit; that
refusal is the ownership guard doing its job, not a defect.

**Verification.**

* `tests/unit/test_correlation.py` — two fixtures measured on a real circuit
  (19.57 W and 13.39 W on a row with an 8.35 W standing load and 0.039 W noise)
  recovered with 3 corroborations and 100 % agreement; a single coincidence, a step inside the
  noise floor, a row moving the wrong way, two equal rows, a lone dominant row, a
  mode-only domain, `unavailable` blips, a transition with no reading on either
  side, an inconsistent switch (4 of 6 transitions, still 3 pairs), a competing
  partial response, stale observations, and a burst inside the dedup gap each
  refuse with their own reason; a dimmer learns `[(0.4, 8.1), (1.0, 19.9)]` from
  off edges only and reports full-on watts as `typical_w`.
* `tests/unit/test_auto_placement.py` — materialisation on the declared row with
  the phase inherited from that row, removal deleting it, two fixtures on one row
  both materialising, an owned row refusing with the conversion message and still
  being credited exactly once at 28.00 W `direct_measured`, conversion to a branch
  unblocking it, a curve scaled from fractions to watts, 40 % → 8 W and
  70 % → 14 W interpolation, no level → full-on with a distinct reason, a
  malformed level degrading to "not reported", and an out-of-range level held at
  the measured endpoint.
* `tests/integration/test_ha_integration.py` — the opt-in fails closed for a
  missing, false, string, numeric and nonsense value and enables only on explicit
  consent; analysis needs no confirmation; `apply: true` without `confirm: true`
  is refused; an explicit request beats the stored option; the service returns
  its response; and it is not in the globally destructive set.
* `tests/unit/test_discover_placements.py` — the orchestration between
  correlation, placement and the recorder, against a real `Database`: analysis
  alone writes nothing; `apply=None` follows the stored option while an explicit
  `apply=False` overrides it; an explicit `apply=True` writes the devices with
  `topology_version` advancing; the created device is materialised and inherits
  the row's phase; a second run neither duplicates nor withdraws; a dimmer
  records a curve from recorded brightness while a switch without levels gets
  none; history is requested with attributes; a main row that *does* track a
  switch is refused at correlation (not merely filtered later); an undeclared
  entity is never requested and never placed, including when the source
  over-returns; no history and no declared entities are reported with a reason
  rather than as a clean empty result; and the command rejects a non-numeric or
  out-of-range lookback.
* Negative verification: each guard was removed and a test failed — response
  threshold, agreement floor, ambiguity guard, pair counting, mode-domain filter,
  age filter, dedup gap, direction check, non-transition filter, stale-reading
  window, main-row refusal, owned-row refusal, user-declaration precedence,
  auto-not-user-asserted, zero-step refusal, withdraw-only-auto, materialisation
  blocker, curve-requires-full-on, opt-in default, apply confirmation, the
  response-service list, the orchestration's own opt-in/`apply` handling, the
  attribute request, the empty-history reason, main-row exclusion from the
  correlation inputs, the declared-entity history request, and the
  over-returning-source re-filter. Three clauses were found *not* load-bearing
  during this and were removed rather than kept as unprovable protection: a
  second curve gate in `_measured_level` (duplicating `configured_power`'s), a
  level clamp there (duplicating `interpolate_curve`'s, which is tested), and a
  second level filter in `discover_placements` (levels are only read for
  entities the filtered state map yielded, so no test could distinguish it).

  Writing those tests also exposed a fixture trap worth recording, because it
  looks exactly like a regression: `observations_from_history` uses a strict
  `<` for the "before" reading, so a synthetic recording that polls on the same
  cadence as it toggles emits a sample *exactly* on each transition carrying the
  old value, and every measured step is zero. A helper that fills the "off"
  interval with "on" watts fails the same way. Both produce
  `unresolved: no_row_responded` and nothing else.
* Live field evidence: 32 CT channels; a lighting row with an 8.35 ±0.02 W
  standing load and fifteen fixtures off; two fixtures measured at +19.57 W and
  +13.39 W on that row. Real history produced 700+ CT rows and row noise
  0.039 / 0.958 / 0.061 W, and recovered the larger fixture at 19.59 W on /
  19.54 W off at `min_corroborations=2`.

---

## ADR-0025 — A single-channel CT is a branch circuit, and a declared wattage bounds what that row may credit

**Status:** Accepted · 2026-11-09 · Extends ADR-0023 and ADR-0024 · **Amended by ADR-0026** (the ceiling now also caps a measured step)

**Context.** Discovery classified every single-channel load as
`CTType.DEDICATED_DEVICE`. The only discriminator was "this channel was not part
of a `ct1`/`ct2` leg pair", which is true of every one-CT lighting circuit in
existence. The classification was not evidence; it was an assumption that looked
like knowledge.

It mattered in two ways. It made a discovery heuristic indistinguishable from a
user declaration — the product asserted "a private measurement" where the
engineer had never said so. And it was the sole blocker for auto-placement: a
`dedicated_device` row is refused as "already another device's measurement", so
`plan_placements` accepted 0 of 15 correlated fixtures. Reclassifying the
fallback to `branch` produced 15 accepted, 0 refused.

But reclassifying exposed a real fabrication. With a lighting row as a branch
circuit carrying three configured fixtures that declared 243.6 / 112.3 / 112.7 W,
a row at its observed 1 036 W peak, one fixture ON and its siblings positively
OFF, the solver credited the entire row to the one ON lamp — 4.25× its declared
draw — and labelled it `tier_3_measured`. The same shape appeared on other
circuits at 402 %, 250 % and 217 % of their claimants' declared draw.

This was pre-existing: the same over-attribution happens on a
`dedicated_device` row, except there it is correct by construction. The
reclassification only made it possible for a *branch* row to do it to a
*named* fixture.

**Decision.**

1. **Discovery emits `branch` for a single-channel circuit.** One CT measures
   one conductor, not one appliance. The device is still emitted so the
   topology validates and the circuit is visible, but it owns the row as
   `branch_ct_ids` with `shared_circuit: True` metadata and no channel
   `device_id`. `dedicated_device` comes only from a declaration.

2. **A declared wattage is a non-relaxable upper bound on a shared row.**
   `EffectiveConfig.config_ceiling_w` is applied as the solver upper bound for
   every configured claimant that is not alone on its row and not state-pinned.
   The relaxation pass — which exists so a stale *model* bound can be widened —
   never widens it: a declaration is user evidence, not a revisable prior. A
   solo row is exempt, because there the CT is a direct observation.

3. **A capped value is graded as a declaration.** When the bound binds and the
   row still carries unexplained power, the result is `configured_prior` /
   `tier_1_modelled` with `declared_wattage_bounds_row_attribution`. Reaching
   the ceiling with the row fully explained is a real measurement and stays
   `tier_3_measured`.

**Rejected alternatives.**

* *Add a slack to every branch row.* Measured: three 156 W lamps on a 468 W row
  collapsed to 117 W each and the solver reported an infeasible problem. The row
  is genuinely separable while the reading is within the claimants' sum.
* *Exclude the row whenever it exceeds the claimants' ceilings.* The same
  measurement works, but only when every claimant states a wattage; discovery's
  structural placeholder devices have none, so the rule silently never fires on
  exactly the rows that need it. The upper bound applies per device and does not
  depend on the placeholder's silence.
* *Treat the placeholder as zero.* "No wattage stated" and "declares no load"
  are not the same claim, and the engine cannot distinguish a user's unstated
  lamp from discovery's placeholder.

**Consequences.** The 15 correlated fixtures can be placed. Every attribution on
a shared row is bounded by what the user said the fixture draws, so undeclared
load stays in the residual instead of becoming a plausible-looking permanent
number. Discovery no longer asserts a fact only a person can supply.

**Verification.**

* `tests/unit/test_topology_bootstrap.py` — the three separate lighting
  circuits are no longer fused into one dedicated device and all three classify
  as `branch`; a single-channel circuit is not declared a private measurement.
* `tests/unit/test_device_attachment.py` — a 1 036 W row with a 150 W declared
  claimant is capped; the same row within the declaration is still measured; a
  declared ceiling is never relaxed; a capped value is graded
  `configured_prior`/`tier_1_modelled`; reaching the ceiling with no surplus
  stays `tier_3_measured`.
* Negative verification: disabling the `declared_ceiling_hit` downgrade made the
  grading test fail (`inferred` instead of `configured_prior`).
* Live topology (post-reclassification, 32 `branch` / 6 `main`, 28 devices, 15
  placements): all five shared-row peak cases report `configured_prior` /
  `tier_1_modelled` at the declared step (243.61 / 49.21 / 74.85 / 20.02 /
  139.29 W) instead of absorbing the row.

---

## ADR-0026 — A CT is a circuit; attribute the measured step, not the whole row

**Status:** Accepted · 2026-11-10 · Supersedes ADR-0023's whole-row credit and amends ADR-0025

**Context.** ADR-0023 let a declared switch settle a shared row: exactly one lamp
ON, its siblings *positively OFF*, therefore the ON lamp owns the row. ADR-0025
then bounded that credit with a declared wattage, which fixed the over-attribution
found on rows whose fixtures stated their draw.

Both rules share an assumption that was never checked: **the declared fixtures
are the whole circuit.** When they are, the single-ON rule is sound. When they are
not — which is the normal case, because no one has enumerated every lamp on a
lighting circuit — the leftover belongs to something else, and the rule hands it
to whichever fixture happens to be on.

Two measurements on the instrumented installation settled this.

*The defect is not the role model, it is the credit.* The live topology was
already a circuit model: 32 `branch`, 6 `main`, **0** `dedicated_device`, and
every "device" on a lighting row was a structural placeholder. No CT is declared
private to any appliance. The failure was purely in attribution and presentation.

*Declaring more truth made the answer less safe.* On one lighting row reading
**881.72 W** with a single declared fixture that stated no wattage, the row was
held as an upper bound and the answer was `unknown` — safe. Adding a **second**
declared fixture, one ON and one OFF, neither stating a wattage, caused the engine
to credit **881.72 W to the single ON lamp** with `source=inferred` and
`tier_3_measured`. With stated wattages (150 W / 100 W) the same row was correctly
bounded and the surplus stayed in the residual. So `shared_unmodelled` — the guard
meant to protect the row — was inverted from safety: it fired when the device was
*alone* and went quiet the moment a sibling was declared.

The mechanism was `bool(observed) and not siblings`. Its intent was "a single
claimant on a shared row has nobody to share with, so protect the row"; its effect
was that the protection disappeared exactly when a second claimant made the
whole-row credit look plausible.

**Decision.**

1. **Every CT is a circuit, and the engine attributes a *change*.** No CT is
   treated as dedicated to a device unless the user declares it so. Where a row is
   shared, the evidence that survives is the measured movement: the row changed by
   *N* watts at the instant one device on it changed state, and nothing else on
   the row changed. That movement is the device's draw, measured — it does not
   require the siblings to be OFF, only to be unchanged.

   Stated by the user, verbatim: *"No need to require all siblings to be off. As
   long as all siblings' status is stable, and service can correctly infer the
   watts changes before and after the device's status changes constantly, we say
   the change is the watts of the target device."*

2. **The delta rule fires only under all of these conditions.** Every one is a
   refusal; any failure leaves the device `unknown` and its watts in the residual.
   * the device's row is **shared** (a solo row keeps the whole-row answer, so a
     single fixture under a CT is never made worse);
   * a step was detected on one of the device's **attribution rows** — never on an
     enclosing main, which may have moved for any other circuit;
   * **every sibling on that row held steady** — no sibling has a non-`None`
     transition direction in this window;
   * the device has a **declared load entity** (a state key that is not declared
     proves nothing, ADR-0012);
   * the transition is an **activation**;
   * the step **direction agrees** (a rising row for an activation);
   * the reading is **not stale** and the device does not report **OFF**;
   * the step is **clamped to the row**: a change cannot exceed the value it
     changed;
   * the step is **dropped to zero if it exceeds the device's declared wattage**
     (`step_exceeds_declared_wattage`). A 400 W movement on a row whose fixture
     declares 50 W is not that fixture being measured; the surplus is something
     else on the circuit, so the step is not this device's draw at all.

3. **A deactivation step is evidence, never current power.** On a deactivation the
   step says what the device drew a moment ago. Reporting it as the device's
   present power would be wrong on the device page and would double count against
   a row that has already dropped. The explanation carries
   `shared_circuit_step_measured_on_deactivation` and the magnitude stays in
   `diagnostics["ct_steps"]`; the reported value is whatever the OFF state
   justifies — zero, or a stated standby.

4. **A measured step grades as a measurement.** `source` stays `inferred`,
   because a shared row still did not measure the device privately, but the
   evidence tier is `TIER_3_MEASURED` with reason `shared_circuit_step_measured`.
   The tier grades evidence, not the shape of the CT (ADR-0013).

5. **The un-wattaged-claimant guard is repaired.** `shared_unmodelled` now also
   fires for a declared device on a row shared with another declared fixture when
   it states no wattage. That restores the safe answer for the case that was
   measured above, and it cannot make a solo row worse.

6. **An OFF state is never worth the aggregate model estimate.** Found while
   verifying this change: `_state_power` substituted `estimated_power_w` whenever
   the model had no value for the state, including a positively-OFF state, so an
   OFF device was reported at its full running power. "No model value for this
   state" is now the answer, and a stated standby remains the only non-zero value
   an OFF device can carry.

**Rejected alternatives.**

* *Keep ADR-0023 and require the user to enumerate the whole circuit.* It cannot
  be enforced — the engine cannot distinguish a complete fixture list from a
  partial one, which is exactly what the 881.72 W case exploited. A rule that
  depends on an unverifiable completeness claim is not a guard.
* *Attribute the whole row minus the siblings' declared wattages.* This
  fabricates a number whenever a sibling's wattage is unknown, and it silently
  absorbs undeclared load into the target device — the failure ADR-0006 exists to
  prevent.
* *Require every sibling positively OFF, plus the delta.* Strictly weaker: it
  discards the common case where a second lamp is on and steady, which the
  measurement can still resolve. The user's rule is the more informative one and
  the measured step is what makes it safe.
* *Report the deactivation step as the device's power.* Measured as wrong: it
  reports a device the user just switched off as drawing its previous load, and
  double counts the drop that the row already contains.

**Consequences.** A lighting circuit with several fixtures reports each fixture's
*measured* contribution at its transitions and leaves the standing load in the
residual, instead of reporting one fixture's full row. A device that is the only
declared fixture under its CT is unaffected and still reports the whole row
(verified: a 1 200 W solo row reports `inferred` / `tier_3_measured`). A
concurrent sibling change correctly blocks attribution rather than splitting the
step.

The cost is honest: between transitions a shared row has no per-fixture answer, so
a device that has not changed state recently reads `unknown` rather than a
plausible permanent number. That is the intended trade — an admitted gap, not a
fabricated value.

**Verification.**

* `tests/unit/test_device_attachment.py` — the five ADR-0023 tests that asserted
  whole-row credit from state alone were rewritten against the delta rule:
  * `test_single_on_claimant_owns_the_whole_row` — a 200 W step at the lamp's
    activation, sibling steady, reports 200 W at `tier_3_measured`;
  * `test_state_alone_does_not_credit_the_whole_row` — the 881.72 W shape: two
    fixtures, no wattages, one ON, no step → `unknown` for both;
  * `test_stated_standby_is_allowed_while_off` — standby bounds an OFF device but
    does not license the rest of the row;
  * `test_a_measured_step_attributes_more_than_standby` — the step is what makes
    the row separable;
  * `test_a_state_pin_is_never_relaxed_by_the_model_bound_pass` — a measured step
    beats a stale 100 W model bound and the OFF lamp stays at 0.
  New: `test_a_measured_step_attributes_more_than_standby`,
  `test_a_concurrent_sibling_change_blocks_step_attribution`.
* Negative verification (`verify_delta_rule.py`, 11 cases, all against a real
  `DisaggregationEngine.infer`): wrong-direction step; concurrent sibling change;
  deactivation; no transition; no history; step larger than the row (clamped);
  device reports OFF; no declared load entity; stated wattage exceeded (capped at
  the declaration); and the solo-row control. Two of these caught real defects in
  the first implementation — the declared-wattage cap was missing, and the rule
  fired on a solo row where the whole row is the better answer.
* Live defect reproductions (`repro_more_truth.py`, `repro_multi_on.py`,
  `repro_placeholder_gate.py`, `repro_indistinguishable.py`) re-run after the
  change: the 881.72 W fabrication is gone in every variant, and every fixture on
  the affected rows reports `unknown` with the row in the residual.
* **Live before/after** (`repro_live_before_after.py`), same exported live topology
  and the same live channel readings, run once at `HEAD` and once with this change
  — synthetic topology not involved:

  | Case (live `lights_ct2` = 880.16 W) | `HEAD` | With ADR-0026 |
  | --- | --- | --- |
  | two fixtures declared, no wattages, one ON one OFF, no step | **880.16 W to the ON fixture**, `inferred`/`tier_3_measured`, row *not* held | `0.00 W unknown`, row held, 880.16 W in residual |
  | same, but the row stepped **+180 W** at the ON transition | 880.16 W (the whole row; the step made no difference) | **180.00 W**, `inferred`/`tier_3_measured`, surplus in residual |
  | one fixture declared, no wattage, ON | 0.00 W `unknown` | 0.00 W `unknown` (unchanged) |
  | two fixtures, **150 W stated**, one ON one OFF | 150.00 W `configured_prior` | 150.00 W `configured_prior` (unchanged) |

  The two controls matter as much as the fix: the declared-wattage path and the
  safe single-fixture path are byte-identical before and after this change.
* Full suite (`tests/`) and `ruff check engine tools tests` clean; the repository
  gate suite (`tools/dev/verify.py`) passes **12/12**.


**Non-claims.** This does not claim calibrated confidence, a per-fixture estimate
between transitions, or that the discovery heuristic knows which fixtures are on a
circuit. It removes an attribution that was demonstrated to be fabricated, and
replaces it with one that is only made when a measurement supports it.

The sibling-stability check sees **declared** siblings only. An undeclared fixture
that switches on at the same instant as the target inflates the step and is
attributed to the target — measured on the live row, a step equal to the whole
881 W row is credited to the one ON fixture. That is the same answer the user's
rule prescribes (the row moved, nothing *known* else moved), and it is why the
fixture inventory in §8 still matters: the more of the circuit that is declared,
the more of the step's competitors the guard can see. It is not a reason to
withhold the attribution, because the alternative — ignoring a measured step
because the inventory might be incomplete — restores the `unknown`-for-everything
behaviour that made the product useless without one.

---

## ADR-0027 — The App panel is static files under a strict CSP, gated on ingress

**Status:** Accepted · 2026-11-10

**Context.** The App's ingress page was a single inline string in
`addon/gateway.py`: a five-row liveness table (engine/HA/database/contract/uptime)
with an inline `<script>` that polled `/api/v1/health`. It answered "is the engine
up" and nothing else. Everything the engine actually inferred — device values,
their evidence grade, the topology and the residual — was only visible in the
Home Assistant integration's entities, so a user debugging an attribution had no
place to look.

Growing the string was not an option. An inline page cannot be cached or linted
separately from the gateway module, every asset edit re-owns the Python file, and
the CSP had to stay permissive (`script-src 'unsafe-inline'`) to run it at all —
which is exactly the policy that makes markup injection from an interpolated
device name possible.

The App also has no internet access at runtime, so a CDN or build step would fail
silently in the field and could not be reproduced in CI.

**Decision.**

1. **The panel is four static files** in `addon/ui/` — `index.html`, `app.css`,
   `app.js`, `mark.svg` — shipped in the image and by
   `scripts/package_addon.sh`. No build step, no external origin, no bundler.
2. **The gateway serves them, and only them.** `GET /` reads `ui/index.html`;
   `GET /ui/{asset}` serves a fixed allowlist (`app.css`, `app.js`, `mark.svg`),
   so a crafted path cannot escape the directory. Unknown assets `404`; a
   missing `ui/` directory returns `503` with a message rather than a blank page.
3. **Ingress is the only audience.** Both routes require a trusted ingress peer
   (Supervisor IP *and* `X-Remote-User-Id`); a direct client gets the JSON
   descriptor at `/` and `401` for assets. The panel can therefore not be
   enumerated from a host port the user publishes.
4. **The CSP is `script-src 'self'`.** Inline script is gone. `style-src` keeps
   `'unsafe-inline'` only for the `style` attribute the panel sets on meter
   segments and dial geometry; there is no inline `<style>` block. Everything
   else is `default-src 'none'` plus same-origin fonts and images.
5. **The browser never holds the API token.** The panel calls relative
   `/api/v1/...` URLs resolved from `document.baseURI`, so the same files work at
   `/` and under `/api/hassio_ingress/<token>/`. The gateway injects
   `X-API-Token` for ingress peers, exactly as it does for the integration.
6. **Markup injection is not expressible.** The `el()` helper has no `html` key;
   every engine-provided string becomes a `textContent` node. A device name
   containing `<script>` renders as text.
7. **Unknown is shown as unknown.** A missing `power_w` renders `—`, never
   `0 W`; a missing `db.healthy` shows "Unknown", not "Unhealthy". The panel
   never invents a value to fill a card.
8. **The panel is read-only** apart from `POST /topology/confirm`, which records
   the topology the engine already inferred. There is no device-control path;
   presenting one would contradict ADR-0002 and ADR-0024.
9. **Static checks are part of the deployment gate.** `tools/dev/check_panel.py`
   rejects an external origin, an inline script/style block, an `on*` attribute,
   a missing asset and `eval`/`new Function`/`document.write`. It runs in
   `scripts/validate_addon.sh`, so a broken shell fails before the image builds.

**Consequences.** The panel can be developed and reviewed as ordinary
HTML/CSS/JS, and the gateway module returns to being a proxy. The CSP is strict
enough that an injected `<script>` cannot run even if escaping were bypassed, and
the ingress gate means the assets have the same audience as the proxied API.

The trade-off is that the panel is a second place UI work happens, separate from
the integration's config flow. It is deliberately not a replacement for that: the
integration owns entity creation and configuration; the panel reports what the
engine already decided.

**Verification.**

* `tests/unit/test_panel.py` — the shell is returned for an ingress peer; the CSP
  and `Cache-Control: no-store` are present; a direct peer gets JSON at `/` and
  `401` for assets; an ingress peer without `X-Remote-User-Id` is refused; each
  asset has its expected content type; an unknown asset `404`s; a traversal
  attempt cannot read outside `UI_ASSETS`; a missing `ui/` directory returns
  `503`; and the static checker rejects each unsafe construct it exists to catch.
* `tools/dev/check_panel.py` — negative-verified in `tests/unit/test_panel.py`,
  which copies the shipped panel into a temp root and then adds an external
  stylesheet, an inline `<script>`, an inline `<style>`, an `onload=`, an
  `eval()`/`document.write()`, and deletes `app.js`; each mutation fails the
  check. The unmodified copy passes.
* `scripts/validate_addon.sh --no-build` — stages the panel and asserts all four
  files reach the packaging directory.
* Visual review: 21 screenshots (7 views × 390/900/1440 px) against a stub engine,
  with no console errors and no horizontal overflow.

---

## ADR-0028 — A device link is resolved by the integration, never by the engine

**Status:** Accepted · 2026-11-11

**Context.** `CONF_DEVICE_LINKS` stores `{engine_device_id: ha_device_id}`. It was
written for *placement*: the power/energy entities of an inferred load are shown
on the appliance's own Home Assistant device. Learning evidence was later read
from the same map, and the two were conflated. An HA **device** id is not an
entity id, so nothing could expand it:

* the engine saw `metadata["state_entity_ids"]` empty,
* `entity_links()` linked only the device's CT *power* channels,
* every real transition hit the `power_events_not_state` bridge and returned
  before the transition buffer.

A live install showed the exact signature for days: `samples_seen: 0`,
`samples_accepted: 0`, `samples_rejected: 0`, `dominant_rejection_count: 0`,
`effective_state: blocked`, `dominant_rejection_reason: ambiguous_topology`. A
candidate topology rejects every *offered* sample, so `rejected: 0` proves the
offer stage was empty rather than the filter being selective.

**Decision.**

1. **The integration resolves, the engine reads.** Only Home Assistant's entity
   registry can expand a device id, and `docs/architecture.md` §1 requires the
   engine to run outside Home Assistant. Resolution therefore happens in
   `custom_components/wattmosaic/state_links.py` (pure: the registry is passed in
   as plain rows) and only the resulting **entity ids** cross the boundary. The
   engine still never learns what a device registry is.
2. **A separate metadata key.** Resolved ids are written to
   `metadata["link_state_entity_ids"]`. `state_entity_ids` stays evidence-owned
   (ADR-0011) and `config_load_entity_ids` stays configuration-owned, so
   unlinking restores both exactly. `runtime_state_entities()` is the union of
   the three, and `entity_links()`/`ambiguous_entities()` read that union.
3. **Measurements are never state.** Domains in `STATE_DOMAINS` may be state
   sources; power/energy/current/voltage domains, device classes and units may
   not, and the device's own CT entity ids are excluded on the topology's
   evidence even when the registry row carries no attributes.
4. **Unresolvable is reported, not guessed.** No name matching, no area
   heuristic, no fabricated binding. Each refusal becomes a record in
   `devices_without_state_entity` with a reason code
   (`only_measurement_entities`, `device_not_in_registry`,
   `not_a_state_domain`, `no_state_entities`, `engine_device_not_in_topology`),
   surfaced by diagnostics. A topology with no device is never written to: there
   is nothing to carry the ids and creating one would fabricate topology.
5. **The write is a replay, not a new path.** The resolved ids travel through the
   existing `async_replay_topology` / `plan_topology_push` /
   `build_topology_update` writer, with `state_links_missing` as its own plan
   reason. A link added *after* setup changes no engine revision, so the replay
   also fingerprints the declared links (`TopologyPushState.checked_links`) to
   re-open exactly that check.
6. **The transition rule is untouched.** `previous is not None and previous !=
   new_state` is not modified: once a real state entity is declared, the existing
   rule is what produces the transition. Adding a second state source does not
   loosen it.

**Consequences.** An install that declared a device link now produces learning
samples (rejected as `ambiguous_topology` until the topology is confirmed, which
is the honest state). An install whose link points at a measurement-only device
produces none, and diagnostics says why. The contract, storage schema and
`CONTRACT_VERSION` are unchanged: `metadata` already round-trips, so this is a
producer of an existing field, not a new field.

**Verification.**

* `tests/integration/test_learning_intake.py` — a device id resolves to the
  device's state entities and never to its CT/power/energy entities; the
  resolved ids reach the topology metadata; the real `WattMosaicBackend` reports
  `samples_seen > 0` **and** `samples_rejected > 0` with
  `dominant_rejection_reason == ambiguous_topology` after the documented
  maturity window (~68 s, simulated, no sleep); a power-only change still
  produces no sample; a link-only install is replayed; an in-sync link is not
  rewritten; a link added after setup re-opens the check; an unresolvable link
  is reported rather than written.
* Fail-before proof: with `engine/wattmosaic_engine/device_links.py` at `HEAD`
  the intake tests reproduce the live signature verbatim (`samples_seen: 0`,
  `samples_rejected: 0`, `dominant_rejection_count: 0`).

---

## ADR-0029 — The manual `dedicated_device` declaration is deprecated; the measurement is not

**Status:** Accepted · 2026-11-13 · Deprecates the *declaration* only; extends ADR-0025/ADR-0026

**Context.** The engine's only path to `Source.DIRECT_MEASURED` is a user
declaring a channel `CTType.DEDICATED_DEVICE` (or listing it in
`DeviceTopology.dedicated_ct_ids`). Two findings make *the requirement* the
problem while leaving *the capability* sound.

*The live install depends on it.* The instrumented installation reports
**~1032 W** of `direct_measured` power through `resolve_direct()` →
`dedicated_channel_for()`. Removing the capability would silently reclassify
those watts as `inferred` and drop confidence from **0.97 to 0.88 or lower** —
a measurement downgraded to a guess, with nothing in the UI saying so.

*The declaration is stored three times with no precedence.* The same fact lives
in `CTChannel.device_id` (on a `dedicated_device` row),
`DeviceTopology.dedicated_ct_ids`, and
`DeviceConfiguration.monitored_by_ct_ids`. Three copies cannot disagree and
cannot be validated together: which one is authoritative is decided by whichever
code path reads first, and a partial edit leaves the others stale.

*Discovery no longer produces it.* ADR-0025 moved the single-channel fallback to
`branch`, and ADR-0026's evidence bar stands on measured circuit *changes*, not a
private-row assertion. After ADR-0026 a `dedicated_device` row comes only from a
declaration — that is, from manual user input and nowhere else.

**Decision.**

1. **Deprecate the declaration, never the capability.**
   `CTType.DEDICATED_DEVICE`, `DeviceTopology.dedicated_ct_ids`,
   `Source.DIRECT_MEASURED`, `resolve_direct()` and
   `dedicated_channel_for()` all keep working exactly as they do today. The role
   stays **selectable** in the wizard. This ADR adds notices and documentation;
   it removes no code path, no contract field and no option.
2. **One notice per CT, not one per storage location.** The new
   `engine/wattmosaic_engine/deprecations.py` scans all three locations and
   deduplicates by `ct_id`. That dedup *is* the finding: the user wrote one fact,
   not three. The registry (`DEPRECATED_DECLARATIONS`) carries the code
   `dedicated_declaration` and `since` version `1.7.0`.
3. **The replacement is documented, not applied.** The advised path is to declare
   the CT as `branch` and let circuit inference classify it; a circuit reaching
   `CircuitClass.DEDICATED` at `CONFIRMED` is the evidenced equivalent of the old
   declaration. There is **no auto-conversion** of existing declarations:
   conversion drops the direct path today (a branch CT is deliberately *not* a
   direct measurement — `docs/electrical_model.md` §9.1), so it must stay opt-in
   until the inferred path is wired as its replacement.
4. **Diagnostics only, byte-identical output.** The new setting
   `WM_DEPRECATE_DEDICATED_DECLARATION` (default `true`) gates the notices and
   the `health["deprecations"]` list. `false` silences them; it must not — and
   does not — change a single output value.
5. **Emit once, never per sample.** `warn_once` logs one `WARNING` per `ct_id`
   for the process lifetime (module-level, resettable for tests). It is called
   from topology installation (`set_topology`, `confirm_topology`, startup), never
   from the inference or per-sample loop, so a 5 s inference cycle cannot turn a
   deprecation into log spam.
6. **The payload is additive.** `health["deprecations"]` is
   `[{"code", "ct_id", "device_id", "since", "replacement", "message"}]`, present
   and empty when nothing is declared. `CONTRACT_VERSION` is unchanged — an
   additive diagnostics key is not a contract change, and a pre-change topology
   (no `deprecations` key anywhere) still loads.

**Consequences.** A declaration that used to look supported now announces its own
replacement while continuing to produce the identical measured value. An
operator who does not want the notice sets one flag and loses nothing else. The
direct path stays testable and stays in use, so the ~1032 W reading is preserved
until the inferred equivalent is proven on live data.

**Deprecation window.** Notices begin at **1.7.0**. Removal of the *declaration*
(never the capability) requires a later ADR that demonstrates on live evidence
that a `branch` CT reaching `CircuitClass.DEDICATED` at `CONFIRMED` reproduces
the direct reading. Until then the role remains selectable and works.

**Verification.**

* `tests/unit/test_deprecations.py` — the same declaration written in all three
  storage locations yields exactly **one** entry; a branch declaration yields
  none.
* `test_inference_output_is_identical_with_the_flag_on_and_off` — a topology with
  a dedicated CT is inferred twice and the per-device
  `(power_w, source, confidence, tier)` tuples must be **byte-identical**; the
  dedicated device is still `direct_measured` at 75 W in both runs.
* `test_resolve_direct_still_measures_a_dedicated_ct` — the capability regression
  guard: `resolve_direct` returns the same usable 75 W reading, same `ct_ids`,
  same quality.
* `warn_once` tests — one `WARNING` across repeated collection (250 iterations of
  a per-sample shape emit nothing further), and `reset_warnings` re-arms it.
* Diagnostics tests — the exact six keys, an empty list for a branch-only
  topology, an empty list when the setting is off, and a legacy topology payload
  without the key loading and reporting normally.

**Non-claims.** This does not claim the inferred path is equivalent yet, does not
remove the direct path, and does not migrate anything. It makes the deprecation
visible in the log, in `/api/v1/diagnostics` and in the wizard help text.
