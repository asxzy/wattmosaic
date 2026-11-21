# E5: monitor companion quantities (V, I, PF), live factor inference and the voltage-sag leg classifier

Status: approved (user, 2026-11-20). It applies on top of E4. It needs a new
ADR-0044, "The engine ingests monitor companion quantities of declared readers".
That ADR amends nothing in ADR-0028: the engine still never reads HA's
registries.

## Why

Measured on the reference install (`docs/specs/topology-principles/README.md`):

- **Factors are invisible to the engine.** HA publishes no factor entity, and the
  engine ingests only power, so a monitor-applied ×2 is invisible live. Yet
  `f = |P| / (V·|I|·|PF|)`, snapped, recovers it exactly.
- **Leg is decided only by voltage sag.** 19/19 120 V readers are called correctly
  on full data, about 70 % within 24 h. A 240 V load sags both legs equally.
- **Current carries a branch prior.** A sustained 5-min current above 12 A never
  occurs on a 120 V branch circuit, which separates feeders and 240 V loads.

## 1. Declaration (no registry reads in the engine)

- An endpoint config revision gains optional `voltage_entity_id`,
  `current_entity_id` and `power_factor_entity_id`, all `str | None`.
  - Add a storage migration `v030_companion_entities.py` that adds the columns
    idempotently with `add_column_if_missing`.
  - Add a repository round-trip.
  - If the revision is a contract model, add the fields with `None` defaults,
    bump `CONTRACT_VERSION` to 4.1.0 and regenerate the schemas.
- The integration's `state_links.py` already resolves same-device entities, so it
  proposes the companion entity ids for a CT reader: same HA device,
  `device_class` voltage / current / power_factor. It sends them with the
  endpoint declaration, through the existing endpoint API (a new optional
  payload field). The pure logic lives in `state_links.py` and has tests.
  Nothing under `engine/` reads the HA registry.
- The replay tool declares them from GT `readers[].factor_inputs` (voltage,
  current and PF, in that order).

## 2. Ingestion

- `ingestion/ha_source.py` subscribes to the declared companion entities. Each
  companion state change updates a per-reader companion record
  `(voltage_v, current_a, power_factor, observed_at)`.
- **Companion entities never become readers and never emit a power reading.**
- The fast path stays O(1), and absent stays `None`.
- `ReaderMeasurement.voltage_rms_v`, `current_rms_a` and `power_factor` are filled
  from the latest companion values that are no more than 30 s older than the
  power sample. Otherwise they are `None`.
- A bounded companion ring per reader (the same 30 s buckets as
  `StructureMemory`, about a week) keeps V and I for the analyses below. Persist
  it the way `StructureMemory` is persisted, or rebuild it from samples.
- Replay: `tools/dev/replay_ground_truth.py` feeds companion samples through the
  same consumer path (`on_sample` with the companion entity id, or a dedicated
  harness hook). It does **not** feed them as readings.

## 3. Live factor inference (`electrical/factor_inference.py`)

- Move the algorithm of `tools/dev/ha_history_pull.py::derive_factors` into the
  engine:
  - align;
  - qualify with |I| ≥ 0.5 A, |PF| ≥ 0.3 and |P| ≥ 50 W;
  - snap to {1, 2} within 15 %;
  - take the hourly mode;
  - require 2 consecutive hours of agreement to change.
  The pull tool then imports it, so there is one implementation.
- A background job, run on `BackgroundExecutor` with a CPU budget and at most
  hourly, writes `MeasurementFactor(applied_by="monitor", declared_by="inferred")`
  intervals for readers with companions:
  - append when the factor changes;
  - never overwrite a user-declared factor;
  - leave readers with no qualifying data unknown, without a row.
- P3, the T5 fold, picks up new rows through its existing row-count invalidation.
- Tests:
  - the move keeps `test_ha_history_pull.py` green;
  - an engine test runs a synthetic ×2 reader through the job and checks that a
    factor row appears and that T5 support follows.

## 4. Voltage-sag leg classifier (`electrical/leg_classifier.py`)

All of this runs on the aligned 30 s companion series, at aligned rebuilds only.

1. **Voltage references.**
   - Group readers whose voltage series correlate at ≥ 0.999 on 1-min means.
     Each group is one physical voltage reference; a monitor has 2–3.
   - Remove the common mode, which is the mean of all references.
   - Cluster the references into two legs L/R by the sign of their residual
     correlation: same leg is positive, cross leg is about −0.9.
   - The labels L/R are arbitrary but stable. Anchor L to the reference that
     sorts first by endpoint id.
2. **Per-reader leg index.**
   - For each step of reader `x` with |ΔP| ≥ 50 W, take the change in mean voltage
     of the L references minus that of the R references across the step, in
     volts per kW: `s = sign(ΔP) · (ΔV_L − ΔV_R) / |ΔP_kW|`.
   - Take the median `s` over n ≥ 5 steps.
   - `s < −0.15` is leg L, `s > +0.15` is leg R.
   - |s| < 0.15 with a common-mode drop (both legs sag below −0.2 V/kW) is
     `both`, a 240 V load.
   - Anything else is unknown.
3. **Uses in T5.**
   - **Cross-leg veto:** a CONTAINS edge between two readers classified on
     different legs (L vs R, neither `both`) is capped at CANDIDATE once both
     calls have n ≥ 10. It is a veto only.
   - A child classified `both` together with an ambiguous pair of sibling
     parents keeps E3's `leg_unidentified`, now with reason `240v` in the
     transition text.
   - A child classified L or R breaks E3 ambiguity in favour of the parent on
     the same leg, when exactly one candidate parent is on that leg.
   - Expose `leg` (`L` / `R` / `both` / `unknown`) and `leg_n` per reader in the
     topology API health/endpoint payload.
4. **Branch-current tag.** If a reader's 5-min mean |I| exceeds 12 A for at least
   one window, tag it `not_branch_circuit`. Expose it in the API. Its only
   behavioural use is as a tie-break: when choosing between two candidate parents
   of equal standing, prefer one tagged `not_branch_circuit`.
5. Tests (synthetic):
   - two references with anti-correlated residuals cluster into L/R;
   - a load that sags L is classified L;
   - an equal sag on both references gives `both`;
   - the cross-leg veto caps an edge;
   - classification needs n ≥ 5;
   - the branch tag fires at more than 12 A.

## 5. Docs

- Add ADR-0044 and its README index line.
- Document the new optional endpoint fields in `docs/configuration.md`.
- Add a `RELEASE_NOTES.md` entry under the Unreleased section (append-only).

## Acceptance

- The full suite, ruff, `check_panel.py`, `check_release.py` and
  `render_validation_docs.py --check` all pass.
- Schemas match after export.
- `tests/integration/test_integration_static.py` passes: no service calls and no
  registry reads under `engine/`.
- `perf_runtime.py --quick` shows no fast-path regression.
