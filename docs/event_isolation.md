# Event isolation (contract 3.0)

Reference for how the engine answers "when this subject changed, was anything
else changing in the same window?" before it attributes any power. The normative
definition is HB §7.4 and the
[design spec](../docs/specs/realtime-topology-disaggregation/design.md); the
contract is the `EventIsolation` verdict in `contracts/`.

The question is not "did an event happen?" but "can the measured change be
attributed to one physical device?". A blended score cannot answer that, so the
output is a named verdict with the window that was examined and the evidence
that decided it.

---

## 1. Two windows, deliberately separate

| Window | Default | Claim |
| --- | --- | --- |
| isolation window | `30 s` | tight: this device's own transition is the only physical device change near the event |
| power window | `60 s` | wide: the endpoint delta attributed to the event was not polluted by a later sibling step |

The power window is never shorter than the isolation window (a wider window that
excluded part of the tight one would hide a change the tight check already saw).
The `analysis_offset_s` shifts the **window**, never the event's `occurred_at`,
so the physical event timestamp survives analysis unchanged.

Resolution is three-layer: a per-device override beats a device-class default,
which beats the global setting. Only timing keys may be overridden; a typo is
reported rather than silently changing an unrelated timing.

## 2. Verdicts and the decision rule

| Verdict | Meaning |
| --- | --- |
| `ISOLATED` | nothing else changed in the isolation window and the window was well observed |
| `CONTAMINATED` | another physical device changed nearby (or the power window was polluted by a later step) |
| `INSUFFICIENT_DATA` | the window was not observed well enough to say |

Two rules make the verdict usable:

- **Contamination is positive evidence** and is checked before coverage. Seeing
  a sibling move needs no endpoint samples to believe.
- **Absence is not clean.** Missing or insufficient coverage yields
  `INSUFFICIENT_DATA`, never an all-clear. A thin window is not evidence of
  isolation.

## 3. Physical devices, not entity identities

Events are collapsed to one per **physical device** via typed HA entity links.
Three entities of one appliance firing together are one competitor, not three;
a second entity of the same device can never masquerade as a competitor. An
entity with no declared role is still treated as load evidence by default — the
absence of a binding is not proof an entity only reports reachability.

A competitor on a different, disjoint endpoint may be recorded as
`ct_separated` for the learning guard, but it does **not** change the verdict:
the verdict is about time overlap between physical devices, not arithmetic.

## 4. Late arrivals and retention

A late competing device that falls inside the examined window can reclassify an
`ISOLATED` verdict to `CONTAMINATED` with `power_window_contaminated=True`;
`occurred_at` is copied, never recomputed. Nothing is deleted: a contaminated
event stays as a real observation, it simply cannot support a clean attribution.
Verdicts are persisted so provenance references still resolve after a restart.

## 5. Boundaries worth stating

- Isolation adds metadata; it never moves event history.
- `INSUFFICIENT_DATA` is the default, not the exception, and is preserved.
- Contamination is never decomposed by guessing a share; joint events are left
  to group inference, which refuses to divide non-identifiable members.

See [`docs/algorithms.md`](algorithms.md) §4–5 and the
[handbook §7](../docs/specs/realtime-topology-disaggregation/handbook.md) for the
event window and matching rules.
