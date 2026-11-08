# Risk Register

| ID | Risk | Likelihood | Impact | Mitigation | Status |
| --- | --- | --- | --- | --- | --- |
| R1 | Panel A / panel B / feed CT pair nesting is unconfirmed. Summing them would double count. | High | Critical | Separate measurement groups (ADR-0009); no cross-group summation; candidate topology requires confirmation. | MITIGATED, OPEN |
| R2 | Room-level `6.42 W` sensors look like template duplicates of one value. | High | Medium | Excluded from `direct_measured`; flagged in audit duplicate report. | MITIGATED |
| R3 | No hardware measurement timestamp from many CT integrations; `last_updated` is state update time. | Certain | High | Three-timestamp model; staleness from `state_timestamp`; repeated-value flag. | MITIGATED |
| R4 | Typical CT cadence is 15.7 s median, up to >30 s. Event windows must match. | Certain | High | Adaptive windows from measured cadence; no 2 s assumption anywhere. | MITIGATED |
| R5 | Double-pole pairing inferred from naming only. | Medium | High | `pair_confidence=name_derived`; pairing is configurable; single logical device. | OPEN |
| R6 | Ambiguous concurrent events could produce false attribution. | High | Critical | Identifiability analysis; explicit ambiguity diagnostics; unknown over fabrication. | MITIGATED |
| R7 | Channels hidden by the user (`hidden_by`) still update. | Certain | Low | Included in audit; hidden flag preserved; not silently dropped. | MITIGATED |
| R8 | SQLite write amplification from 36 channels at 15 s + 240 energy entities. | Medium | Medium | Only subscribed power/energy entities; batch inserts; WAL; retention pruning. | MITIGATED |
| R9 | Engine unreachable → HA entities go unavailable. | Medium | Medium | Explicit staleness thresholds; last-good timestamps; clear UI status. | MITIGATED |
| R10 | Model overfitting on small sample counts. | Medium | High | Validation hold-out, promotion margin, minimum sample count, manual review path. | MITIGATED |
| R11 | User confirms a wrong topology, leading to confident wrong estimates. | Medium | High | Topology versioning, diagnostics, visible candidate status, easy rollback. | OPEN |
| R12 | Production HA writes without authorization. | Low | Critical | Structural: no service-call code path; install deferred to user. | CONTROLLED |
| R13 | Token leakage via logs/diagnostics/exports. | Low | Critical | Redaction in config/logs/diagnostics; `.env` gitignored; constant-time token compare. | MITIGATED |

## Residual risk statement

The largest remaining risk is **electrical topology uncertainty** (R1, R5, R11).
No amount of algorithmic sophistication compensates for a wrong containment
model. The system therefore refuses to assume nesting and reports residual power
rather than redistributing it.
