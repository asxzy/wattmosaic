# Raw CT capture is a debug option, HA's recorder is the production history

**Status:** Implemented · 2026-11-19 · on branch `feat/raw-capture`; see `verification.md`
**Plan branch:** `worktree-raw-retention-plan` (based on `origin/main` @ `29ad782`)
**Addresses:** `docs/specs/realtime-topology-disaggregation/TODO.md` §1 (disk use on Pi-class hosts)
**Amends:** DEC-30 (raw-sample clause) through a new ADR-0042 (task T07)

## What this is

The engine persists every canonical CT reading into `ct_samples`. In the full-scale
benchmark that is **≈ 0.71 GB/day of the ≈ 0.81 GB/day total growth** (~274 B/sample,
40 readers × 1 Hz), about 5 GB steady state at the 7-day default.

The investigation behind this plan established:

1. **No production path reads `ct_samples`.** The engine writes it at
   `core/runtime.py:234` (enqueued at `:682`). Detection, evidence windows and quality
   read in-memory rings (`realtime/reader_state.py:685`), restart recovery reloads a
   reader-state blob (`put_reader_state`/`get_reader_state`), and no API endpoint, panel
   view or learning path reads the table. The repository read methods
   (`readings_between`, `ct_samples_between`, `latest_ct_sample(s)`, `latest_reading`,
   `sample_exists`) have no engine callers.
2. **Every horizon longer than 10 days consumes derived tables.** Identifiability
   (90 d) reads `event_groups`; residual priors (7 d) read event steps; promotion needs
   5 independent events from evidence; profile retirement and match expiry (30 d) read
   derived records.
3. **HA's recorder already holds the same stream.** All CT readings arrive as HA
   `state_changed` events (`ingestion/ha_source.py:865`). HA's default
   `purge_keep_days` is 10. Polarity and multiplier are re-derivable from the
   topology/mapping versions, which are kept forever.
4. Raw readings are therefore useful for **development debugging and fast replay
   only**. For production debugging, HA's 10-day history is sufficient, with three
   accepted caveats (see `design.md` §5).

## The change in one paragraph

Add `raw_capture_days` (`WM_RAW_CAPTURE_DAYS`, App option `raw_capture_days`,
range 0–90, **default 0**). At 0 the engine does not persist CT readings at all. At N > 0
it persists them and keeps N days. `ct_samples` gets its own `capture` retention tier
and is deleted only by a batched background job, never by the on-loop `db.prune` or by
the evidence prune. Existing installs drain their old rows in the background. The replay
tool gains an HA-recorder source and a recorder-coverage probe, so production issues can
be replayed without any engine-side raw store.

## How to start

Read in this order:

1. `design.md`: behaviour, acceptance criteria, non-goals and caveats
2. `interfaces.md`: exact names, signatures, semantics (the shared contract between tasks)
3. `implementation-plan.md`: waves, DAG, file ownership, agent protocol, gates
4. `tasks/T01.md` … `tasks/T10.md`: one self-contained brief per coding agent

## Waves at a glance

| Wave | Tasks (run in parallel) | Needs |
|---|---|---|
| 1 | T01 settings · T02 prune tier · T03 batched purge · T04 HA-history replay · T05 recorder probe · T06 App option · T07 docs + ADR | nothing |
| 2 | T08 runtime wiring · T09 diagnostics surface | Wave 1 merged (T08: T01–T03; T09: T01) |
| 3 | T10 verification, parity tests, perf numbers | Waves 1–2 merged |
