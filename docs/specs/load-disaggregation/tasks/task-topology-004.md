# TASK-TOPOLOGY-004 — Incomplete topology and the circuit scope

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-TOPOLOGY-004`
**Title.** Incomplete topology and the circuit scope
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-TOPO-010`, `REQ-TOPO-011`

**Description.**
Let the topology graph be incomplete without failing, and give a circuit its
own identity, class, status and membership — including the honest statement of
what it cannot explain.

**Expected output.**
- An unplaced device still loads and publishes as unknown.
- `GET /circuits` returns the identifier, class, status, reader set and device set.
- A `PARTIALLY_UNDERSTOOD` circuit states its unattributed load.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/disaggregation/circuits.py` | `CircuitClassifier`, `Circuit` |
| `engine/wattmosaic_engine/api/server.py` | the `GET /circuits` route |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The containment evidence | `TASK-TOPOLOGY-002` | the evidence that places a device |
| `Circuit` and `CircuitClass` | `contracts.py` | reused, extended additively |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| `GET /circuits` | the panel `#/topology` | additive route with the contract header |
| The circuit status | `addon/ui/app.js` | `PARTIALLY_UNDERSTOOD` states its unattributed load |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-TOPOLOGY-002` | the containment evidence that places a device |

**Acceptance criteria.**
- [ ] An unplaced device still loads and publishes as unknown.
- [ ] `GET /circuits` returns the identifier, class, status, reader set and device set.
- [ ] A `PARTIALLY_UNDERSTOOD` circuit states its unattributed load.
- [ ] An incomplete topology is a valid state, not an error.
- [ ] The route returns the contract header and `400` on a validation failure.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-topology-bootstrap::test_incomplete_topology_still_loads` (new) | `pytest tests/unit` | the load succeeds |
| `UT-circuits::test_circuit_class_status_and_member_sets` (new) | `pytest tests/unit` | the five fields are present |

**Builder assignment.**
Unassigned. Wave 2, segment 2.3. Write owner of `api/server.py` for wave 2.

**Blockers.**
- None.
