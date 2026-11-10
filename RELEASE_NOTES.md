# Release Notes

## v0.2.1-dev.10 — 2026-11-10 (development channel)

Automatic development build.

### Changes

- Merge pull request #1 from asxzy/feature/app-ui
- Give the App a real panel, and stop the engine from misreporting its version

## v0.2.1-dev.9 — 2026-11-10 (development channel)

Automatic development build.

### Changes

- Delete the app and its data outright; keep the entry and every statistic

## v0.2.1-dev.8 — 2026-11-10 (development channel)

Automatic development build.

### Changes

- Reset the engine database without touching the install

## v0.2.1-dev.7 — 2026-11-10 (development channel)

Automatic development build.

### Changes

- Clear the 26 provably redundant stranded statistics

## v0.2.1-dev.6 — 2026-11-10 (development channel)

Automatic development build.

### Changes

- Investigate the stranded statistics instead of guessing at them
- Record verification gate results for ADR-0026
- Treat every CT as a circuit and attribute the measured step (ADR-0026)

## v0.2.1-dev.5 — 2026-11-10 (development channel)

Automatic development build.

### Changes

- Correct the glued-counter claim in the v0.2.1-dev.1 notes

## v0.2.1-dev.4 — 2026-11-10 (development channel)

Automatic development build.

### Changes

- Record the prune fail-open the first real release run exposed

## v0.2.1-dev.3 — 2026-11-10 (development channel)

Automatic development build.

### Changes

- Make the prune job fail closed, and give it a repository to talk to

## v0.2.1-dev.2 — 2026-11-10 (development channel)

Automatic development build.

### Changes

- Regenerate verification data after the merge
- Join main's history so the v0.1.6 record is not orphaned
- Carry the v0.1.6 release notes onto the development line
- Record the branch rework and the two premises it falsified
- Make main the development line and release every push from one workflow
- Stop using the deprecated via_device pair, which took 276 sensors offline
- Close the stale-image guard's fail-open, and record the release work
- Make the stale-image guard fail closed, and prove it against the registry
- Record both channels naming a version, and the harness/CI gap
- Lint the release tests, and make the harness run the linter
- Lint the release tests, and make the harness run the linter
- Release 0.1.6: automate the stable channel's tags and releases

## v0.2.1-dev.1 — 2026-11-09 (development channel)

Home Assistant was showing this integration's version as a **commit hash**
(`eec273d`), not `0.2.0-dev9`. The code was correct: every version field said
`0.2.0-dev9` and the matching image had been published for each of the fifteen
releases. What was missing was **metadata**. Home Assistant, HACS and the
Marketplace do not read a version out of the tree — they read the repository's
git tags and GitHub Releases, and this repository had never created a single one
of either. With nothing to resolve, they fell back to the branch head and
displayed its commit SHA. **This is a prerelease. It is installed from the `dev`
branch and does not replace the stable integration or App.**

### Added

- **A release pipeline.** `.github/workflows/release.yaml` runs on `main` and
  `dev` and, when the version changes, creates an annotated `v<version>` tag and
  a GitHub Release whose body is taken from this file. The tag is derived from
  `manifest.json`, so the version a user is offered and the version in the tree
  cannot disagree. Stable versions (`X.Y.Z`) publish as "latest"; `-devN`
  versions publish with `--prerelease --latest=false`, so a development build can
  never be offered to someone tracking stable.
- **A release-contract gate,** `tools/dev/check_release.py`, in CI and in
  `tools/dev/verify.py`. It checks that `manifest.json`, `const.py`,
  `addon/config.yaml`, `pyproject.toml` and the engine's `__version__` all state
  the same version; that `RELEASE_NOTES.md` documents it; that a candidate tag
  matches the tree; and that `main` carries only stable versions while `dev`
  carries only prereleases.

### Fixed

- **The dev channel stopped incrementing at `dev10`, and nothing said so.**
  SemVer 2.0.0 compares a prerelease identifier that contains letters
  **lexically**, so `0.2.0-dev10` sorts *below* `0.2.0-dev9`. An updater that
  orders releases by version therefore keeps offering `dev9` as the newest
  release and never installs `dev10` — even though the tag, the Release and the
  image all exist. This was found by building it and watching Home Assistant
  resolve `latest_version: v0.2.0-dev9` twice, including after a probe release
  created with the newest timestamp was ignored. That probe is the evidence:
  ordering is by SemVer, not by date, so the numbering had to change.
  The counter is now its own numeric identifier (`-dev.1`, `-dev.2`, …), which
  compares numerically and keeps incrementing. `check_release.py` rejects the
  glued form (`-dev10`) so this cannot be reintroduced.

  > **Correction (2026-11-10).** The *symptom* above was real — the update entity
  > did keep resolving `dev9` — but the mechanism given here is **wrong**. The
  > Marketplace compares versions with **AwesomeVersion**
  > (`marketplace/utils/version.py::is_newer_version`), not raw SemVer, and it
  > orders `v0.2.0-dev10 > v0.2.0-dev9` correctly. The dot form is kept because
  > it is unambiguous and already published, not because the glued form is
  > broken here. Some other cause produced the stuck `dev9`; it has not been
  > identified, and this note should not be cited for the ordering claim.
- **The version fields had silently drifted, and nothing noticed.** The gate
  found two on its first run: `pyproject.toml` was still `0.2.0-dev4` (five
  releases behind) and `engine/wattmosaic_engine/__init__.py` was `0.1.3` (three
  releases behind). Both are now checked, so neither can drift again.
- **A documentation-only push turned the App-image run red.** That workflow runs
  on every push because the repository history is replaced rather than appended,
  so a path filter can never be trusted — but its stale-tag check compared the
  published commit SHA against `HEAD`. A commit that touched only
  `PROJECT_STATUS.md` therefore failed while building a byte-identical image.
  The check now compares the paths that actually enter the image, so a docs-only
  push reports "nothing to rebuild" instead of an error, and a real engine change
  without a version bump still fails.

### Backfill

Tags and Releases are created for the history that is still reachable and
publishable. `0.1.0` and `0.1.1`/`0.1.2` are **not** tagged: the `0.1.0` commits
are reachable only from a local branch that holds real household data, and
`0.1.1`/`0.1.2` were never committed to this repository's history. Tagging them
would publish either a household inventory or a tag with no code behind it.

## v0.2.0-dev9 — 2026-11-09 (development channel)

One defect, found while performing the operation dev8's release notes said could
not be performed: the wizard could demote a `dedicated_device` CT to `branch`,
and doing so silently cut the device off from every measurement. **This is a
prerelease. It is installed from the `dev` branch and does not replace the stable
integration or App.**

### Fixed

- **Demoting a CT to `branch` orphaned its device.** The channel wizard mirrors
  `dedicated_device` assignments onto the device's `dedicated_ct_ids`. Changing
  the role to `branch` removed the CT from that list — and never added it to
  `branch_ct_ids`, which is where a shared row lives. The result looked correct
  in the UI: the channel said `branch`, the device still existed, the save
  succeeded. Behind it the graph resolved **zero** attribution rows for that
  device (`('ct1',)` → `()`), so the fixture would have reported nothing forever
  while the wizard reported success. This is the shape the reclassification
  itself needs, so it blocked the operation rather than merely complicating it.
  A demoted CT now moves to `branch_ct_ids` and keeps measuring the circuit.
  Turning the CT into a `main` feed or reassigning it to a different device still
  removes it from this device's rows, which is correct: a feed is a containment
  constraint, not a measurement.

### Notes

- The code comment at that site already described the intended behaviour
  ("the device silently keeps zero attribution"); only the `dedicated_ct_ids`
  half was ever implemented.
- Negative verification: restoring the old block makes the new integration test
  fail (`[] == ['ct_lights']`).

## v0.2.0-dev8 — 2026-11-09 (development channel)

Discovery was calling every single-channel CT a **private measurement**. It had
no evidence for that — the only test was "this channel is not a `ct1`/`ct2`
pair", which every one-CT lighting circuit passes — and the claim was the sole
reason 15 correlated fixtures could not be placed. Reclassifying exposed a real
fabrication behind it, which this release also fixes. **This is a prerelease. It
is installed from the `dev` branch and does not replace the stable integration or
App.**

### Fixed

- **A single CT is a circuit, not an appliance.** `discovery.py` emitted
  `CTType.DEDICATED_DEVICE` for every single-channel load and created a device
  that claimed the row as its own measurement. On the reference installation the
  three lighting rows declared this way each resolved 7, 3 and 2 independent
  fixtures, and one peaked at 221 % of what its claimants summed to. The
  fallback now emits `branch`: the circuit may feed zero or more devices, which
  is what a single conductor actually establishes. The device is still created
  so the circuit is visible and the topology validates, but it owns the row as a
  shared circuit (`branch_ct_ids`, `shared_circuit: True`) and no channel
  carries a `device_id`. Only a declaration produces `dedicated_device`. This
  alone takes auto-placement from **0 accepted / 15 refused** to **15 accepted /
  0 refused**.
- **A whole shared row could be credited to one fixture.** With the corrected
  topology, one lighting row peaked at 1 036 W while its three fixtures declared
  243.6 / 112.3 / 112.7 W. One fixture ON, its siblings positively OFF, and the
  solver handed that lamp the entire row — 4.25× its declared draw, graded
  `tier_3_measured`. The same shape appeared on other circuits at 402 %, 250 %
  and 217 % of their claimants' declared draw. This was pre-existing; the
  reclassification is what made it reach a named fixture on a branch row. A
  declared wattage is now a **non-relaxable upper bound** on a shared row:
  applied to every configured claimant that is not alone on its row and not
  state-pinned, and never widened by the model-relaxation pass. When it binds
  and leaves power unexplained the value is reported as `configured_prior` /
  `tier_1_modelled` with `declared_wattage_bounds_row_attribution` — the surplus
  is undeclared load, not that lamp's draw. Reaching the ceiling with the row
  fully explained is still a real measurement.
- **An ambiguous match could reach the wrong engine and report success.** See
  v0.2.0-dev7 for the routing fix; nothing here changes that path.

### Notes

- `config_ceiling_w` existed since 0.1.5 and was never read. It is now the
  mechanism above, which is why the safeguard needed no new contract field.
- Adding a slack to every branch row was measured and **rejected**: three 156 W
  lamps on a 468 W row collapsed to 117 W each and the solver reported an
  infeasible problem. The row is genuinely separable while the reading is within
  what the claimants could draw.
- The stored configuration of a *confirmed* installation is not rewritten by
  this release. A topology that already declares a single-channel row
  `dedicated_device` keeps that declaration until it is changed deliberately.

## v0.2.0-dev7 — 2026-11-09 (development channel)

Three defects found by replaying the full 11-day history against every declarable
entity. One was a double-count in the correlation itself; the other two were
failures to *report* a problem that already existed. **This is a prerelease. It
is installed from the `dev` branch and does not replace the stable integration or
App.**

### Fixed

- **One step could be paid twice.** Two fixtures on one circuit driven by one
  scene each see the row rise by the sum of both, so each independently explains
  the whole step and each is credited the whole step. No single candidate looks
  wrong; the row is simply counted twice once they are summed. On the reference
  install two scene-driven fixtures on one lighting row toggled 12/12 together
  (median 0.55 s) yet each was credited ~86 W from a row averaging 26.5 W.
  Fixtures that share a row but switch independently overlap only a few percent,
  so timing separates the two cases: two candidates on one row are now withheld
  when at least 80 % of one side's transitions have a partner within 5 s.
  **Both** are withheld — choosing which fixture owns the step would be a guess.
  Over the 11-day replay this withholds exactly the two scene-driven pairs and
  leaves the independent ones untouched, taking accepted candidates from 19 to
  15. They remain reportable as `unresolved` so the wiring can be declared by
  hand.
- **A service call could silently reach the wrong engine.** With a stable and a
  development App both configured, `_resolve_entry` fell back to the first entry
  whenever the target did not resolve. Both Apps publish the same engine device
  ids (28 of 30 collide), so a bare id matched both and the stable engine won:
  probes returned contract `1.1.0` for a bare id and `1.2.0` only for the fully
  qualified identifier. `target.device` from the UI was ignored outright, because
  it is an HA registry id and never an identifier value. Routing now refuses
  rather than guessing — `engine_ambiguous`, `ambiguous_target` or
  `unknown_target` — and a device picked in the UI is translated back to the
  engine id the entry published, so it reaches the engine that owns it.
- **A load row declared `normal` that reads negative was never flagged.** Two
  branch rows report 245 negative readings and zero positive (about −1190 W)
  while declared `polarity: normal`. The quality engine only checked the inverse
  case, so these passed silently; downstream the direct path clips them to 0 W
  and the circuit vanishes from every total while still claiming good quality.
  The check is now symmetric for `branch` and `dedicated_device` rows. A `main`
  row is excluded deliberately: negative power there is real export, and flagging
  it would mark every exporting installation defective. The rows are currently
  inert for reporting, so no published number was wrong — but the defect was one
  declaration away from being invisible loss.

### Notes

- The reversed reading is the hardware's own classification: the Refoss
  accumulators report `this_month_return_energy` 0.956/0.961 kWh against
  `this_month_energy` 0.0, i.e. the meter itself calls that flow return.
  Correcting the declaration is a user decision and is not done here.
- `1344` unit and integration tests pass. Each fix is negatively verified:
  disabling the co-toggle filter accepts both gym fixtures at 80 W each;
  restoring the first-entry fallback fails 6 routing tests; removing the polarity
  check hides the real −1210 W reading, and extending it to mains flags
  legitimate export.

### Follow-up, same day

- **A HomeKit device identifier turned every routing error into an opaque 500.**
  The routing code unpacked each device-registry identifier as a
  `(domain, value)` pair. Home Assistant does not enforce that length — the
  HomeKit controller stores three-element tuples — and this installation has two
  of them, so every `device_id` lookup raised `ValueError: too many values to
  unpack`. A correct `ambiguous_target` refusal surfaced as
  `500 Internal Server Error` with an empty body. Identifiers are now normalised
  to their first two elements with malformed entries skipped, so one bad device
  cannot break a lookup over the whole registry. Found by probing the deployed
  system; the suite cannot import Home Assistant and never sees real registry
  contents. `1355` tests pass.

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
