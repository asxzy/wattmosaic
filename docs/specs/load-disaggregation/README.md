# Circuit-aware load disaggregation — specification set

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION** — approved by the orchestrator's
review gate. Every requirement in this folder is an implementation commitment
from this commit onward.

This folder specifies the next generation of WattMosaic's load disaggregation:
a system that understands a home's **circuits**, not just its meters, and that
treats uncertainty, partial observability and configuration change as
first-class facts rather than as defects to be smoothed away.

The target system is **not** a rewrite. The repository already owns topology
inference, residual modelling, device power models, event correlation, the
confidence model and the identifiability analysis. This specification is a set
of **additive, contract-versioned deltas** on top of that engine
(`docs/implementation_plan_v2.md` §0: "extend, do not redesign").

---

## 1. Scope of this specification

| In scope | Out of scope |
| --- | --- |
| The observable behaviour of a circuit-aware disaggregation system | Any implementation, code change or migration |
| Requirements stated against outcomes a reader can observe | Deployment, packaging and release |
| The architecture that carries those requirements | Files `03`–`15` of this folder (authored in parallel by other agents) and the two forthcoming delivery appendices (`16`, `17`) |
| The gap between today's shipped engine and that target | Changes to `contracts.py`, `storage/`, `api/`, the panel or the HA integration |

**Change policy.** Every requirement in this set is additive and optional with
respect to the existing contract, so a `2.0.0` reader keeps validating
(`docs/implementation_plan_v2.md` §0, "the 1.5.0 compatibility rule"; the
current version is `2.0.0`, `engine/wattmosaic_engine/contracts.py:32`).

**Governing constraints.** No new runtime dependency, no new database, no new
service, no message bus, no distributed infrastructure
(`docs/implementation_plan_v2.md` §0). One file has one owner per wave
(`docs/implementation_plan_v2.md` §5).

---

## 2. File index

| File | Purpose |
| --- | --- |
| [`00-project-overview.md`](00-project-overview.md) | Goals, scope, non-goals, the interfaces to preserve, the core architecture, engineering principles, dependencies and major technical risks. |
| [`01-requirements.md`](01-requirements.md) | Every requirement with a unique ID, description, priority, rationale, acceptance criteria, dependencies and verification method. |
| [`02-architecture.md`](02-architecture.md) | System context, module boundaries, data flow, event flow, dependency relationships, online versus batch processing, failure isolation, extension points and integration with the existing interfaces. |
| [`03-measurement-and-electrical-domain.md`](03-measurement-and-electrical-domain.md) | What the system can observe, in what units, with what semantics, and what it cannot observe at all. |
| [`04-topology-spec.md`](04-topology-spec.md) | The two topologies, the typed relationship set, the edge lifecycle, topology versioning and change detection. |
| [`05-residual-model-spec.md`](05-residual-model-spec.md) | The per-Reader residual model: decomposition, baseline and drift, unknown-event detection and the immutable evidence record. |
| [`06-device-and-load-model-spec.md`](06-device-and-load-model-spec.md) | The five device entities, the power-model classes, the power levels and the learned-model lifecycle. |
| [`07-event-and-inference-spec.md`](07-event-and-inference-spec.md) | Event production and ingestion, correlation and isolation, simultaneity, replay and the identifiability solve. |
| [`08-storage-and-data-lifecycle.md`](08-storage-and-data-lifecycle.md) | The four logical tiers, retention, aggregation, indexing, replay and recovery over the existing single SQLite database. |
| [`09-api-and-data-contracts.md`](09-api-and-data-contracts.md) | The normative names, shapes, units, nullability, versions and error semantics that cross a module boundary. |
| [`10-ui-and-user-experience-spec.md`](10-ui-and-user-experience-spec.md) | The panel capabilities the disaggregation engine must be reachable through, and the labelling rules they carry. |
| [`11-test-and-validation-plan.md`](11-test-and-validation-plan.md) | The test layers, the measured suite baseline, the acceptance thresholds and the verification vehicles. |
| [`12-deployment-and-rollback.md`](12-deployment-and-rollback.md) | How the capability is built, shipped, backed up, smoke-tested, rolled back and monitored. |
| [`13-open-decisions-and-assumptions.md`](13-open-decisions-and-assumptions.md) | Everything the set did not decide, plus the V1 defaults the implementation proceeds on. |
| [`14-requirement-traceability.md`](14-requirement-traceability.md) | Every requirement mapped to an architecture component, a task, a proposed code location, a test and a verification result. |
| [`15-implementation-plan.md`](15-implementation-plan.md) | The 36 tasks in 5 dependency waves, the ownership matrix, the contract-freeze list and the acceptance gates. |
| [`phase0-repository-and-environment-discovery.md`](phase0-repository-and-environment-discovery.md) | The repository architecture summary and implementation constraints the set was written from. |

`16-final-verification-report.md` and `17-deployment-report.md` are the
forthcoming delivery and acceptance appendices. They do not exist yet, so they
are named here rather than linked.

The sibling files are listed above and linked from `00` and `01`; the links are
relative and resolve to the files in this folder.

---

## 3. Reading order

1. **`00-project-overview.md`** — why the system exists, what it will not do,
   and which existing interfaces are frozen.
2. **`01-requirements.md`** — the normative content. Every requirement is
   independently verifiable.
3. **`02-architecture.md`** — how the requirements are carried by the existing
   module layout, and what is new versus reused.
4. The sibling files listed in §2, for measurement, topology, residual, device
   models, events, storage, API, UI, testing, deployment, decisions,
   traceability and rollout detail.

A reader who only wants the product shape should read `00` §1–§4. A reader who
is about to build should read `01` and `02` in that order.

---

## 4. Conventions this specification follows

**Status header.** Every file opens with a one-line status and purpose line, in
the style of `docs/specs/retire-dedicated-ct-concept/README.md:3`. Every file
of the set carries the token `SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION` on that
first status line — all 55 files, the 18 in this folder and the 37 under
`tasks/`. A file that drops the token is a defect the `spec_status` gate in
`tools/dev/verify.py` names and fails on. The token never replaces the
purpose text: the status line carries both, in that order.

**Evidence.** Every factual claim about the *existing* system cites a
`path:line` or names the existing document it comes from. A claim about the
*target* system is a requirement, and is written as a requirement.

**Existing vocabulary.** Requirements are written against the repository's own
type names — `Measurement`, `CTChannel`, `Topology`, `TopologyRelation`,
`Circuit`, `ResidualPower`, `ConfidenceVector`, `ConfidenceTier`, `Source`,
`QualityStatus`, `RelationKind`, `IsolationStatus`, `CircuitClass`,
`MeasurementBasis`, `MonitoringLevel`, `ModelType`, `ModelStatus`, `Phase`,
`Polarity`, `CTType`, `InferenceResult`, `InferenceRecord`, `Reconciliation`,
`MeasurementFactor`, `HaEntityBinding`, `EventIsolation` — all in
`engine/wattmosaic_engine/contracts.py`. No type is invented in this set; a
target-side artefact that does not exist yet is described in prose and marked
as new.

**Gap identifiers.** Existing gaps are cited as `G1`–`G20` from
`docs/assessment_gap_analysis.md` §11. A gap found while writing this
specification that is not in that list takes a `G21+` identifier and says so —
the source register for the `G21`–`G32` band is `00-project-overview.md` §13.1.
A gap found by a sibling specification takes a **locally-scoped** identifier
instead, and is valid only together with its entry in the canonical
reconciliation register at `00-project-overview.md` §13.2, which is the register
of record and the single source of truth for gap identifiers. Each sibling's own
gap table is an upstream source register for the gaps it found; §13.2 is the
register of record for every gap identifier.

**Requirement identifiers.** `REQ-<AREA>-<NNN>`, numbered sequentially within
each area. The areas are fixed by the master instruction:
`FOUNDATION`, `MEASURE`, `TOPO`, `RESIDUAL`, `DISAGG`, `POWER`, `STORAGE`,
`EVENT`, `LEARNING`, `CONFIDENCE`, `MAPPING`, `CHANGE`, `UI`, `DEPLOY`, `TEST`.

**Priority vocabulary.** `MUST` / `SHOULD` / `MAY`.

**Acceptance criteria.** Observable and testable: a named artefact, a named API
response field, a named test file, or a named UI element. A requirement whose
acceptance cannot be observed is not a requirement.

**Verification method.** Names the concrete vehicle: a `pytest` path, a
`tools/dev/verify.py` gate, a `tools/simulator` scenario, a `tools/audit`
capture replay, a static panel gate, or a manual deployment check.

**Measured versus estimated.** The words "measured", "estimated", "allocated"
and "unknown" are used strictly. An inferred or modelled value is never
described as measured.

**Diagrams.** Mermaid, for system context, module dependency and sequence
views.

**Hard rules.** Stated as a bold one-liner under a `**Rule.**` label, in the
style of `docs/electrical_model.md` and `docs/residual_analysis.md`.

**Related documents.** Every file ends with a short list of relative links to
its siblings and to the existing `docs/` files it relied on.

---

## 5. Non-negotiable constraints this specification inherits

These are stated once here and are quoted by the requirements. They are the
repository's own, and breaking one is a defect.

- The signed residual is the only subtraction; no `max(0.0, …)`, no `abs()`,
  no epsilon clamp (`docs/residual_analysis.md` §1).
- A residual is never redistributed onto devices (`docs/residual_analysis.md` §9).
- Missing is never zero (`docs/electrical_model.md` §8; `docs/limitations_v2.md`
  §10.6).
- A candidate is a proposal: `requires_confirmation` is always true,
  `MAX_CONFIDENCE = 0.99` (`docs/association_and_topology.md` §5;
  `engine/wattmosaic_engine/disaggregation/association.py:144`).
- Confidence is never blended into one score and never presented as a
  probability (`docs/confidence_model.md` §1, §6).
- Correlation is not topography, and a bigger reading is not upstream
  (`docs/association_and_topology.md` §5).
- No automatic promotion of a candidate topology (`docs/architecture.md` §8).
- The engine never reads the HA entity/device registry; the integration
  resolves links and writes entity ids into `DeviceTopology.metadata`
  (`docs/architecture.md` §2.1).
- The engine never calls HA services; the integration never computes power
  (`docs/architecture.md` §1, §6).
- No new runtime dependency, no new database, no new service, no message bus
  (`docs/implementation_plan_v2.md` §0).
- Negative verification: every guard is proved by removing it and watching the
  test fail (`tools/dev/negverify_nesting.py`).

---

## 6. Related documents

- [`00-project-overview.md`](00-project-overview.md) — scope, principles, risks.
- [`01-requirements.md`](01-requirements.md) — the normative requirement set.
- [`02-architecture.md`](02-architecture.md) — the architecture that carries them.
- [`../retire-dedicated-ct-concept/README.md`](../retire-dedicated-ct-concept/README.md)
  — the house style for a specification folder.
- `docs/assessment_gap_analysis.md` — the `G1`–`G20` gap list and the
  "must NOT be rebuilt" list.
- `docs/implementation_plan_v2.md` — the governing decisions and acceptance gates.
- `docs/interface_contracts.md` — the frozen engine↔HA and engine↔integration
  contracts, and the change protocol.
- `docs/architecture.md` — the shipped module boundaries and the inference cycle.
- `docs/electrical_model.md` — the measurement model, double-counting rule,
  solving and identifiability.
- `docs/residual_analysis.md` — the signed residual, baseline versus dynamic.
- `docs/confidence_model.md` — the confidence dimensions and why there is no
  aggregate.
- `docs/association_and_topology.md` — the four relation kinds and the eight
  evidence rules.
- `docs/limitations_v2.md` — what the shipped system does **not** do.
- `docs/test_scenario_matrix.md` — the existing `A1`–`J9` scenario matrix.
