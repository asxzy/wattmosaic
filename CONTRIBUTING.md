# Contributing to WattMosaic

## Ground rules

1. **Never actuate a household device during development.** No service calls, no
   switching appliances to generate test data. Use the simulator or passive
   observation.
2. **Never commit secrets.** `.env` is gitignored; only `.env.example` with
   placeholders belongs in the repository.
3. **The contracts are the contract.** `engine/wattmosaic_engine/contracts.py` is
   the single source of truth for every cross-module schema.
4. **Do not fabricate attribution.** If a change makes the system report a
   device power it cannot support with evidence, it is a regression, even if
   accuracy metrics look better.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## Repository layout

```
engine/wattmosaic_engine/   independent inference engine
custom_components/wattmosaic/  Home Assistant integration (thin client)
addon/                      Home Assistant OS App packaging
tools/                      audit, simulator, dev utilities
tests/unit|integration|simulation|end_to_end
docs/                       architecture, contracts, guides, ADRs
data/schemas/               generated from contracts — do not edit by hand
```

## Changing a contract

1. Edit `contracts.py`.
2. Bump `CONTRACT_VERSION` (semantic: breaking → major).
3. Regenerate schemas:
   ```bash
   python tools/dev/export_schemas.py
   ```
4. Update `docs/interface_contracts.md`.
5. Update every affected implementation and test.
6. Run the full suite.

A contract change that breaks the integration without a version bump will be
rejected in review.

## Code standards

- Python 3.11+; type annotations on public functions.
- `ruff check` must pass; `mypy engine` should stay clean.
- No blocking I/O in async code paths.
- Every network call has a timeout.
- Every exception that crosses a component boundary is logged with context and
  converted into a degraded state, never a crash loop.
- Sorting: new modules are added to the dependency direction documented in
  `docs/architecture.md`; no upward imports.

## Tests

| Layer | Location | Rule |
| --- | --- | --- |
| Unit | `tests/unit` | Fast, deterministic, no network, no HA |
| Integration | `tests/integration` | Cross-module, still no real HA |
| Simulation | `tests/simulation` | Ground-truth accuracy, seeded |
| End-to-end | `tests/end_to_end` | Lifecycle and recovery |

When you fix a bug, add a regression test that fails before the fix. Do not
weaken an assertion to make a test pass; fix the code or explain in the PR why
the assertion was wrong.

## Adding a device model type

1. Add the type to `ModelType` in `contracts.py` (contract change).
2. Implement `fit`, `predict`, `validate` in `engine/wattmosaic_engine/models/`.
3. Register it in the model registry's type selection.
4. Add fitting, prediction and rejection tests.
5. Document when the type is appropriate in `docs/learning.md`.

## Electrical correctness checklist

Before merging anything touching `electrical/` or `disaggregation/`, confirm:

- [ ] Nested CTs are not double counted.
- [ ] Double-pole loads are one logical device.
- [ ] Negative power (export) is preserved.
- [ ] Residual is reported, not distributed.
- [ ] Ambiguous concurrent events produce ambiguity, not attribution.
- [ ] Direct measurements are never replaced by estimates.
- [ ] Stale and repeated readings are excluded from learning evidence.

## Security checklist

- [ ] No token in logs, diagnostics, exports or error strings.
- [ ] New endpoints require `X-API-Token` when configured.
- [ ] New configuration is documented in `docs/configuration.md`.
- [ ] `python tools/dev/check_secrets.py` passes.

## Commit and review

- One logical change per commit, with a message explaining *why*.
- Reference the relevant ADR or risk ID when the change touches topology or
  attribution semantics.
- The maintainer runs the full suite independently before merging; a green
  local run is necessary but not sufficient.

## Reporting a bug

Include: engine version, contract version, `/api/v1/diagnostics` output
(redacted), the relevant device's `explanation`, and whether the issue reproduces
with the simulator. Never include `.env` or tokens.
