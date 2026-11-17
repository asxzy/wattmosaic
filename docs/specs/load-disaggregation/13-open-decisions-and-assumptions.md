# 13 — Open Decisions and Assumptions

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION** — Register of everything the load-disaggregation specification set did **not** decide, plus the V1 defaults the implementation proceeds on. Two product decisions from `06-device-and-load-model-spec.md` have since been answered by the project owner and are recorded as `RESOLVED` (§2.4); a third owner decision — the Device↔Reader mapping module **is** built in V1 — closes `ASM-06` (§4); and the orchestrator's approval of the specification set closes `OD-38` (§3).

## 1. How to read this register

| Field | What it means |
| --- | --- |
| **ID** | `OD-<nn>` — an open decision (a question a named owner must answer). `ASM-<nn>` — an assumption (the project proceeds on a stated value or rule). |
| **Description** | Exactly what is undecided or assumed. One or two sentences. |
| **Impact** | What it costs if the assumption is wrong — the requirement IDs and the behaviour that would change. |
| **Current assumption** | The concrete V1 value or rule implementation proceeds on. |
| **Validation needed** | The check, test, measurement or user action that would settle it, and the vehicle that runs it. |
| **Implementation may proceed?** | `YES`, `NO`, or `YES-WITH-CONSTRAINT` — with the constraint named. |
| **Owner** | `orchestrator`, `user`, or the named subsystem that closes it. |
| **Status** | `OPEN`, `ASSUMED`, `DEFERRED`, `RESOLVED` — a `RESOLVED` item cites the evidence that closed it. |
| **Source** | The sibling spec file and section, or the `file:line` it came from. |

**Rule.** Every item below carries all nine fields; an item without a source is not a register entry.
**Rule.** No item in this register blocks the whole project. Uncertain parameters are made configurable; the assumption is stated and the escape hatch is named (§7).
**Rule.** An item is never phrased as a vague concern. If it cannot be actioned by someone who did not write the specification, it does not belong here.

**ID bands.**

| Band | Owner file | Note |
| --- | --- | --- |
| `OD-01`–`OD-08` | `00-project-overview.md` §12 | Inherited, not renumbered. |
| `OD-03.1`–`OD-03.4` | `03-measurement-and-electrical-domain.md` §16 | Inherited, not renumbered. |
| `OD-09` | `01-requirements.md` §4 | **Retained**, deliberately unchanged — a distinct decision about the T-catalogue conflict; see §8.2. |
| `OD-03.5`–`OD-03.9` | `05-residual-model-spec.md` §15 | Renumbered to `OD-40`–`OD-44` (§5.2). |
| `OD-03.5`–`OD-03.8`, `OD-03.10`–`OD-03.11` | `06-device-and-load-model-spec.md` §14 | `OD-03.9` and `OD-03.12` are **RESOLVED** by the owner (§2.4) and keep their IDs; the other six renumber to `OD-45`–`OD-50` (§5.2). |
| `OD-21`–`OD-30` | `04-topology-spec.md` | Renumbered to `OD-51`–`OD-60` (§5.2). |
| `OD-21`–`OD-26` | `07-event-and-inference-spec.md` §22 | Renumbered to `OD-61`–`OD-66` (§5.2). |
| `OD-21`–`OD-27` | `08-storage-and-data-lifecycle.md` §18 | Renumbered to `OD-67`–`OD-73` (§5.2). |
| `OD-09`–`OD-15` | `09-api-and-data-contracts.md` §14.1 | Renumbered to `OD-74`–`OD-80` (§5.2). |
| `OD-09`–`OD-15` | `10-ui-and-user-experience-spec.md` §15 | Renumbered to `OD-81`–`OD-87` (§5.2). |
| `OD-21`–`OD-31` | `11-test-and-validation-plan.md` §15 | Renumbered to `OD-88`–`OD-98` (§5.2). |
| `OD-09`–`OD-13` | `12-deployment-and-rollback.md` §13.2 | Renumbered to `OD-99`–`OD-103` (§5.2). |
| `OD-30`–`OD-38`, `ASM-01`–`ASM-07` | this register | Items raised here. |

**Rule.** No ID is used twice. The `OD-21` band was claimed independently by four sibling files; the resolution is renumbering, not deletion (§5.2).
**Rule.** The old IDs are valid **only** inside the sibling files until the orchestrator's correction pass rewrites them.

**Tally.** 95 items: 12 inherited from `00-`/`03-` (`OD-01`–`OD-08` and `OD-03.1`–`OD-03.4`), 1 retained from `01-` (`OD-09`), 2 owner-resolved (`OD-03.9`, `OD-03.12`), 9 open decisions raised here (`OD-30`–`OD-38`) — one of them, `OD-38`, is now **RESOLVED** by the orchestrator's approval of the specification set (§3) — 7 assumptions recorded here (`ASM-01`–`ASM-07`) — one of them, `ASM-06`, is now **RESOLVED** by the owner (§4) — and 64 sibling items harvested from the nine landed files (§5.1). Every sibling in the `04`–`12` range has landed; `14-…` and `15-…` landed and raise no new decisions. The complete one-line index is §8.

---

## 2. Inherited items

### 2.1 From `00-project-overview.md` — `OD-01` … `OD-08`

*Consolidated from `00-project-overview.md:431-438`. The full entries are reproduced below with the same field set.*

<!-- INHERITED-00 -->

### 2.2 From `03-measurement-and-electrical-domain.md` — `OD-03.1` … `OD-03.4`

*Consolidated from `03-measurement-and-electrical-domain.md:757-760`.*

<!-- INHERITED-03 -->

### 2.3 Cross-references confirmed from `02-architecture.md`

`02-architecture.md` references the decision register at `:182-186`, `:321`, `:507` and `:562-571`. The cross-reference at `:562-571` is the one that fixes `OD-08`: the topology UI is an **extension of the `#/topology` route**, not a new route. The remaining cross-references resolve to items already listed above and introduce no new decision.

### 2.4 From `06-device-and-load-model-spec.md` — owner decisions `OD-03.9` and `OD-03.12` (RESOLVED)

*Two product decisions raised by `06-device-and-load-model-spec.md` were put to the project owner and answered. The owner's answer is the decision; it is recorded here with its consequences and is not re-litigated. The remaining `06-…` decisions are renumbered and listed in §5.*

### OD-03.9 — May the system name an appliance on its own? — **RESOLVED: "Propose candidate names."**

| Field | Value |
| --- | --- |
| **ID** | OD-03.9 |
| **Description** | Whether the system may propose an identity for an unknown load from its measured power signature — a refrigerator cycling, a washing machine's stage profile. |
| **Impact** | Requirement: `REQ-POWER-005`, `REQ-RESIDUAL-006`, `REQ-DISAGG-005`, `REQ-UI-011`, `REQ-MAPPING-004`. The decision adds a **user-facing confirmation surface**, so `REQ-UI-011`'s acceptance criteria must cover the proposal flow. The unknown-load lifecycle changes from "no identity without the user" to "no **device** without the user, a candidate may be proposed". **Sections of `06-device-and-load-model-spec.md` that assumed the opposite have been reconciled and now carry the owner's answer:** the conflation table row at `06-…:43` ("Unknown profile = device | The system names a device it has not identified") now distinguishes a *proposal* from a *device*; the rule at `06-…:110` ("An unknown load profile is never presented as an identified device") now distinguishes "never presented as identified" from "may be presented as a proposal" and states the proposal's hard rules; the discovery rule at `06-…:483` and the lifecycle row at `06-…:491` now say that the proposal may carry a candidate *name*; and the `OD-03.9` row itself at `06-…:649` now states the owner's answer ("Propose candidate names.") with its surviving hard rules and is marked **RESOLVED**. **History:** before that reconciliation, all five of those locations held the pre-decision position — the conflation row and the `06-…:110` rule stated the narrower "never named / never presented as identified" claim without distinguishing a proposal, the discovery rule and the lifecycle row stated that a discovery is a proposal but did not say the proposal may carry a candidate *name*, and the `06-…:649` row recorded only the auto-promotion half of the question ("Never automatically."). This register records the owner's answer; the reconciliation it required is now made in `06-…`. |
| **Current assumption** | The system **may propose** a candidate name or appliance type from the load's power signature; a proposal is **never auto-promoted** to a device. The hard rules that survive regardless: (1) a proposal is a **candidate** — `requires_confirmation` is always `true` (`contracts.py:791`) and its confidence is bounded by the absolute ceiling `MAX_CONFIDENCE = 0.99` (`disaggregation/association.py:144`); V1 additionally applies a stricter, configurable `candidate_confidence_cap = 0.90` (§6) so a proposal can never present itself as an established association; (2) a proposal is **never presented as a measured or identified fact**; (3) a proposal **never becomes a device** without user confirmation; (4) the confirmed identity **and** the proposal are both recorded, so the proposal's history is not lost when the user confirms a different identity. |
| **Validation needed** | A simulator scenario in which an unknown load's signature is stable across repeats (the `unknown_load` stratum, `tests/fixtures/labhouse.py:32-54`), asserting that a candidate is proposed, that it is not presented as identified, and that confirming it produces a device while declining it leaves the profile intact. UI check: the proposal renders as a proposal with a confirm/decline action. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — implementation may proceed on the assumption that candidates are proposed and confirmed by a user, but the **confirmation path must exist in the same wave**. A proposal the user cannot accept or decline is worse than no proposal, because it invites an unconfirmed identity to be read as a fact. |
| **Owner** | `user` (decided); the confirmation path's implementation is the subsystem owner's |
| **Status** | RESOLVED — closed by the project owner's answer ("Propose candidate names."), recorded in this item. |
| **Source** | `06-device-and-load-model-spec.md` (`OD-03.9`) |

### OD-03.12 — How much may be inferred for a device with no Home Assistant integration? — **RESOLVED: "Allow state model learning."**

| Field | Value |
| --- | --- |
| **ID** | OD-03.12 |
| **Description** | Whether a device with no Home Assistant integration — observed by a reader but with no state to confirm when it is on — may learn a state-conditioned power model. |
| **Impact** | Requirement: `REQ-POWER-005`, `REQ-POWER-002`, `REQ-POWER-003`, `REQ-LEARNING-001`, `REQ-LEARNING-003`, `REQ-LEARNING-007`, `REQ-CONFIDENCE-003` (a device learned without a state entity must show reduced measurement-association confidence), `REQ-MAPPING-003`. **Sections of `06-device-and-load-model-spec.md` that assumed the opposite have been reconciled and now carry the owner's answer:** the rule at `06-…:177` ("A device with no HA entity has no control state. It therefore cannot be a state-conditioned model in the ordinary sense") is narrowed rather than reversed — the model is fitted on the reader's step structure, not on a confirmed state transition, and the row now records that a state-model-shaped fit over the reader's own steps is permitted; the eligibility rule at `06-…:185` ("A device with no HA integration is eligible for learning. What it is *not* eligible for is a state-conditioned attribution") now permits a *state-model-shaped* fit over the reader's own steps and carries the surviving hard rules (estimate never a measurement; `ESTIMATED`/`ALLOCATED` labelling; weaker evidence count; never promoted over a direct measurement; absence of a state entity is reduced observability, not zero power); the labelling table at `06-…:191-198` (§3.3) stays as written and is the constraint the owner's answer is bounded by; and the `OD-03.12` row itself at `06-…:652` now states the owner's answer ("Allow state model learning.") with its surviving hard rules and is marked **RESOLVED**. **History:** before that reconciliation, all four of those locations held the pre-decision position — the `06-…:177` rule stated the narrower "cannot be a state-conditioned model" claim without the owner's permission, the `06-…:185` eligibility rule admitted learning but excluded a state-model-shaped fit, and the `06-…:652` row recorded only the conservative half. This register records the owner's answer; the reconciliation it required is now made in `06-…`. |
| **Current assumption** | State model learning **is allowed** for a reader-observed device with no HA entity. The rules that survive regardless: (1) the learned model is an **estimate, never a measurement**; (2) the device's reported power is labelled `ESTIMATED` or `ALLOCATED`, never `MEASURED` — the four-state rule (`REQ-FOUNDATION-005`); (3) a model learned without a state entity is fitted on the reader's own step structure and carries a **weaker evidence count** than one fitted against a confirmed state; (4) such a model can **never be promoted over a direct measurement** (`REQ-LEARNING-007`); (5) the absence of a state entity is recorded as **reduced observability in the mapping cell**, not as zero power. |
| **Validation needed** | A simulator scenario for a reader-observed device with no HA entity (the `dedicated` stratum can be adapted), asserting that a model is fitted from the reader's steps alone, that the reported value carries the `ESTIMATED` label, that a direct measurement elsewhere still wins, and that the confidence vector's measurement-association dimension is lower than for a state-confirmed device. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — the learning path may proceed, but the **labelling constraint is not optional**: an endpoint-less device reported as `MEASURED` would be a direct violation of `REQ-FOUNDATION-005` and of the repository's "never present an estimate as a measurement" rule. |
| **Owner** | `user` (decided); the learning-pipeline implementation is the subsystem owner's |
| **Status** | RESOLVED — closed by the project owner's answer ("Allow state model learning."), recorded in this item. |
| **Source** | `06-device-and-load-model-spec.md` (`OD-03.12`) |

---

## 3. Open decisions raised by this register

### OD-30 — Effective inference resolution against the CT update cadence

| Field | Value |
| --- | --- |
| **ID** | OD-30 |
| **Description** | The audited median CT state-update interval is 15.7 s, the inference engine ticks at 5 s, and the retention cascade aggregates at 5 minutes. The T05 (noise) and T14 (missing/late data) strata therefore assume a resolution the hardware does not deliver. |
| **Impact** | Requirement: [REQ:cadence]. The staleness/freshness surface changes: any UI that implies a 5 s live reading misreports the age of the underlying sample. The T05 and T14 acceptance figures are not reproducible at a resolution the meter never produced. |
| **Current assumption** | An inference is *computed* every 5 s, but its **effective resolution is bounded by the ~15.7 s CT update interval**. The UI shows the age of the newest underlying CT sample, never the engine tick. The engine tick is an implementation detail and is not published as a measurement rate. |
| **Validation needed** | Re-measure the CT update interval against a live meter and record the distribution, using `tools/audit/collect_samples.py` to capture and `replay_capture.py` to replay; add a `pytest` case over the T05/T14 simulator strata asserting the effective resolution stated here. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — no surface may claim a 5 s measurement resolution; the constraint must be stated in the UI spec and in the freshness tooltip. |
| **Owner** | orchestrator, with the measurement subsystem |
| **Status** | OPEN |
| **Source** | `PROJECT_STATUS.md:27`; `docs/decisions/README.md` (ADR-0003); `config/settings.py:158`; `03-measurement-and-electrical-domain.md` (T05, T14 strata) |

### OD-31 — Raw-retention default versus the `REQ-STORAGE-002` acceptance range

| Field | Value |
| --- | --- |
| **ID** | OD-31 |
| **Description** | The overview states raw retention is "configurable in 7–14 d, **defaulting to the current 30 d**", while `REQ-STORAGE-002`'s acceptance criteria require the value to be "**accepted in 7–14**" and "**refused at startup**" outside that range. A 30 d default is admissible only if 30 is a ceiling, which the requirement's own range does not admit. |
| **Impact** | Requirement: `REQ-STORAGE-002` (`01-requirements.md:564,566`). The startup validator, the retention cascade (`config/settings.py:151-152`) and the storage acceptance criteria all change together. A strict 7–14 validator would refuse the shipped default and the engine would not start. |
| **Current assumption** | V1 keeps `retention_days_raw = 30` and the validator accepts 1–90 d, so the shipped default is always admissible. The requirement text is referred back for rewording rather than the code being changed. Reversible by `WM_RETENTION_DAYS_RAW`. |
| **Validation needed** | A `pytest` case asserting the startup validator accepts the shipped default (30) and refuses 0 and 91; a manual check that `WM_RETENTION_DAYS_RAW=7` and `=14` also validate; an orchestrator decision on which of the two ranges is authoritative. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — the validator range must be a superset of `{7, 14, 30}` until `REQ-STORAGE-002` is reworded; no validator may refuse the value the shipped default uses. |
| **Owner** | orchestrator, with `user` for the requirement text |
| **Status** | OPEN |
| **Source** | `00-project-overview.md:431-438` (`OD-03`); `01-requirements.md:564,566` (`REQ-STORAGE-002`); `config/settings.py:151` |

### OD-32 — No creation path for `MeasurementFactor`

| Field | Value |
| --- | --- |
| **ID** | OD-32 |
| **Description** | There is no way to create a measurement factor from the integration or the HTTP API, so every real install classifies as `no_factor_declared`. |
| **Impact** | Requirement: [REQ:classification]. Per-device calibration and the device-classification surface stay at their declared-absent state; the per-device accuracy and calibration requirements cannot be exercised on a real install. |
| **Current assumption** | V1 adds **no** creation path. `no_factor_declared` is the expected first-run state of every device, not an error state; the classification pipeline and the UI must render it as a declared-absent condition. |
| **Validation needed** | An API contract test asserting that a device with no factor classifies as `no_factor_declared`; a manual UI check that such a device shows the declared-absent badge and not an error state. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — no code path may treat `no_factor_declared` as a validation failure, and the V1 scope statement must say the creation path is not implemented. |
| **Owner** | orchestrator (V1 scope); `user` (product decision on an authoring surface) |
| **Status** | OPEN |
| **Source** | `docs/limitations_v2.md` §5 |

### OD-33 — `audit_config_source` is always `"global"`

| Field | Value |
| --- | --- |
| **ID** | OD-33 |
| **Description** | The audit pass always reports `config_source="global"`; the audit record carries no per-device provenance. |
| **Impact** | Requirement: [REQ:audit]. The audit record's provenance column changes if per-device sources are introduced; an auditor currently cannot tell a device configured in one place from a device running on defaults. |
| **Current assumption** | V1 keeps `config_source="global"`, and the audit contract defines the field as "the configuration namespace the device resolved from", not as a per-device edit trail. |
| **Validation needed** | A contract test on the audit record asserting `config_source` is present and equals `"global"` under the shipped configuration; a manual check that the audit view labels the value as global scope. |
| **Implementation may proceed?** | YES |
| **Owner** | audit subsystem |
| **Status** | ASSUMED |
| **Source** | `docs/limitations_v2.md` §8 |

### OD-34 — `ConfidenceVector.topology` and `.residual` are declared but never populated

| Field | Value |
| --- | --- |
| **ID** | OD-34 |
| **Description** | The confidence vector declares a `topology` dimension and a `residual` dimension that no code path ever fills. |
| **Impact** | Requirement: [REQ:confidence]. The confidence contract changes (two dimensions become meaningful), the confidence-breakdown UI gains two components, and the audit's confidence arithmetic changes if they are weighted. |
| **Current assumption** | V1 keeps both dimensions declared-but-unpopulated; the effective confidence is computed from the populated dimensions only, and the UI must not render the two as if they carried information. The contract states that their derivation is undefined in V1. |
| **Validation needed** | A `pytest` case asserting the populated dimensions and that the effective confidence is unchanged when `topology` and `residual` are zeroed; a manual UI check that no badge or gauge derives from them. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — the two dimensions must not be published as populated scores, and the API contract must state their derivation is undefined. |
| **Owner** | disaggregation subsystem |
| **Status** | OPEN |
| **Source** | `docs/confidence_model.md` §2; `docs/limitations_v2.md` §8 |

### OD-35 — `docs/` version strings and workflow references are stale

| Field | Value |
| --- | --- |
| **ID** | OD-35 |
| **Description** | `docs/api.md:7` says `1.6.0` while the code says `2.0.0` (`contracts.py:32`); `docs/architecture.md:3` says `1.4.0`; `PROJECT_STATUS.md`'s header is stale; and the `app-image.yaml` references point at a workflow that does not exist — the App image is built by the `image` job in `release.yaml`. |
| **Impact** | Requirement: [REQ:docs]. Any acceptance step or release note that cites a version string or the image-build path reads the wrong value; the release documentation is wrong about how the App image is produced. |
| **Current assumption** | The documentation refresh is a **separate change** and is not part of the V1 implementation series; no acceptance criterion in this specification set cites those version strings or the `app-image.yaml` path. |
| **Validation needed** | A doc-freshness check comparing `docs/api.md` against `contracts.py:32` and `docs/architecture.md` against the shipped version, run as part of the docs change; the dead workflow reference is removed in the same change. |
| **Implementation may proceed?** | YES |
| **Owner** | orchestrator (docs change), not the implementation series |
| **Status** | DEFERRED |
| **Source** | `docs/api.md:7`; `docs/architecture.md:3`; `contracts.py:32`; `PROJECT_STATUS.md`; `.github/workflows/release.yaml` |

### OD-36 — Nine live-capture tests skip without `WATTMOSAIC_REAL_DB`

| Field | Value |
| --- | --- |
| **ID** | OD-36 |
| **Description** | Nine tests in `tests/simulation/test_real_topology.py` skip unless a local `WATTMOSAIC_REAL_DB` is present; they never run in CI. |
| **Impact** | Requirement: [REQ:live-capture]. The live-path coverage implied by the test suite is not exercised by CI, so any acceptance step that names live-capture replay as its evidence is unproven in the automated run. |
| **Current assumption** | V1 leaves the skip in place. The release note and the validation report must state that live-path coverage was not executed in CI. The skip must not be deleted to make CI green. |
| **Validation needed** | Run `pytest tests/simulation/test_real_topology.py` with a local capture DB and record the result; a `pytest` check that the nine tests are still present and still skipped rather than removed. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — the skip stays visible and the non-execution is disclosed in the release notes; un-skipping is not a V1 release blocker. |
| **Owner** | orchestrator (release disclosure); quality owner (local run) |
| **Status** | OPEN |
| **Source** | `tests/simulation/test_real_topology.py`; `docs/limitations_v2.md` §3 |

### OD-37 — `mypy` delta-zero policy and the pre-existing error debt

| Field | Value |
| --- | --- |
| **ID** | OD-37 |
| **Description** | `mypy engine/wattmosaic_engine` currently reports "Found 112 errors in 27 files (checked 80 source files)"; `docs/final_validation_report.md:23` records 120; the sibling specs `00` and `01` say 109. Three figures, one tree. CI treats mypy as non-blocking (`.github/workflows/ci.yml:25`). |
| **Impact** | Requirement: [REQ:quality]. Any acceptance criterion that says "the type check passes" is unmet today, and three different baselines make a delta policy ambiguous. |
| **Current assumption** | V1 policy is **delta-zero**: the error count must not increase above the measured 112, and the pre-existing debt is out of V1 scope. mypy stays non-blocking in CI. |
| **Validation needed** | Re-run `mypy engine/wattmosaic_engine` before and after each change and compare the summary line; reconcile 109 / 112 / 120 to one recorded baseline, with the command and its output as the evidence. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — no change may raise the error count above the recorded baseline; CI must not be made green by weakening the mypy configuration. |
| **Owner** | orchestrator (policy); engine owner (debt) |
| **Status** | OPEN |
| **Source** | measured `mypy engine/wattmosaic_engine` → "Found 112 errors in 27 files (checked 80 source files)"; `docs/final_validation_report.md:23`; `.github/workflows/ci.yml:25`; `00-project-overview.md` and `01-requirements.md` (109) |

### OD-38 — Approval of the specification set itself — **RESOLVED: "APPROVED_FOR_IMPLEMENTATION".**

| Field | Value |
| --- | --- |
| **ID** | OD-38 |
| **Description** | `REQ-FOUNDATION-001` made the whole specification set a draft until the orchestrator recorded a review decision. The review gate has now passed, and the set is approved for implementation. |
| **Impact** | Requirement: `REQ-FOUNDATION-001`. A deliverable may claim an "approved specification" from this commit onward; the release sign-off and external citation of the spec follow the implementation. |
| **Current assumption** | The set is **approved**: every file that declares a status carries `SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION`. The V1 defaults in §6 are the contract of record. |
| **Validation needed** | An orchestrator review decision recorded against `REQ-FOUNDATION-001` in the decision log; a checklist showing every item in this register is either `RESOLVED` or explicitly accepted as `ASSUMED`. **Met**: the orchestrator's plan-integrity verification passed all five sub-checks and found no hard-rule violation; the approval is recorded in the status lines of the set and in `REQ-FOUNDATION-001`. |
| **Implementation may proceed?** | YES — the specification is approved for implementation; the §6 defaults are the contract of record. |
| **Owner** | orchestrator |
| **Status** | RESOLVED — closed by the orchestrator's approval of the specification set, recorded as `SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION` across the set and as the status of `REQ-FOUNDATION-001` (2026-11-16). |
| **Source** | `01-requirements.md` (`REQ-FOUNDATION-001`); `00-project-overview.md`; `README.md` |

---

## 4. Assumptions recorded by this register

### ASM-01 — The accuracy claim is simulator-derived only

| Field | Value |
| --- | --- |
| **ID** | ASM-01 |
| **Description** | Every published accuracy figure is simulator-derived. There is no real-house per-device ground truth, and real household data must never be published. |
| **Impact** | Requirement: [REQ:accuracy]. The shape of every accuracy claim changes: no per-device accuracy may be asserted for a customer install, and no figure derived from real household data may appear in any artefact. |
| **Current assumption** | V1 claims **only** simulator-verified performance on the published strata, with the stratum, sample count and simulator version named next to every figure. Explicitly **not** claimed: real-house accuracy, per-device accuracy on a real install, or any figure derived from real household data. |
| **Validation needed** | A release-note review with the CI `privacy` job (`.github/workflows/ci.yml:37`) as the enforcement vehicle; a manual check that every accuracy number in the release notes has a named simulator stratum beside it. |
| **Implementation may proceed?** | YES |
| **Owner** | orchestrator; `user` for any external claim |
| **Status** | ASSUMED |
| **Source** | `docs/limitations_v2.md` §1; `.github/workflows/ci.yml:37` |

### ASM-02 — The replay boundary is a local capture, never a live instance

| Field | Value |
| --- | --- |
| **ID** | ASM-02 |
| **Description** | The owner chose "simulator + an authorized local live-capture replay", but touching a live Home Assistant instance needs explicit authorization. |
| **Impact** | Requirement: [REQ:live-capture]. Any validation step that needs real data must be expressed as capture-and-replay; a live read changes the authorization requirement and the privacy posture, and would make the run subject to `docs/WATTMOSAIC_MASTER_INSTRUCTIONS.md` §4.2. |
| **Current assumption** | The replay runs against a **local capture** — `tools/audit/collect_samples.py` produces it, `replay_capture.py` consumes it — and requires no live access to a Home Assistant instance. A live read is out of scope until a separate, explicit authorization is recorded. |
| **Validation needed** | A manual run of `tools/audit/collect_samples.py` against a capture followed by a `replay_capture.py` run over it, with no Home Assistant credentials present in the environment; a check that the replay path fails closed when live credentials are absent. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — no validation step may connect to a live Home Assistant instance under this assumption. |
| **Owner** | `user` (authorization); orchestrator (scope) |
| **Status** | ASSUMED |
| **Source** | `docs/WATTMOSAIC_MASTER_INSTRUCTIONS.md` §4.2; `tools/audit/collect_samples.py`; `replay_capture.py` |

### ASM-03 — One engine process per database file

| Field | Value |
| --- | --- |
| **ID** | ASM-03 |
| **Description** | Two engine processes pointed at the same SQLite file contend. |
| **Impact** | Requirement: [REQ:deployment]. The deployment topology (one engine per host/container) and any scale-out requirement change; a multi-process deployment would need a different storage engine, which is out of V1 scope. |
| **Current assumption** | V1 supports exactly **one** engine process per database file. A second writer is unsupported: the engine logs an explicit advisory, or refuses to start, when it detects another writer, and the deployment documentation says so. |
| **Validation needed** | A manual two-process run against one database file showing contention and the advisory; a `pytest` case asserting the single-writer detection path. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — the deployment and the docs state one writer per database file; no horizontal scale-out of the engine is offered in V1. |
| **Owner** | engine / runtime subsystem |
| **Status** | ASSUMED |
| **Source** | `docs/limitations_v2.md` §7.1 |

### ASM-04 — The implementation lands as one reviewed series on `main`

| Field | Value |
| --- | --- |
| **ID** | ASM-04 |
| **Description** | The owner has authorized committing and pushing to `main`, and `release.yaml` publishes a prerelease on **every** push to `main`. 16 git worktrees already exist under `~/.local/share/opencode/worktree/d661cf/`. |
| **Impact** | Requirement: [REQ:release]. The commit strategy and the release cadence change: feature-branch merges add review gates and a different prerelease trigger, and the relationship of the existing worktrees to the series must be settled before the series starts. |
| **Current assumption** | The implementation lands as **one reviewed series** of commits on `main`, in dependency order (specs → contracts → code → docs → tests), accepting that every push publishes a prerelease. Existing worktrees are inspected for unmerged work before the series starts, and no shared branch is rewritten. |
| **Validation needed** | `git log --oneline main` after the series, confirming it is linear and reviewed; `git worktree list` to confirm no unmerged worktree is orphaned by the series. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — the series is reviewed before the final push; published prereleases are not retracted, and every push publishes one by design. |
| **Owner** | `user` (branch policy); orchestrator (execution) |
| **Status** | ASSUMED |
| **Source** | `.github/workflows/release.yaml:44-46` (trigger block), `:105-133` (version/prerelease logic); `~/.local/share/opencode/worktree/d661cf/` (16 worktrees) |

### ASM-05 — Residual baseline window: `residual_baseline_window_s = 3600.0` (proposed)

| Field | Value |
| --- | --- |
| **ID** | ASM-05 |
| **Description** | `ResidualScope.window_w` is never populated by any caller and has no shipped configuration field, so the residual baseline window is genuinely open rather than merely undocumented. |
| **Impact** | Requirement: [REQ:residual]. The residual-inference requirement and the corresponding API field change if a different window is chosen: too short a window makes the baseline noisy, too long makes it stale relative to the load profile. |
| **Current assumption** | V1 ships `residual_baseline_window_s = 3600.0` (1 h) as a new `EngineConfig` field with env `WM_RESIDUAL_BASELINE_WINDOW_S`, constrained to be at least `4 × inference_interval_s` and at least `isolation_window_s`. Until the field exists, the residual path uses 3600 s as an explicit constant so the behaviour is visible in code rather than implicit. |
| **Validation needed** | A `pytest` case asserting the bound rejects a window smaller than `4 × inference_interval_s`; a simulator stratum that sweeps the window and reports the residual error for each value. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — the window is a configuration field and never a constant baked into the algorithm or silently defaulted at the call site. |
| **Owner** | disaggregation subsystem |
| **Status** | ASSUMED |
| **Source** | `ResidualScope.window_w` (no caller populates it); no corresponding field exists in `config/settings.py` |

### ASM-06 — Mapping-matrix refresh trigger: `mapping_refresh_interval_s = 3600.0` (proposed), disabled by default — **RESOLVED: "Build it in V1."**

| Field | Value |
| --- | --- |
| **ID** | ASM-06 |
| **Description** | The refresh trigger for the mapping projection has no shipped configuration field, so it is not configurable today; the projection itself **is** built in V1. |
| **Impact** | Requirement: `REQ-MAPPING-001`, `REQ-MAPPING-002`, `REQ-MAPPING-003`, `REQ-MAPPING-004`, `REQ-MAPPING-005`, `REQ-MAPPING-006`, `REQ-MAPPING-007`. This item **supersedes its earlier reading**, which held that the mapping module was outside V1 and that the mapping matrix governed nothing — seven requirements in `14-requirement-traceability.md` §7.11 do depend on it. The consequences of the owner's decision: the matrix projection is a **new module** (`engine/wattmosaic_engine/disaggregation/mapping.py`) and `GET /mapping` is a **new route**; both are **additive** and consistent with the no-new-database and no-new-service rules, but the module **is** new code, which the previous reading of this item denied. The item **unblocks** `TASK-MAPPING-001` and `TASK-MAPPING-002`; no requirement's text, priority, acceptance criteria or verification method changes. |
| **Current assumption** | The mapping module **is** built in V1 — the project owner's decision, recorded 2026-11-16 ("Build it in V1."). The refresh trigger is specified as `mapping_refresh_interval_s = 3600.0` (1 h, aligned with `reanalysis_interval_s`) with env `WM_MAPPING_REFRESH_INTERVAL_S`, and `mapping_refresh_enabled = false` by default — disabled because the projection is derived on read, so a periodic refresh is an optimisation rather than a requirement. The configuration fields must exist in `EngineConfig` and be added to `addon/run.sh`'s `OPTION_TABLE` and `addon/config.yaml`'s `options`/`schema` (the three-place rule) **only if** the implementation exposes them as App options; the env var alone is acceptable for V1. |
| **Validation needed** | A unit test that the projection is deterministic and derived — the same inputs give the same matrix and the matrix is never written (`UT-mapping::test_mapping_is_derived_and_deterministic`); a test that every cardinality — 1:1, 1:N, N:1 and N:N — is expressible (`UT-mapping::test_mapping_supports_every_cardinality`); a test that the response carries a coverage block naming what the matrix cannot see (`UT-mapping::test_mapping_reports_its_coverage`). |
| **Implementation may proceed?** | YES |
| **Owner** | `user` (decided); the implementation is owned by the mapping subsystem |
| **Status** | RESOLVED — closed by the project owner's decision ("Build it in V1."), recorded in this item on 2026-11-16. |
| **Source** | Project owner decision ("Build it in V1"), recorded by the orchestrator on 2026-11-16 and cited in this item; `config/settings.py:223` (`reanalysis_interval_s`) for the alignment value; no mapping field exists in `config/settings.py` today |

### ASM-07 — Event-evidence retention horizon: `event_evidence_retention_days = 90` (proposed)

| Field | Value |
| --- | --- |
| **ID** | ASM-07 |
| **Description** | Event-evidence records have no shipped configuration field and therefore no declared retention horizon. |
| **Impact** | Requirement: [REQ:storage]. The storage and audit-retention requirement changes; if evidence is dropped sooner than the raw window it justifies, the audit trail loses the records that explain a historical inference. |
| **Current assumption** | V1 ships `event_evidence_retention_days = 90` (3 × the 30 d raw default) as a new `EngineConfig` field with env `WM_EVENT_EVIDENCE_RETENTION_DAYS`; evidence therefore outlives the raw window it is derived from and is pruned by the same cascade. |
| **Validation needed** | A `pytest` case asserting the cascade keeps evidence for at least `retention_days_raw` days; a simulator run showing evidence pruning at the stated horizon. |
| **Implementation may proceed?** | YES-WITH-CONSTRAINT — the horizon must be ≥ `retention_days_raw`; a smaller value needs an explicit owner decision recorded here. |
| **Owner** | storage subsystem |
| **Status** | ASSUMED |
| **Source** | no shipped field for event-evidence retention in `config/settings.py`; `config/settings.py:151-152` for the retention defaults it must exceed |

---

## 5. Sibling harvest and ID renumbering

All siblings in the `04`–`12` range have landed. Their open decisions are **harvested** below. Six bands collided — four files independently claimed `OD-21`–`OD-30`, two files claimed `OD-09`–`OD-15`, and two claimed `OD-03.5`–`OD-03.9` — so the overlapping items are **renumbered** into the free `OD-40` onward band (§5.2). The sibling files must be corrected by the orchestrator's correction pass; this register does not edit them.

### 5.1 Landed siblings and their harvested open decisions

| Source file | Title | Open decisions as written | Canonical IDs | Status | Note |
| --- | --- | --- | --- | --- | --- |
| `00-project-overview.md` | Project overview | `OD-01`–`OD-08` | unchanged | LANDED | Inherited at §2.1. |
| `03-measurement-and-electrical-domain.md` | Measurement and electrical domain | `OD-03.1`–`OD-03.4` | unchanged | LANDED | Inherited at §2.2. |
| `01-requirements.md` | Requirements | `OD-09` | `OD-09` **unchanged** | LANDED | **Retained** — a distinct decision about the T-catalogue conflict between `01-…` §4.1 and `11-…` §5.1; deliberately not renumbered, because it is not one of the colliding bands (see §8.2). |
| `05-residual-model-spec.md` | Residual model | `OD-03.5`–`OD-03.9` (5) | `OD-40`–`OD-44` | LANDED | A dedicated unknown-residual event model vs a `DeviceEvent` sentinel; `R_r` derived-on-read vs persisted; the noise floor per Reader/scope/raw CT; the profile retirement horizon; the promotion artefact vs a `TopologyRelation`. |
| `06-device-and-load-model-spec.md` | Device and load model | `OD-03.5`–`OD-03.8`, `OD-03.10`–`OD-03.11` (6) | `OD-45`–`OD-50` | LANDED | The power-class field shape; `ALWAYS_ON` as a flag; `DELAYED` as an orthogonal parameter; component persistence; acceptance criteria for shape and curve models; the `Source` labelling for endpoint-less devices. `OD-03.9` and `OD-03.12` stay **unchanged** (RESOLVED, §2.4). |
| `04-topology-spec.md` | Measurement topology | `OD-21`–`OD-30` (10) | `OD-51`–`OD-60` | LANDED | `CANDIDATE` vs `PROVISIONAL`; `SUSPECTED_CHANGE` as an edge state vs an overlay; automatic demotion of a `CONFIRMED` edge; overlapping `CONTAINS` edges; phase evidence for `PAIRED_LEG`; whether `CORRELATED` may reach `CONFIRMED`; the `valid_until` rename; rollback as a new version; `topology_version` as an integer; the unknown residual as a topology attribute. |
| `07-event-and-inference-spec.md` | Event and inference | `OD-21`–`OD-26` (6) | `OD-61`–`OD-66` | LANDED | `received_at` as a first-class field; automation groups as transient artefacts; the delay estimate's scope; a range as a field vs a new `Source`; the cross-reader vector persisted vs reconstructed; rate limiting on `POST /events/replay`. |
| `08-storage-and-data-lifecycle.md` | Storage and data lifecycle | `OD-21`–`OD-27` (7) | `OD-67`–`OD-73` | LANDED | The default L1 raw horizon; the L2/L4 horizon; the split-assignment policy; the storage-health surface's location; L2 as one new table vs an extension of `device_events`; the queue-drop rate limit; the `context` encoding. |
| `09-api-and-data-contracts.md` | API and data contracts | `OD-09`–`OD-15` (7) | `OD-74`–`OD-80` | LANDED | The `CONTRACT_VERSION` bump boundary; `POST /events/replay` in production; append-only rollback; the basis of a mixed-basis mapping rollup; who prunes; `X-API-Token` scoping and rotation; `EventEvidenceRecord.payload` retention. |
| `10-ui-and-user-experience-spec.md` | UI and user experience | `OD-09`–`OD-15` (7) | `OD-81`–`OD-87` | LANDED | The mapping endpoint shape; the confidence vector on card vs detail; the provenance and storage strips' placement; destructive operations staying Home Assistant-only per ADR-0027; the diff style; the retention horizon; polling vs manual refresh. |
| `11-test-and-validation-plan.md` | Test and validation plan | `OD-21`–`OD-31` (11) | `OD-88`–`OD-98` | LANDED | The seven new simulator strata; the database size-growth threshold; the `monitor_published` hot-tub variant; the J-group matrix rows; the pure-JS UI harness; `--cov` in CI; the `G-TEST-nn` gap reconciliation; the two non-T gap closures; a generation device in the reference topology; the simulator's relocation / replacement capability; the recurring-pattern mode for `UnknownLoadProfile`. |
| `12-deployment-and-rollback.md` | Deployment and rollback | `OD-09`–`OD-13` (5) | `OD-99`–`OD-103` | LANDED | Backup cadence and retention; the same image; the pre-migration backup form; audit retention's locus; the smoke-test CI wiring. |
| `14-requirement-traceability.md` | Requirement traceability | none raised | — | LANDED | Traces `OD-31`, `OD-34`, `OD-36`, `OD-38`; opens no decision of its own. |
| `15-implementation-plan.md` | Implementation plan | none raised | — | LANDED | Traces `OD-31`, `OD-34`, `OD-36`, `OD-38`; opens no decision of its own. |

**Rule.** A sibling's items map to its canonical block **in band order**: the first item of the band takes the first ID of the block and each subsequent item takes the next, so the correction pass is mechanical. Block sizes match the band extents counted above.

### 5.2 ID-collision renumbering

Six bands overlapped. The `OD-21`–`OD-30` range was claimed independently by **four** files (`04`, `07`, `08`, `11`); the `OD-09`–`OD-15` range by **two** (`09`, `10`); the `OD-03.5`–`OD-03.9` range by **two** (`05`, `06`). The collision is resolved by **renumbering, not deleting**: each sibling's items keep their band order and move into a contiguous block in the free `OD-40` onward band.

| Sibling file | Old ID | New ID | Subject |
| --- | --- | --- | --- |
| `05-residual-model-spec.md` | `OD-03.5` … `OD-03.9` | `OD-40` … `OD-44` | Unknown-residual event model; `R_r`; noise floor scope; retirement horizon; promotion artefact. |
| `06-device-and-load-model-spec.md` | `OD-03.5`, `OD-03.6`, `OD-03.7`, `OD-03.8` | `OD-45`, `OD-46`, `OD-47`, `OD-48` | Power-class field; `ALWAYS_ON` flag; `DELAYED` parameter; component persistence. |
| `06-device-and-load-model-spec.md` | `OD-03.10`, `OD-03.11` | `OD-49`, `OD-50` | Shape/curve acceptance criteria; `Source` labelling for endpoint-less devices. |
| `06-device-and-load-model-spec.md` | `OD-03.9`, `OD-03.12` | **unchanged** | RESOLVED by the owner (§2.4); `06-…` must keep citing them. |
| `04-topology-spec.md` | `OD-21` … `OD-30` | `OD-51` … `OD-60` | Edge lifecycle and topology-version decisions. |
| `07-event-and-inference-spec.md` | `OD-21` … `OD-26` | `OD-61` … `OD-66` | Event-record and inference decisions. |
| `08-storage-and-data-lifecycle.md` | `OD-21` … `OD-27` | `OD-67` … `OD-73` | Storage-tier decisions. |
| `09-api-and-data-contracts.md` | `OD-09` … `OD-15` | `OD-74` … `OD-80` | API and contract decisions. |
| `10-ui-and-user-experience-spec.md` | `OD-09` … `OD-15` | `OD-81` … `OD-87` | Panel decisions. |
| `11-test-and-validation-plan.md` | `OD-21` … `OD-31` | `OD-88` … `OD-98` | Test-plan decisions. |
| `12-deployment-and-rollback.md` | `OD-09` … `OD-13` | `OD-99` … `OD-103` | Deployment decisions. |

**Note.** `05-…` and `06-…` both numbered an item `OD-03.9`; they are different questions. The owner-resolved `OD-03.9` (may the system name an appliance?) is **`06-…`'s** and keeps its ID; `05-…`'s `OD-03.9` (the promotion artefact) renumbers to `OD-44` with the rest of `05-…`'s band.

**Note.** `01-requirements.md`'s `OD-09` is **not** part of any colliding band and is **retained** unchanged — it is the T-catalogue conflict described in §8.2.

**Rule.** The blocks above do not overlap each other, this register's own `OD-30`–`OD-38` / `ASM-01`–`ASM-07`, or the inherited `OD-01`–`OD-08` / `OD-03.1`–`OD-03.4`.

**Rule.** The old IDs are valid **only** in the sibling files until the orchestrator's correction pass rewrites them; after that pass the `OD-40` onward IDs are the only valid ones.

**Rule.** The two owner-resolved IDs `OD-03.9` and `OD-03.12` are **not** renumbered — the owner's answers are recorded against them (§2.4) and `06-…` must keep citing them.

**Rule.** This register's own `OD-30`–`OD-38` and `ASM-01`–`ASM-07` are **not** renumbered.

**Rule.** `OD-09`–`OD-39` are retired as standalone IDs and must not be reused — except `01-requirements.md`'s `OD-09`, which is retained (§8.2), and except `OD-30`–`OD-38` and `OD-03.9` / `OD-03.12`, which are live items of this register and its inherited and owner-resolved bands. `OD-39` was never allocated to anything and is retired with the rest of the band.

---

## 6. Selected V1 defaults

The concrete values the implementation proceeds on. A default marked *(proposed)* is specified here and has **no shipped configuration field yet** — the field to add is named in the third column.

| Parameter | V1 value | Where it is configured | Requirement it serves |
| --- | --- | --- | --- |
| `inference_interval_s` | 5.0 | `EngineConfig.inference_interval_s`; env `WM_INFERENCE_INTERVAL_S` (`config/settings.py:158`) | [REQ:cadence] — see OD-30 |
| `stale_after_s` | 120.0 | `EngineConfig.stale_after_s`; env `WM_STALE_AFTER_S` (`config/settings.py:157`) | [REQ:freshness] |
| `retention_days_raw` | 30 | `EngineConfig.retention_days_raw`; env `WM_RETENTION_DAYS_RAW` (`config/settings.py:151`) | `REQ-STORAGE-002` — see OD-31 |
| `retention_days_aggregates` | 730 | `EngineConfig.retention_days_aggregates`; env `WM_RETENTION_DAYS_AGGREGATES` (`config/settings.py:152`) | [REQ:storage] |
| `queue_max_size` | 5000 | `EngineConfig.queue_max_size`; env `WM_QUEUE_MAX_SIZE` (`config/settings.py:159`) | [REQ:ingestion] |
| `analysis_offset_s` | 0.0 | `EngineConfig.analysis_offset_s` (`config/settings.py:195`) | [REQ:alignment] |
| `isolation_window_s` | 30.0 | `EngineConfig.isolation_window_s` (`config/settings.py:199`) | [REQ:isolation] |
| `isolation_power_window_s` | 60.0 | `EngineConfig.isolation_power_window_s` (`config/settings.py:200`) | [REQ:isolation] |
| `min_samples_for_model` | 12 | `learning/thresholds.py:99-107`; mirrored in `config/settings.py:227-230` | [REQ:learning] |
| `min_validation_r2` | 0.70 | `learning/thresholds.py:99-107`; mirrored in `config/settings.py:227-230` | [REQ:learning] |
| `promotion_margin` | 0.05 | `learning/thresholds.py:99-107`; mirrored in `config/settings.py:227-230` | [REQ:learning] |
| `max_sample_rejection_ratio` | 0.60 | `learning/thresholds.py:99-107`; mirrored in `config/settings.py:227-230` | [REQ:learning] |
| `audit_algorithm_version` | `disagg@2.0` | `DEFAULT_AUDIT_ALGORITHM_VERSION` (`config/settings.py:35`); env `WM_AUDIT_ALGORITHM_VERSION` | [REQ:audit] |
| `MAX_CONFIDENCE` | 0.99 | `disaggregation/association.py:144` | [REQ:confidence] |
| `phase_split_l1` | 0.5 | `electrical/__init__.py:91` | [REQ:phase] |
| `reanalysis_interval_s` | 3600.0 | `EngineConfig.reanalysis_interval_s` (`config/settings.py:223`) | [REQ:reanalysis] |
| `reanalysis_lookback_s` | 21600.0 | `EngineConfig.reanalysis_lookback_s` (`config/settings.py:225`) | [REQ:reanalysis] |
| `history_lookback_s` | 86400.0 | `EngineConfig.history_lookback_s` (`config/settings.py:177`) | [REQ:history] |
| `RECORD_HEARTBEAT_S` | 3600.0 | `disaggregation/audit.py:107` | [REQ:audit] |
| residual baseline window *(proposed)* | 3600.0 | **No shipped field.** To add: `EngineConfig.residual_baseline_window_s`; env `WM_RESIDUAL_BASELINE_WINDOW_S` | ASM-05 |
| mapping-matrix refresh trigger *(proposed)* | 3600.0, disabled by default | **No shipped field** — the field is **new in V1**. To add: `EngineConfig.mapping_refresh_interval_s`; env `WM_MAPPING_REFRESH_INTERVAL_S`. The mapping module itself is built in V1 (ASM-06, RESOLVED) | ASM-06 |
| event-evidence retention horizon *(proposed)* | 90 days | **No shipped field.** To add: `EngineConfig.event_evidence_retention_days`; env `WM_EVENT_EVIDENCE_RETENTION_DAYS` | ASM-07 |
| candidate-proposal confidence cap *(proposed)* | 0.90 | **No shipped field.** To add: `EngineConfig.candidate_confidence_cap`; env `WM_CANDIDATE_CONFIDENCE_CAP`. Bounded above by the absolute ceiling `MAX_CONFIDENCE = 0.99` (`disaggregation/association.py:144`) | OD-03.9 |
| minimum evidence count, state-model learned without an entity *(proposed)* | 24 samples (2 × `min_samples_for_model`) | **No shipped field.** To add: `EngineConfig.min_samples_stateless_model`; env `WM_MIN_SAMPLES_STATELESS_MODEL` | OD-03.12 |
| unknown-load profile retirement horizon *(proposed)* | 30 days of no observation | **No shipped field.** To add: `EngineConfig.unknown_profile_retirement_days`; env `WM_UNKNOWN_PROFILE_RETENTION_DAYS`. Aligned with the 30 d raw default | `OD-44` (from `05-…`) |

**Rule.** Every row marked *(proposed)* ships with a named field and env var in V1; a value that exists only as a constant in code is not a V1 default.

---

## 7. Consistency check — configurable versus structural

**Criterion.** A **configurable** assumption is one a user can change without a code change (an `EngineConfig` field, an env var, or a configuration file). A **structural** assumption is one baked into the contract or the algorithm and cannot be changed by configuration.

### 7.1 Configurable

| Assumption | Changeable through | Item |
| --- | --- | --- |
| Inference cadence | `WM_INFERENCE_INTERVAL_S` | OD-30 |
| Raw retention | `WM_RETENTION_DAYS_RAW` | OD-31 |
| Aggregate retention | `WM_RETENTION_DAYS_AGGREGATES` | OD-31 |
| Staleness threshold | `WM_STALE_AFTER_S` | OD-30 |
| Queue depth | `WM_QUEUE_MAX_SIZE` | OD-30 |
| Learning thresholds | `learning/thresholds.py`; mirrored `config/settings.py:227-230` | OD-30, OD-34 |
| Audit algorithm version | `WM_AUDIT_ALGORITHM_VERSION` | OD-33 |
| Residual baseline window | `WM_RESIDUAL_BASELINE_WINDOW_S` *(proposed)* | ASM-05 |
| Mapping refresh trigger | `WM_MAPPING_REFRESH_INTERVAL_S` *(proposed)* — the trigger only; the projection itself is built in V1 | ASM-06 |
| Event-evidence horizon | `WM_EVENT_EVIDENCE_RETENTION_DAYS` *(proposed)* | ASM-07 |
| Candidate-proposal confidence cap | `WM_CANDIDATE_CONFIDENCE_CAP` *(proposed)* | OD-03.9 |
| Minimum evidence, stateless state-model | `WM_MIN_SAMPLES_STATELESS_MODEL` *(proposed)* | OD-03.12 |
| Unknown-load profile retirement horizon | `WM_UNKNOWN_PROFILE_RETENTION_DAYS` *(proposed)* | `OD-44` |

### 7.2 Structural

| Assumption | Why it is structural | Item |
| --- | --- | --- |
| Event identity derivation | The code builds `uuid.uuid4()` and falls back to `device@timestamp`; a content-addressed derivation is not in the code path, and replay/dedup semantics follow from it | OD-03.1 |
| Confidence-vector shape | Two dimensions are declared in the contract but have no derivation | OD-34 |
| Single-writer storage model | SQLite contention is a property of the storage engine, not of a setting | ASM-03 |
| `no_factor_declared` as first-run state | With no creation path in V1, there is no configuration that produces a factor | OD-32 |
| Release cadence | Prerelease-on-every-push is a property of the workflow trigger | ASM-04 |
| Accuracy-claim discipline | A policy on what may be published, enforced by the CI privacy job | ASM-01 |
| Audit provenance scope | `config_source` is a single-valued contract field | OD-33 |
| A proposal is never a device | The rule that a candidate cannot become a device without confirmation is a contract invariant, not a setting | OD-03.9 |
| An estimate is never a measurement | The four-state labelling rule (`MEASURED` / `ESTIMATED` / `ALLOCATED` / …) is a contract invariant, not a setting | OD-03.12 |
| The mapping module is a **new module** in V1 | The projection is new code at `engine/wattmosaic_engine/disaggregation/mapping.py`, with `GET /mapping` as a new route; neither is a configuration choice | ASM-06 |

### 7.3 Does any item block the whole project?

**No.** No item in this register blocks the whole project. Every item has an owner, a status and a bounded assumption, and every uncertain parameter is either already configurable or has a named field to add.

The three items closest to blocking, and what each would block if it went the other way:

| Item | What it would block | Why it does not block |
| --- | --- | --- |
| OD-31 (retention contradiction) | Engine start-up, if the validator implements the requirement's 7–14 range literally and refuses the shipped 30 d default | The constraint in OD-31 makes the validator range a superset of `{7, 14, 30}`, so start-up is safe while the requirement text is settled |
| OD-38 (spec approval) | The "approved specification" claim and release sign-off | **RESOLVED.** The set is approved (`SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION`); the approval claim no longer waits on anything, and the item no longer blocks any task |
| ASM-04 (branch strategy) | The release mechanics — the prerelease cadence and the fate of the 16 existing worktrees | Nothing in the code depends on the branch strategy; the series is reviewable on `main` and the worktrees can be inspected before it starts |

OD-37 (the `mypy` delta) is next in line: it blocks nothing today because CI treats mypy as non-blocking, and the delta-zero policy keeps the count from rising.

The three owner-resolved items do not join this list: `OD-03.9`, `OD-03.12` and `ASM-06` are decided. `OD-03.9` blocks the **unknown-load identification path** if the confirmation surface is not built in the same wave, and `OD-03.12` blocks the **auto-learning path for endpoint-less devices** if the `ESTIMATED` / `ALLOCATED` labelling is not implemented with it. `ASM-06` blocks nothing: the mapping module is built in V1, and the seven requirements it governs proceed on their own task IDs. None of the three blocks the project as a whole: each is scoped to one subsystem, and in the two open cases the safe behaviour (propose nothing; learn nothing) remains available if the constrained path is deferred.

`OD-38` is a fourth resolved item, resolved by the orchestrator rather than by the project owner. It constrains nothing: the specification set is approved for implementation and every task proceeds on the §6 defaults.

### 7.4 ID integrity check

**Rule.** Every ID in this register is used exactly once. `OD-01`–`OD-08` and `OD-03.1`–`OD-03.4` keep the IDs their source files gave them; `OD-03.9` and `OD-03.12` keep theirs because the owner answered them (§2.4); `OD-30`–`OD-38` and `ASM-01`–`ASM-07` are this register's own.

**Rule.** The renumbered sibling items occupy ten disjoint blocks, each matching its source band's extent exactly:

| Block | Size | Source file | Band as written |
| --- | --- | --- | --- |
| `OD-40`–`OD-44` | 5 | `05-residual-model-spec.md` | `OD-03.5`–`OD-03.9` |
| `OD-45`–`OD-50` | 6 | `06-device-and-load-model-spec.md` | `OD-03.5`–`OD-03.8`, `OD-03.10`, `OD-03.11` |
| `OD-51`–`OD-60` | 10 | `04-topology-spec.md` | `OD-21`–`OD-30` |
| `OD-61`–`OD-66` | 6 | `07-event-and-inference-spec.md` | `OD-21`–`OD-26` |
| `OD-67`–`OD-73` | 7 | `08-storage-and-data-lifecycle.md` | `OD-21`–`OD-27` |
| `OD-74`–`OD-80` | 7 | `09-api-and-data-contracts.md` | `OD-09`–`OD-15` |
| `OD-81`–`OD-87` | 7 | `10-ui-and-user-experience-spec.md` | `OD-09`–`OD-15` |
| `OD-88`–`OD-98` | 11 | `11-test-and-validation-plan.md` | `OD-21`–`OD-31` |
| `OD-99`–`OD-103` | 5 | `12-deployment-and-rollback.md` | `OD-09`–`OD-13` |
| `OD-03.9`, `OD-03.12` | 2 | `06-device-and-load-model-spec.md` | unchanged — RESOLVED |

**Rule.** No two blocks overlap, none touches `OD-30`–`OD-38` / `ASM-01`–`ASM-07`, and none touches the inherited `OD-01`–`OD-08` / `OD-03.1`–`OD-03.4`. `OD-09`–`OD-39` are retired and unused, save `01-requirements.md`'s retained `OD-09` (§8.2).

---

## 8. The complete decision index

Every decision and assumption in the specification set, on one line each, so that
"What is open?" can be answered from this file alone. The **Subject** column is
lifted verbatim from the row the source file itself carries; the **Source file**
column names the file that *defines* the item, and the **Status** column is the
status that item carries there (or in this register, for the items raised here).

The table holds **95 rows**: 12 inherited from `00-`/`03-`, 1 retained from
`01-`, 2 owner-resolved (`OD-03.9`, `OD-03.12`), 9 open decisions raised by this
register, 7 assumptions recorded by this register, and 64 sibling items harvested
from the nine landed siblings in the `04`–`12` range.

### 8.1 The index

| ID | Subject | Source file | Status |
| --- | --- | --- | --- |
| `OD-01` | Does the target system introduce a first-class `UnknownLoad` entity in the contract, or keep unknown load as a residual attribute? | `00-project-overview.md` | OPEN |
| `OD-02` | Is the relation lifecycle a new enum, or an extension of `TopologyStatus`? | `00-project-overview.md` | OPEN |
| `OD-03` | What is the raw retention horizon: the shipped 30 days, or the 7–14 days the target brief names? | `00-project-overview.md` | OPEN |
| `OD-04` | Does `reader_configuration_version` become a new column or a field inside the topology payload? | `00-project-overview.md` | OPEN |
| `OD-05` | Should a mapping-matrix cell be persisted, or rebuilt on read? | `00-project-overview.md` | OPEN |
| `OD-06` | Is a "suspected topology change" a `TopologyRelation` of a new kind, or a distinct artefact? | `00-project-overview.md` | OPEN |
| `OD-07` | Does the target keep `Source.DIRECT_MEASURED` in the enum? | `00-project-overview.md` | OPEN |
| `OD-08` | Is the panel's topology view a new route or an extension of `#/topology`? | `00-project-overview.md` | OPEN |
| `OD-09` | A T-catalogue conflict between this file and `11-test-and-validation-plan.md` §5.1 | `01-requirements.md` | RESOLVED |
| `OD-03.1` | The `DeviceEvent.event_id` derivation. `docs/data_flow.md:75-78` documents `sha1(entity_id + last_updated + new_state)`; the code builds `str(uuid.uuid4())` (`ha_source.py:1186`) and `isolation.py:356-369` falls back to `f"{device_id}@{occurred_at.isoformat()}"`. | `03-measurement-and-electrical-domain.md` | OPEN |
| `OD-03.2` | Per-device phase-split fraction. `α` is global today (`electrical/__init__.py:91`). | `03-measurement-and-electrical-domain.md` | OPEN |
| `OD-03.3` | Energy-counter `state_class`. The engine infers counter behaviour from observed resets rather than reading a declared `state_class`. | `03-measurement-and-electrical-domain.md` | OPEN |
| `OD-03.4` | Whether the six unmeasured electrical quantities (rows 33–38) get a representation at all. | `03-measurement-and-electrical-domain.md` | OPEN |
| `OD-03.9` | May the system name an appliance on its own? | `13-open-decisions-and-assumptions.md` | RESOLVED |
| `OD-03.12` | How much may be inferred for a device with no Home Assistant integration? | `13-open-decisions-and-assumptions.md` | RESOLVED |
| `OD-30` | Effective inference resolution against the CT update cadence | `13-open-decisions-and-assumptions.md` | OPEN |
| `OD-31` | Raw-retention default versus the `REQ-STORAGE-002` acceptance range | `13-open-decisions-and-assumptions.md` | OPEN |
| `OD-32` | No creation path for `MeasurementFactor` | `13-open-decisions-and-assumptions.md` | OPEN |
| `OD-33` | `audit_config_source` is always `"global"` | `13-open-decisions-and-assumptions.md` | ASSUMED |
| `OD-34` | `ConfidenceVector.topology` and `.residual` are declared but never populated | `13-open-decisions-and-assumptions.md` | OPEN |
| `OD-35` | `docs/` version strings and workflow references are stale | `13-open-decisions-and-assumptions.md` | DEFERRED |
| `OD-36` | Nine live-capture tests skip without `WATTMOSAIC_REAL_DB` | `13-open-decisions-and-assumptions.md` | OPEN |
| `OD-37` | `mypy` delta-zero policy and the pre-existing error debt | `13-open-decisions-and-assumptions.md` | OPEN |
| `OD-38` | Approval of the specification set itself | `13-open-decisions-and-assumptions.md` | RESOLVED |
| `OD-40` | Does the unknown-residual event get its own contract model, or is it a `DeviceEvent` variant with a sentinel `device_id`? | `05-residual-model-spec.md` | OPEN |
| `OD-41` | Is `R_r` (the residual minus its noise estimate) published as a field, or only derived on read? | `05-residual-model-spec.md` | OPEN |
| `OD-42` | Is the noise floor per Reader, per scope, or per raw CT? | `05-residual-model-spec.md` | OPEN |
| `OD-43` | What is the retirement horizon for an unknown-load profile? | `05-residual-model-spec.md` | OPEN |
| `OD-44` | Does the promotion of a profile to `CANDIDATE` create a `TopologyRelation`, or an artefact of its own? | `05-residual-model-spec.md` | OPEN |
| `OD-45` | How the master's six power model classes are recorded on a device: a new field on `DeviceConfiguration` (`contracts.py:1376`), a new enum, or derivation from `ModelType` plus model parameters. | `06-device-and-load-model-spec.md` | OPEN |
| `OD-46` | Whether `ALWAYS_ON` is a new `ModelType` member or a flag on an existing one. | `06-device-and-load-model-spec.md` | OPEN |
| `OD-47` | Whether `DELAYED` is a new `ModelType` member or an orthogonal parameter. | `06-device-and-load-model-spec.md` | OPEN |
| `OD-48` | Whether a Load Component is a persisted contract entity or a model-internal concept. | `06-device-and-load-model-spec.md` | OPEN |
| `OD-49` | The acceptance criteria for a model with no control state (the shape model of §3.2) and for a non-linear curve (§7.2). | `06-device-and-load-model-spec.md` | OPEN |
| `OD-50` | Which `Source` labels a device that has no measurement endpoint of its own but is allocated a share of a solve. | `06-device-and-load-model-spec.md` | OPEN |
| `OD-51` | Does `CANDIDATE` mean "proposed, unvalidated" only, or also "validated but not yet user-confirmed"? | `04-topology-spec.md` | OPEN |
| `OD-52` | Is `SUSPECTED_CHANGE` a state of the *edge* or a separate overlay on the edge? | `04-topology-spec.md` | OPEN |
| `OD-53` | May a `CONFIRMED` edge be demoted automatically on persistent inconsistency, or only to `SUSPECTED_CHANGE`? | `04-topology-spec.md` | OPEN |
| `OD-54` | Should `CONTAINS` edges be allowed to overlap (a child in two parents)? | `04-topology-spec.md` | OPEN |
| `OD-55` | Are `PAIRED_LEG` edges allowed to be `CONFIRMED` on timing alone, or do they require phase evidence? | `04-topology-spec.md` | OPEN |
| `OD-56` | Is a `CORRELATED` edge ever `CONFIRMED`, or does it terminate at `PROVISIONAL`? | `04-topology-spec.md` | OPEN |
| `OD-57` | Does `valid_until` rename happen on the `TopologyRelation` contract, or on a new `MeasurementEdge` type? | `04-topology-spec.md` | OPEN |
| `OD-58` | Does rollback create a new version pointing at old content, or restore in place? | `04-topology-spec.md` | OPEN |
| `OD-59` | Is `topology_version` an integer that increments, or an opaque identifier? | `04-topology-spec.md` | OPEN |
| `OD-60` | Should the unknown residual be surfaced on the topology edge, or only in reconciliation diagnostics? | `04-topology-spec.md` | OPEN |
| `OD-61` | Should the event record carry `received_at` as a first-class field, or should lateness live only in diagnostics? | `07-event-and-inference-spec.md` | OPEN |
| `OD-62` | Should an automation group be a first-class persisted object, or a transient correlation artefact? | `07-event-and-inference-spec.md` | OPEN |
| `OD-63` | Should the delay estimate be per device, per device-class, or per event? | `07-event-and-inference-spec.md` | OPEN |
| `OD-64` | Should a range be a new `Source` value or a field on the existing result? | `07-event-and-inference-spec.md` | OPEN |
| `OD-65` | Should the cross-reader event vector be persisted, or reconstructed on demand? | `07-event-and-inference-spec.md` | OPEN |
| `OD-66` | Does `POST /events/replay` need rate limiting or a scoped role? | `07-event-and-inference-spec.md` | OPEN |
| `OD-67` | Default L1 raw horizon: ship 30 d, offer 7–14 d as an operator opt-in | `08-storage-and-data-lifecycle.md` | OPEN |
| `OD-68` | Default L2/L4 horizon: disabled (long-term) | `08-storage-and-data-lifecycle.md` | OPEN |
| `OD-69` | Split-assignment policy: who labels train vs evaluation | `08-storage-and-data-lifecycle.md` | OPEN |
| `OD-70` | Where the storage-health surface lives: HTTP read endpoint vs the existing diagnostics path | `08-storage-and-data-lifecycle.md` | OPEN |
| `OD-71` | Whether L2 is one new table or an extension of `device_events` | `08-storage-and-data-lifecycle.md` | OPEN |
| `OD-72` | Queue-drop `system_events` rate limit | `08-storage-and-data-lifecycle.md` | OPEN |
| `OD-73` | Whether the `context` block is a JSON column or discrete columns | `08-storage-and-data-lifecycle.md` | OPEN |
| `OD-74` | When is `CONTRACT_VERSION` bumped for the ten new contracts — at merge, or at a release boundary? | `09-api-and-data-contracts.md` | OPEN |
| `OD-75` | Does `POST /events/replay` run in production? | `09-api-and-data-contracts.md` | OPEN |
| `OD-76` | Is rollback append-only or in-place? | `09-api-and-data-contracts.md` | OPEN |
| `OD-77` | What basis does a mapping rollup carry when its cells have mixed bases? | `09-api-and-data-contracts.md` | OPEN |
| `OD-78` | Who prunes, and is pruning triggerable through the API? | `09-api-and-data-contracts.md` | OPEN |
| `OD-79` | How is `X-API-Token` scoped and rotated for browser-facing reads? | `09-api-and-data-contracts.md` | OPEN |
| `OD-80` | Is `EventEvidenceRecord.payload` retained by default (storage cost) or only `payload_hash`? | `09-api-and-data-contracts.md` | OPEN |
| `OD-81` | Does the device↔reader mapping get its own `GET /mapping` endpoint, or is it embedded in `GET /devices/:id` and `GET /topology`? | `10-ui-and-user-experience-spec.md` | OPEN |
| `OD-82` | Is the confidence vector shown on the device card, or only on the device detail? | `10-ui-and-user-experience-spec.md` | OPEN |
| `OD-83` | Do the provenance split and the storage strip appear on the overview, or only in diagnostics? | `10-ui-and-user-experience-spec.md` | OPEN |
| `OD-84` | Are the destructive engine operations (`reset_device_model`, `rebuild_topology`, `reject_sample`) reachable from the panel at all? | `10-ui-and-user-experience-spec.md` | OPEN |
| `OD-85` | Is the topology diff a text diff, a relation-level diff, or a visual overlay? | `10-ui-and-user-experience-spec.md` | OPEN |
| `OD-86` | What is the retention horizon the storage strip shows, and is it configurable by the user? | `10-ui-and-user-experience-spec.md` | OPEN |
| `OD-87` | Does the panel show live-updating values (polling) or a manual refresh only? | `10-ui-and-user-experience-spec.md` | OPEN |
| `OD-88` | Whether the seven new strata (`single_child_containment`, `multi_child_contributions`, `unobserved_parent_load`, `plug_relocation`, `reader_relocation`, `factor_timeline`, `factor_misconfigured`) are added to the canonical `STRATA` set or held in a separate testing-only registry | `11-test-and-validation-plan.md` | OPEN |
| `OD-89` | The database size-growth threshold (P4) in bytes per channel per day | `11-test-and-validation-plan.md` | OPEN |
| `OD-90` | Whether the `monitor_published` hot-tub variant (present in `tools/simulator/hot_tub.py:943`, absent from `tests/fixtures/labhouse.py:147-157`) is added to the required set | `11-test-and-validation-plan.md` | OPEN |
| `OD-91` | Whether the nine J-group matrix rows become `MATRIX_STRATA` entries backed by new learning strata, or the J group is re-scoped | `11-test-and-validation-plan.md` | OPEN |
| `OD-92` | Whether the pure-JS UI harness is acceptable without a node dependency, or the DOM-level check is formally deferred | `11-test-and-validation-plan.md` | OPEN |
| `OD-93` | Whether `--cov` is wired into CI so coverage becomes machine-measured | `11-test-and-validation-plan.md` | OPEN |
| `OD-94` | The reconciliation between locally-scoped `G-TEST-nn` IDs and the canonical gap register | `11-test-and-validation-plan.md` | OPEN |
| `OD-95` | Whether the two non-T gap closures (factor timeline, startup surge) are accepted as coverage for matrix groups B and A6/H4, given that the canonical T-catalogue is fixed and contains no such scenario | `11-test-and-validation-plan.md` | OPEN |
| `OD-96` | Whether the reference topology gains a **generation** device (solar / PV) so T15 "Bidirectional Power" can be exercised against a true export source | `11-test-and-validation-plan.md` | OPEN |
| `OD-97` | Whether `SimulatorConfig` gains an explicit relocation / replacement capability for T11 and T12, or the simulator grows a second-phase scenario model | `11-test-and-validation-plan.md` | OPEN |
| `OD-98` | Whether `UnknownLoadProfile` gains a recurring-pattern mode for T10, or a new profile class is added | `11-test-and-validation-plan.md` | OPEN |
| `OD-99` | Backup cadence and retention period | `12-deployment-and-rollback.md` | OPEN |
| `OD-100` | Whether the disaggregation capability ships in the same image as the current engine | `12-deployment-and-rollback.md` | OPEN |
| `OD-101` | Whether the pre-migration backup is a cold Supervisor backup or an online copy of the DB plus `-wal`/`-shm` | `12-deployment-and-rollback.md` | OPEN |
| `OD-102` | Whether audit-table retention is added to the engine or handled operationally (external pruning) | `12-deployment-and-rollback.md` | OPEN |
| `OD-103` | Whether the smoke-test automatable set (ST-01 … ST-24, ST-27 … ST-30) is wired into CI | `12-deployment-and-rollback.md` | OPEN |
| `ASM-01` | The accuracy claim is simulator-derived only | `13-open-decisions-and-assumptions.md` | ASSUMED |
| `ASM-02` | The replay boundary is a local capture, never a live instance | `13-open-decisions-and-assumptions.md` | ASSUMED |
| `ASM-03` | One engine process per database file | `13-open-decisions-and-assumptions.md` | ASSUMED |
| `ASM-04` | The implementation lands as one reviewed series on `main` | `13-open-decisions-and-assumptions.md` | ASSUMED |
| `ASM-05` | Residual baseline window: `residual_baseline_window_s = 3600.0` (proposed) | `13-open-decisions-and-assumptions.md` | ASSUMED |
| `ASM-06` | Mapping-matrix refresh trigger: `mapping_refresh_interval_s = 3600.0` (proposed), disabled by default | `13-open-decisions-and-assumptions.md` | RESOLVED |
| `ASM-07` | Event-evidence retention horizon: `event_evidence_retention_days = 90` (proposed) | `13-open-decisions-and-assumptions.md` | ASSUMED |
### 8.2 The one retained ID

`01-requirements.md` carries an `OD-09` that is **not** part of any of the
colliding bands in §5.2: it is the decision about the T-catalogue conflict
between `01-…` §4.1 and `11-test-and-validation-plan.md` §5.1, resolved in
favour of the canonical catalogue. It is a distinct question from the `OD-09`–
`OD-15` bands claimed by `09-…`, `10-…` and `12-…`, so it is **retained
unchanged** and is listed in the index above with its own source file.

**Rule.** A retained ID is never renumbered into a sibling's block. If a future
correction pass finds a genuine collision on `OD-09`, it must resolve the
**collision**, not by moving this item — the fix belongs to the `09-`/`10-`/`12-`
band, which §5.2 already renumbers to `OD-74`–`OD-80`, `OD-81`–`OD-87` and
`OD-99`–`OD-103`.

### 8.3 Reading the index

**Rule.** A row's status is the status of the item at the source named in its
**Source file** column. An item marked `OPEN` in a sibling is `OPEN` here; the
sibling's own row carries the full nine-field entry and the routing decision.
**Rule.** `OD-03.9`, `OD-03.12` and `ASM-06` are `RESOLVED`; `OD-33`, `ASM-01`,
`ASM-02`, `ASM-03`, `ASM-04`, `ASM-05` and `ASM-07` are `ASSUMED`; `OD-35` is
`DEFERRED`; every other row is `OPEN`.
**Rule.** The index is a **read-only view**. It restates no decision and closes
none; the authoritative text for every row is the source file it names.

---

## Related documents

- [00 — Project overview](./00-project-overview.md)
- [01 — Requirements](./01-requirements.md)
- [02 — Architecture](./02-architecture.md)
- [03 — Measurement and electrical domain](./03-measurement-and-electrical-domain.md)
- [04 — Measurement topology](./04-topology-spec.md)
- [05 — Residual model spec](./05-residual-model-spec.md)
- [06 — Device and load model spec](./06-device-and-load-model-spec.md)
- [07 — Event and inference spec](./07-event-and-inference-spec.md)
- [08 — Storage and data lifecycle](./08-storage-and-data-lifecycle.md)
- [09 — API and data contracts](./09-api-and-data-contracts.md)
- [10 — UI and user experience spec](./10-ui-and-user-experience-spec.md)
- [11 — Test and validation plan](./11-test-and-validation-plan.md)
- [12 — Deployment and rollback](./12-deployment-and-rollback.md)
- [14 — Requirement traceability](./14-requirement-traceability.md)
- [15 — Implementation plan](./15-implementation-plan.md)
- [Master instructions](../../WATTMOSAIC_MASTER_INSTRUCTIONS.md)
- [Limitations (v2)](../../limitations_v2.md)
- [Confidence model](../../confidence_model.md)
- [Final validation report](../../final_validation_report.md)
- [Data flow](../../data_flow.md)
- [Decision log](../../decisions/README.md)
- [Project status](../../../PROJECT_STATUS.md)
- [API reference](../../api.md)
- [Architecture notes](../../architecture.md)
- CI workflow: `../../../.github/workflows/ci.yml`
- Release workflow: `../../../.github/workflows/release.yaml`
