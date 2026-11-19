# Association and topology (contract 3.0)

Reference for how repeated events turn into **typed topology edges** — the
subject↔endpoint mapping and the electrical containment structure. The
authoritative design is HB §4, §7.2 and §14, with concrete defaults in the
[design spec](../docs/specs/realtime-topology-disaggregation/design.md) and
shapes in the
[interfaces spec](../docs/specs/realtime-topology-disaggregation/interfaces.md).

In 3.0 a CT is a **Measurement Endpoint**, never a device. Unexplained load on
an endpoint is a Reader Residual or an Unknown Load Profile, not a fabricated
device. Discovery no longer mints one device per CT.

---

## 1. Edge kinds that never imply one another

Edges are typed. Keeping them separate is what prevents a correlation from
silently becoming a wiring fact:

| Edge kind | Claim |
| --- | --- |
| subject↔endpoint mapping / association | this subject's power is observable through this endpoint |
| measurement overlap | two endpoints measure overlapping conductors (a duplicate or subset), **not** containment |
| electrical parent/child | a demonstrated wiring containment relation between two endpoints |

Storing them as distinct typed edges means setting a parent pointer cannot
assert membership, and "two endpoints on one appliance" stays an overlap rather
than becoming parenthood. Two legs of one appliance respond identically, and
identical response is **still not containment**.

## 2. Edge lifecycle and user authority

Every inferred edge carries a lifecycle status:

| Status | Meaning |
| --- | --- |
| `CANDIDATE` | a single event's proposal — the furthest one event may promote an edge |
| `PROVISIONAL` | soft; used by inference but not asserted as fact |
| `CONFIRMED` | user-confirmed, authoritative |
| `REJECTED` | user-rejected, never re-promoted by the engine |

Rules that keep the output honest:

- **One event never promotes an edge past `CANDIDATE`.** Repeated, independent
  evidence is required to move beyond that.
- **A parent-only change is never evidence against containment** — a change
  downstream of the parent is expected.
- **PROVISIONAL edges are soft**: they influence inference but do not become
  declarations.
- **Declared wins.** A user-pinned edge outranks an inferred one, and the loser is
  recorded rather than silently dropped.

Per-edge `pin` / `reject` is the user's authority (DEC-10). The engine never
confirms its own output.

## 3. Evidence behind an edge

An association is emitted only after repeated, isolated, in-basis,
in-direction, near-simultaneous corroboration:

- **only isolated events are evidence** — a contaminated event is counted and
  excluded, never deleted or averaged into support;
- **magnitude consistency** — the endpoint delta must agree with the subject's
  step in sign and size; an oversized move is a different load;
- **repeatability** — one coincidence cannot prove a relation, so single-event
  support is capped and labelled;
- **lag and noise** — the response is searched over a bounded lag and must rise
  above the endpoint's measured noise; median/MAD statistics resist outliers;
- **basis** — measurements must be on compatible bases; an unknown basis blocks
  the claim rather than being assumed.

The emitted evidence object carries every number used, plus conflicting
observations with their reasons, so a reader can recompute the confidence
without trusting it. Direction is never silently flipped: a reversed endpoint
step is withheld; declared polarity is applied exactly once upstream.

## 4. Mapping edges and single-subject scope

A mapping edge links a subject (device or component) to an endpoint. Its
`observability` is `DIRECT` only for a confirmed single-subject endpoint — a
smart plug or appliance monitor whose declared scope is exactly one subject.
A circuit CT never gets that scope automatically, and the retired CT-channel
role vocabulary is not reused (see the vocabulary gate in
`tests/unit/test_docs_vocabulary.py`). A circuit feeding one appliance is an
ordinary branch row fitted through normal identifiability rules.

## 5. Self-healing (HB §14)

A configuration change (a plug move, a polarity change, a declaration) produces
a typed `ConfigurationEvent` and a new topology version. The self-healing
workflow reconciles affected estimates against the new version, keeping the
old version readable for audit. Topology, mapping and model versions are
independent: a plug move never touches a power model (AT-15).

---

See [`docs/algorithms.md`](algorithms.md) §5, §11, and the
[handbook §4](../docs/specs/realtime-topology-disaggregation/handbook.md) for the
asymmetric evidence rules and worked containment example.
