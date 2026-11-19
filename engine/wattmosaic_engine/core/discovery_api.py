"""Discovery facade: startup endpoint bootstrap and placement discovery.

Owned by D2 after X03 (endpoint discovery)."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any

from ..api.facade import InvalidRequest, maybe_await
from ..api.timeouts import HISTORY_TIMEOUT_BASE_S, HISTORY_TIMEOUT_PER_HOUR_S
from ..disaggregation.auto_placement import (
    devices_for_plan,
    links_for_plan,
    mapping_edges_for_plan,
    reader_roles,
)
from ..ingestion.discovery import (
    devices_for_candidate_endpoints,
    discover_candidate_endpoints,
    links_for_candidate_endpoints,
)

log = logging.getLogger("wattmosaic.main")

#: How long startup waits for the first Home Assistant subscription before
#: running topology discovery.  Bounded so a down Home Assistant cannot delay
#: the API and probes from coming up; 30 s matches ``HASource.wait_connected``.
TOPOLOGY_BOOTSTRAP_WAIT_S = 30.0
#: Minimum gap between discovery retries while none is configured.  Discovery is
#: a full state scan, so retrying every inference cycle (5 s) would be wasteful.
TOPOLOGY_RETRY_INTERVAL_S = 60.0


class DiscoveryApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    def _has_endpoints(self) -> bool:
        try:
            return bool(self.db.repos.entities.endpoints())
        except Exception:  # noqa: BLE001 - a read failure means "unknown", retry later
            return False

    async def bootstrap_topology(self, *, wait_connected_s: float = 0.0) -> dict[str, Any]:
        """Discover candidate measurement endpoints when the database has none.

        A fresh install used to look healthy while doing nothing.  Discovery is
        therefore run once at startup when no :class:`MeasurementEndpoint` is
        stored, and the outcome is logged so the condition is never silent.

        ``hasource.start()`` only *schedules* the connection; the first states
        arrive later from ``_prime()``, which is why ``wait_connected_s`` bounds
        how long startup waits for the first successful subscription.  A zero
        wait is used by the retry path.

        This only ever creates *candidates* through the v3 repositories
        (:meth:`bootstrap_endpoints`): CT endpoints, candidate single-subject
        endpoints with ingestion disabled until confirmed, the Physical Devices
        they imply and their HA power links.  It never confirms a scope, never
        writes a topology edge and never invents device state links.  A failure
        is recorded and logged but must not stop the engine.
        """
        if not self.config.topology_bootstrap:
            return {"skipped": "topology bootstrap disabled by configuration"}
        if self._has_endpoints():
            return {"skipped": "measurement endpoints already configured"}

        if wait_connected_s > 0:
            waiter = getattr(self.hasource, "wait_connected", None)
            if waiter is not None:
                try:
                    await maybe_await(waiter(wait_connected_s))
                except Exception as exc:  # noqa: BLE001 - never block startup
                    log.debug("waiting for Home Assistant failed: %s", exc)

        try:
            states = await self.hasource.state_snapshot()
        except Exception as exc:  # noqa: BLE001 - never block startup
            self.topology_bootstrap_error = f"{type(exc).__name__}: {exc}"
            log.warning("endpoint discovery failed: %s", self.topology_bootstrap_error)
            return {"error": self.topology_bootstrap_error}
        if not states:
            # Not the same condition as "no monitor installed": Home Assistant
            # simply has not answered yet.
            message = (
                "Home Assistant returned no states; endpoint discovery will retry "
                "once the connection is up"
            )
            self.topology_bootstrap_error = message
            log.info("%s", message)
            return {"deferred": message}

        outcome = await self.bootstrap_endpoints()
        if "error" in outcome:
            self.topology_bootstrap_error = str(outcome["error"])
            return outcome
        if not outcome.get("discovered"):
            message = (
                "no energy monitor or metering plug found in Home Assistant; "
                "configure a CT monitor before inference can run"
            )
            self.topology_bootstrap_error = message
            log.warning("%s", message)
            return {"skipped": message, "endpoints": 0}

        self.topology_bootstrap_error = None
        self.db.log_system_event(
            "info",
            "topology",
            "candidate measurement endpoints discovered at startup",
            dict(outcome),
        )
        log.info(
            "discovered %d measurement endpoints (%d CT, %d candidates awaiting "
            "confirmation in the WattMosaic integration)",
            outcome.get("discovered", 0),
            outcome.get("ct", 0),
            outcome.get("candidates", 0),
        )
        return {**outcome, "status": "candidate"}

    async def bootstrap_endpoints(self, *, persist: bool = True) -> dict[str, Any]:
        """Discover Measurement Endpoints and persist them as candidates (D2).

        CT channels become CT endpoints; bare single-load power entities become
        candidate single-subject endpoints whose ingestion is disabled until the
        user confirms the scope (DEC-06).  Physical Devices implied by those
        candidates and their HA power links are created too.  This never mints a
        ``dev_<ct>`` device and never touches an existing endpoint's scope.
        """
        from ..devices.service import DeviceService

        try:
            states = await self.hasource.state_snapshot()
            registries = await self.hasource.registries()
            clock = getattr(self, "_clock", None)
            endpoints = discover_candidate_endpoints(states, registries, clock=clock)
            devices = devices_for_candidate_endpoints(
                endpoints, registries, clock=clock
            )
            links = links_for_candidate_endpoints(endpoints, clock=clock)
        except Exception as exc:  # noqa: BLE001 - discovery must not break startup
            log.warning("endpoint discovery failed: %s", exc)
            return {"error": f"{type(exc).__name__}: {exc}"}

        if persist:
            service = DeviceService(self.db, clock=getattr(self, "_clock", None))
            known = {e.endpoint_id for e in service.list_endpoints()}
            created = 0
            for endpoint in endpoints:
                if endpoint.endpoint_id not in known:
                    service.upsert_endpoint(endpoint)
                    created += 1
            for device in devices:
                if service.get(device.device_id) is None:
                    service.create_device(device)
            for link in links:
                self.db.repos.entities.add_entity_link(link)

        from ..contracts import EndpointKind

        return {
            "discovered": len(endpoints),
            "ct": sum(1 for e in endpoints if e.kind == EndpointKind.CT),
            "candidates": sum(1 for e in endpoints if not e.enabled),
        }

    async def maybe_bootstrap_topology(self) -> dict[str, Any] | None:
        """Retry discovery while none is configured, at most once per interval.

        Startup cannot always win the race against the first Home Assistant
        connection (a slow App start, a Supervisor restart, a Core restart while
        the engine is already running).  Without a retry the engine would sit
        topology-less until the next process restart, which is the same silent
        no-op the defect was about.  This is cheap when a topology exists: it
        returns before doing any work.
        """
        if not self.config.topology_bootstrap:
            return None
        if self._has_endpoints():
            return None
        now = time.monotonic()
        if now - self._last_topology_attempt < TOPOLOGY_RETRY_INTERVAL_S:
            return None
        self._last_topology_attempt = now
        return await self.bootstrap_topology()

    @staticmethod
    def _candidate_state_entities(
        states: Iterable[Mapping[str, Any]],
        registries: Mapping[str, Any] | None,
        declared: Iterable[str] = (),
    ) -> tuple[list[Any], dict[str, str]]:
        """The entities this run may reason about, from states + registries.

        Candidate *population* is deliberately not derived from declarations
        (contract §2): doing that starved auto placement to one device's
        entities.  ``declared`` (entities a person already linked) is only
        subtracted.  Kept as a thin seam so the population rule can be exercised
        without a recorder or a database.
        """
        from ..ingestion.candidate_entities import candidate_state_entities

        return candidate_state_entities(states, registries, declared)

    def _user_linked_entities(self) -> set[str]:
        """HA entities a person (or HA's own device link) already declared.

        Links written by discovery -- including earlier auto placements -- are
        not a user's assertion and must not block a re-run from re-deriving them.
        """
        try:
            rows = self.db.repos.entities.links()
        except Exception:  # noqa: BLE001 - no links table -> nothing declared
            return set()
        return {
            str(row["entity_id"])
            for row in rows
            if str(row.get("declared_by") or "") not in ("discovery",)
            and row.get("valid_until") is None
        }

    async def discover_placements(
        self,
        *,
        lookback_hours: float = 24.0,
        apply: bool | None = None,
        include_groups: bool = True,
        max_candidates: int = 200,
    ) -> dict[str, Any]:
        """Learn which fixtures sit on which CT row from recorded history.

        This is the only facade entry point that creates devices from an
        inference, so every guard that makes that safe is visible here:

        * **opt-in** -- ``config.auto_placement`` (or an explicit ``apply=True``)
          gates the write.  Analysing without writing is always allowed, because
          it changes nothing;
        * **candidate population is structural** -- every ``light``/``switch``/
          ``fan``/``input_boolean`` state is considered, minus entities the user
          already declared and WattMosaic's own output.  Names, areas and
          manufacturers are never used to decide membership (contract §2);
        * **read-only history** -- the recorder is queried, no service is called;
        * **branch rows only** -- a main row refuses
          the placement with a reason instead of double counting the circuit;
        * **user declarations win** -- an entity a person configured is never
          rewritten;
        * **no fabrication** -- a transition that did not move measured power is
          not placed.  Fixtures whose individual wattage is not identifiable are
          reported as an advisory *group*, never as a device (tier B).

        Returns the plan, the refusals, the advisory groups and whether anything
        was applied, so a caller can show the user exactly what was learned and
        what it declined to believe.
        """
        # Group fitting lands in a separate workstream; guard the import so this
        # module stays importable (and testable) before it exists.
        try:
            from ..disaggregation.group_fit import fit_shared_rows
        except ImportError:  # group fitting lands separately
            fit_shared_rows = None

        # Validate here as well as in the transport: the facade is a public entry
        # point (the ``discover_placements`` service command reaches it directly),
        # and an infinite lookback would otherwise overflow when building the
        # window.
        if isinstance(lookback_hours, bool) or not isinstance(lookback_hours, (int, float)):
            raise InvalidRequest("lookback_hours must be a number")
        lookback_hours = float(lookback_hours)
        if not math.isfinite(lookback_hours) or lookback_hours <= 0 or lookback_hours > 2160:
            raise InvalidRequest("lookback_hours must be greater than 0 and at most 2160")
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
            raise InvalidRequest("max_candidates must be an integer")
        if max_candidates < 1:
            raise InvalidRequest("max_candidates must be at least 1")

        endpoints = self._matrix_endpoints()
        topology_version = self._matrix_versions()["topology_version_id"]
        if not endpoints:
            return {
                "ok": False,
                "detail": "no measurement endpoints to place devices on",
                "applied": False,
                "lookback_hours": lookback_hours,
                "topology_version": None,
                "stats": {
                    "candidate_entities": 0,
                    "refused_entities": 0,
                    "branch_rows": 0,
                    "observations": 0,
                    "accepted": 0,
                    "groups": 0,
                    "withheld": 0,
                },
                "accepted": [],
                "refused": {},
                "groups": [],
                "withheld": {},
                "skipped": {},
                "notes": ["no measurement endpoints to place devices on"],
            }

        from ..disaggregation.auto_placement import ROLE_BRANCH, ROLE_MAIN, plan_placements
        from ..ingestion.correlation import (
            correlate,
            estimate_row_noise,
            observations_from_history,
            parse_history_payload,
        )

        states_payload = await maybe_await(self.hasource.state_snapshot())
        registries = await maybe_await(self.hasource.registries())
        user_entities = self._user_linked_entities()
        candidates, candidate_refused = self._candidate_state_entities(
            states_payload or [], registries, user_entities
        )
        found = len(candidates)
        candidates = candidates[:max_candidates]
        entity_ids = [candidate.entity_id for candidate in candidates]

        roles = reader_roles(endpoints)
        by_id = {endpoint.endpoint_id: endpoint for endpoint in endpoints}
        row_channels = {
            endpoint_id: by_id[endpoint_id]
            for endpoint_id, role in roles.items()
            if role == ROLE_BRANCH and by_id[endpoint_id].enabled
        }
        # Contract §4: a group is never emitted for a main row.  A main aggregate
        # contains every downstream fixture, so fitting it would "identify"
        # wattages that were never separately measured -- exactly the fabrication
        # tier B must not do.
        excluded_ct_ids = frozenset(
            endpoint_id for endpoint_id, role in roles.items() if role == ROLE_MAIN
        )
        # A reader a confirmed single-subject declaration owns is that subject's
        # whole measurement; placing another fixture on it would count twice.
        owned_readers = {
            endpoint.endpoint_id: str(endpoint.scope_subject_id)
            for endpoint in endpoints
            if endpoint.scope_subject_id
            and str(getattr(endpoint.scope_status, "value", endpoint.scope_status)) == "confirmed"
        }
        notes: list[str] = []
        stats: dict[str, Any] = {
            "candidate_entities": len(candidates),
            "refused_entities": len(candidate_refused),
            "branch_rows": len(row_channels),
            "observations": 0,
            "accepted": 0,
            "groups": 0,
            "withheld": 0,
        }
        if found > len(candidates):
            # Say it out loud: a truncated run is not "the engine found nothing
            # else", and the omitted entities were never analysed.
            notes.append(
                f"analysed the first {len(candidates)} of {found} candidate entities "
                f"(max_candidates={max_candidates}); increase it to consider the rest"
            )

        from ..entity_evidence import MODE_ONLY_DOMAINS, domain_of
        from ..ingestion.correlation import CANDIDATE_DOMAINS

        def _named_refusals() -> dict[str, str]:
            """Candidate refusals worth naming individually.

            Only domains a user could plausibly have meant as a fixture are
            listed: a whole-house snapshot contains hundreds of sensors, and
            naming each one would bury the interesting refusals.  The count of
            *every* entity looked at and declined stays in
            ``stats.refused_entities`` and is restated in ``notes``, so a refusal
            is never rounded into silence.
            """
            listed = CANDIDATE_DOMAINS | MODE_ONLY_DOMAINS
            return {
                entity_id: reason
                for entity_id, reason in candidate_refused.items()
                if domain_of(entity_id) in listed
            }

        def _short_circuit(reason: str, skipped: dict[str, Any]) -> dict[str, Any]:
            """The frozen response shape with an honest "nothing was analysed"."""
            return {
                "ok": True,
                "applied": False,
                "lookback_hours": lookback_hours,
                "topology_version": topology_version,
                "stats": stats,
                "accepted": [],
                "refused": _named_refusals(),
                "groups": [],
                "withheld": {},
                "skipped": skipped,
                "notes": [reason, *notes],
            }

        if not row_channels or not entity_ids:
            return _short_circuit(
                "no branch CT rows or no candidate state entities",
                {
                    "reason": "no branch CT rows or no candidate state entities",
                    "channels": len(row_channels),
                    "entities": len(entity_ids),
                },
            )

        end = self._clock()
        start = end - timedelta(hours=max(lookback_hours, 0.1))
        wanted = [*entity_ids, *(ch.entity_id for ch in row_channels.values())]
        # The history query is the only command whose cost grows with the window
        # *and* the candidate count, and ``history()`` cannot tell a timeout from
        # an empty recorder -- both come back as ``{}``.  Give it a budget derived
        # from the window it was asked for (a 7-day whole-house request takes 17 s
        # on the reference install against a 30 s default) and refuse rather than
        # report "no recorded history" when the answer is incomplete.
        #
        # That budget is the budget for the *whole* window: ``history()`` splits
        # a window longer than its slice threshold into bounded slices and divides
        # this total across them.  It has to, because one whole-window frame with
        # attributes is 79.6 MB at 48 h and 282.5 MB at 168 h against a 64 MiB
        # websocket frame cap -- the connection was dropped, not answered.  The
        # slicing is a property of the source (it knows the cap); discovery only
        # says how much time the window is worth.
        history_timeout_s = max(HISTORY_TIMEOUT_BASE_S, HISTORY_TIMEOUT_PER_HOUR_S * lookback_hours)
        payload = await self.hasource.history(
            wanted,
            start=start,
            end=end,
            include_attributes=True,
            timeout_s=history_timeout_s,
        )
        if not payload:
            if getattr(self.hasource, "last_history_error", None):
                return _short_circuit(
                    "history query did not complete",
                    {
                        "reason": "history query did not complete",
                        "lookback_hours": lookback_hours,
                        # Names the failed slice and the transport error, e.g.
                        # "slice 3 of 14 [..) failed: TimeoutError: ...".  A
                        # partial answer is never returned as if it were the whole
                        # window, so the only honest report is which part of the
                        # window could not be read.
                        "detail": str(self.hasource.last_history_error),
                    },
                )
            return _short_circuit(
                "no recorded history",
                {"reason": "no recorded history", "lookback_hours": lookback_hours},
            )

        # Everything from here to the result dict is pure computation over the
        # recorder payload: parsing it, pairing transitions with row readings,
        # correlating, planning placements and fitting groups.  It is all
        # synchronous, and at production volume it is expensive -- 72 h of
        # history is ~200k rows and took 25 s before the pairing index, 12 s
        # after it.  Running that on the event loop starved every other
        # request: measured during a 120 h discovery, ``GET /health`` degraded
        # from 0.03 s to 4.1 s and ``GET /healthz`` -- the Supervisor's watchdog
        # probe -- answered 503, which restarts the App mid-analysis.  The
        # gateway's 4 s liveness budget then surfaced as a 504 to the panel.
        # A worker thread keeps the loop free to answer health while it works.
        def _analyse() -> tuple[
            Any,
            Any,
            list[dict[str, Any]],
            dict[str, str],
            Any,
            list[dict[str, Any]],
            dict[str, Any],
            bool,
            Any,
        ]:
            """The CPU-bound half of discovery, run off the event loop."""
            state_history, readings, levels = parse_history_payload(payload)
            # Readings are keyed by entity id in the payload; remap to ct_id so the
            # correlation (and the placement) speak the topology's vocabulary.
            by_entity = {
                str(endpoint.entity_id): endpoint.endpoint_id
                for endpoint in endpoints
                if endpoint.entity_id
            }
            row_readings = {
                by_entity[entity]: rows for entity, rows in readings.items() if entity in by_entity
            }
            row_noise = estimate_row_noise(row_readings)
            observations = observations_from_history(
                entity_states={e: rows for e, rows in state_history.items() if e in entity_ids},
                row_readings=row_readings,
                # Levels are deliberately *not* re-filtered here.  A level is only
                # ever read for an entity that ``entity_states`` yielded, so the
                # filter above already decides which entities can contribute one;
                # a second filter would be unprovable protection against a refactor
                # rather than a guard that any test can distinguish.
                entity_levels=levels,
            )
            stats["observations"] = len(observations)
            report = correlate(observations, now=end, row_noise_w=row_noise)

            # Contract §3.1: a write needs **both** the request's consent and the
            # stored opt-in.  ``apply`` defaults to false, so ``None`` is read-only;
            # an explicit ``apply=True`` cannot override a disabled opt-in, because
            # that switch is the user's standing decision.
            allow_apply = apply is True and bool(self.config.auto_placement)
            labels = {candidate.entity_id: candidate.friendly_name for candidate in candidates}
            plan = plan_placements(
                report=report,
                readers=roles,
                user_entities=user_entities,
                owned_readers=owned_readers,
                labels=labels,
            )

            def _channel_label(ct_id: str) -> tuple[str, str | None]:
                endpoint = by_id.get(ct_id)
                if endpoint is None:
                    return ct_id, None
                return endpoint.display_name or ct_id, endpoint.panel_id

            groups: list[dict[str, Any]] = []
            withheld: dict[str, str] = {}
            # Entities tier A is about to place.  Computed before the group fit so the
            # advisory pass can exclude them: the same fixture must not appear with
            # both a measured wattage (Placed) and a fitted one (Group).
            placed_entity_ids = {placement.entity_id for placement in plan.accepted}
            if include_groups and fit_shared_rows is not None:
                # Contract §4 forbids a group on a main row, and
                # keeps those rows out of the fit entirely: a main aggregate contains
                # every downstream fixture, so fitting it would "identify" wattages
                # that were never separately measured.  The exclusion is applied
                # here, to the *inputs*, so the call itself stays exactly the frozen
                # §4 signature (no extra keyword for the other workstream to honour).
                #
                # CT rows are filtered to *numeric* states first.  A recorder really
                # does return ``unavailable`` for a CT row (a Refoss bridge restart
                # during the 7-day window on the reference install does exactly
                # that), and a non-numeric state is not a measurement -- the same
                # rule ``parse_history_payload`` already applies when building
                # ``readings`` above, which is why correlation never saw this.  The
                # fit's numeric parser raises ``ValueError`` on such a row instead of
                # skipping it, and that only became reachable once 168 h started
                # returning at all: the old code dropped the connection first.  The
                # real fix belongs in ``group_fit``'s row parser; filtering here keeps
                # discovery from handing a wattage fit a row that has no wattage.
                fitted = fit_shared_rows(
                    row_readings={
                        endpoint.endpoint_id: [
                            dict(row)
                            for row in payload.get(endpoint.entity_id, ())
                            if isinstance(row, Mapping) and _is_numeric_state(row)
                        ]
                        for endpoint in row_channels.values()
                        if endpoint.endpoint_id not in excluded_ct_ids and endpoint.entity_id
                    },
                    entity_states={
                        entity: [dict(row) for row in rows if isinstance(row, Mapping)]
                        for entity, rows in payload.items()
                        if entity in entity_ids
                    },
                    entity_levels={
                        entity: {stamp: float(fraction) for stamp, fraction in rows}
                        for entity, rows in levels.items()
                        if entity in entity_ids
                    },
                    row_noise_w=row_noise,
                )
                for group in fitted:
                    ct_label, panel_name = _channel_label(group.ct_id)
                    # A fixture that tier A placed already owns a device with a
                    # measured wattage.  Leaving it in a group as well would report
                    # the same watts twice (once measured, once fitted) and would
                    # make the advisory card contradict the Placed card.  The
                    # aggregate fit is the weaker claim, so it yields.
                    members = [
                        {
                            "entity_id": member.entity_id,
                            "name": labels.get(member.entity_id, member.entity_id),
                            "estimated_w": member.estimated_w,
                        }
                        for member in group.members
                        if member.entity_id not in placed_entity_ids
                    ]
                    if not members:
                        continue
                    kept = [m for m in group.members if m.entity_id not in placed_entity_ids]
                    for member in kept:
                        # A single-fixture group is *identified* by the aggregate
                        # fit, so "it co-toggles with another fixture" would be
                        # false.  The honest reason is that the aggregate fit found
                        # it while transition-pair correlation did not: that is a
                        # weaker claim, so it still does not become a device.
                        #
                        # The test is the *original* group size, not the surviving
                        # members: a two-fixture group whose other member was placed
                        # solo still co-toggles, and saying otherwise would invent a
                        # reason the evidence does not support.
                        withheld[member.entity_id] = (
                            f"identified_by_aggregate_fit_on_{group.ct_id}"
                            if len(group.members) == 1
                            else f"co_toggles_with_another_fixture_on_{group.ct_id}"
                        )
                    groups.append(
                        {
                            "ct_id": group.ct_id,
                            "ct_label": ct_label,
                            "panel_name": panel_name,
                            "total_w": group.total_w,
                            "r2": group.r2,
                            "r2_null_p95": group.r2_null_p95,
                            "r2_margin": group.r2_margin,
                            # Truthful, not a constant: ``GroupFit`` derives this from
                            # whether any member carries a coefficient the fit
                            # identified.  Measured on the production install at 72 h
                            # of history, 16 of 20 groups have a single member and 14
                            # of those 20 carry an identified coefficient, so they
                            # *do* have a measured wattage; reporting them all as "not
                            # individually identifiable" would understate evidence
                            # the engine actually holds, and would make the UI warn
                            # about numbers that are sound.
                            "individually_identifiable": group.individually_identifiable,
                            "members": members,
                            "evidence": list(group.evidence),
                        }
                    )
            elif include_groups:
                notes.append(
                    "group fitting is not installed; fixtures that co-toggle cannot be "
                    "reported even as advisory groups"
                )

            accepted: list[dict[str, Any]] = []
            for placement in plan.accepted:
                ct_id = placement.reader_id
                entity_id = placement.entity_id
                ct_label, panel_name = _channel_label(ct_id)
                candidate = next(
                    (c for c in report.candidates if c.entity_id == entity_id), None
                )
                accepted.append(
                    {
                        "device_id": placement.device_id,
                        "entity_id": entity_id,
                        "name": labels.get(entity_id, entity_id),
                        "ct_id": ct_id,
                        "ct_label": ct_label,
                        "panel_name": panel_name,
                        "on_w": (
                            getattr(candidate, "typical_on_w", None)
                            if candidate is not None
                            else None
                        )
                        or placement.on_w,
                        "off_w": getattr(candidate, "typical_off_w", None)
                        if candidate is not None
                        else None,
                        "symmetry": getattr(candidate, "symmetry", None),
                        "agreement": getattr(candidate, "agreement", None),
                        "corroborations": getattr(candidate, "corroborations", None),
                        "specificity_z": getattr(candidate, "specificity_z", None),
                        "dimmable": bool(placement.dimming_curve),
                        "evidence": list(placement.evidence),
                        "applied": False,
                    }
                )

            stats["accepted"] = len(accepted)
            stats["groups"] = len(groups)
            stats["withheld"] = len(withheld)

            # Candidate-level refusals ("this entity can never prove runtime") and
            # placement-level refusals ("this evidence may not become a device") are
            # both reported.
            refused: dict[str, Any] = dict(plan.refused)
            for entity_id, reason in _named_refusals().items():
                refused.setdefault(entity_id, reason)
            accepted_ids = {item["entity_id"] for item in accepted}
            withheld = {
                key: value
                for key, value in withheld.items()
                if key not in refused and key not in accepted_ids
            }
            stats["withheld"] = len(withheld)
            return (
                report,
                plan,
                groups,
                withheld,
                row_noise,
                accepted,
                refused,
                allow_apply,
            )

        (
            report,
            plan,
            groups,
            withheld,
            row_noise,
            accepted,
            refused,
            allow_apply,
        ) = await asyncio.to_thread(_analyse)
        stats["withheld"] = len(withheld)

        notes.extend(
            f"{entity_id}: {reason}" for entity_id, reason in report.unresolved.items()
        )
        if stats["refused_entities"]:
            notes.append(
                f"{stats['refused_entities']} state entities are not candidates "
                "(a mode entity, or a domain whose state does not prove runtime)"
            )

        result: dict[str, Any] = {
            "ok": True,
            "applied": False,
            "lookback_hours": lookback_hours,
            "topology_version": topology_version,
            "stats": stats,
            "accepted": accepted,
            "refused": refused,
            "groups": groups,
            "withheld": withheld,
            "skipped": dict(report.skipped),
            # Additive to the frozen shape: kept from the pre-§3.3 payload so a
            # caller that already explained "why was this not placed?" keeps
            # working.  ``notes`` repeats them in human-readable form.
            "unresolved": dict(report.unresolved),
            "row_noise_w": row_noise,
            "notes": notes,
        }
        if not plan.accepted:
            return result
        if not allow_apply:
            if not self.config.auto_placement:
                result["detail"] = (
                    "auto placement is disabled: turn on the 'auto_placement' "
                    "App option (Add-on → Configuration) and send apply=true "
                    "to write these placements"
                )
            else:
                result["detail"] = (
                    "apply is false; pass apply=true to write these placements"
                )
            return result

        # Contract 3.0: an applied placement writes v3 rows only -- a Physical
        # Device per fixture and a discovered ``load_state`` link from its entity.
        now = self._clock()
        for device in devices_for_plan(plan, created_at=now):
            if self.db.repos.entities.get_device(device.device_id) is None:
                self.db.repos.entities.upsert_device(device)
        for link in links_for_plan(plan, valid_from=now):
            self.db.repos.entities.add_entity_link(link)
        # The placement's reader becomes a CANDIDATE mapping edge (S15).  The
        # version service owns the append-only write and the version bump.
        versions = getattr(self, "version_service", None)
        if versions is not None:
            # Auto placements are replaced wholesale on each run.  Keep every
            # declared/user edge from the active version, drop only the previous
            # auto-placement edges, then append this run's proposals.
            active_id = versions.mapping_version_id()
            persisted: list[Any] = []
            if active_id:
                from ..versioning.service import _map_edge_contract

                persisted = [
                    edge
                    for edge in (
                        _map_edge_contract(row)
                        for row in self.db.repos.mapping.mapping_edges(active_id)
                    )
                    if getattr(edge, "declared_by", None) != "auto_placement"
                ]
            edges = (*persisted, *mapping_edges_for_plan(plan, valid_from=now))
            if edges:
                version_id = versions.propose_mapping(edges, None)
                versions.promote_mapping(version_id, "auto placement")
                result["mapping_version_id"] = version_id
        self.db.log_system_event(
            "info",
            "configuration",
            f"auto placement created {len(plan.accepted)} device(s)",
        )
        # ``applied`` is per accepted item and only true once the write landed.
        for item in result["accepted"]:
            item["applied"] = True
        result["applied"] = True
        return result


def _is_numeric_state(row: Mapping[str, Any]) -> bool:
    """True when a recorder row's state is a number, i.e. an actual measurement.

    A CT row legitimately reports ``unavailable`` while its bridge restarts
    (observed on the reference install inside a 7-day window).  That row carries
    no watts, and the group fit's row parser raises on it rather than skipping
    it.  ``parse_history_payload`` already drops such rows when building
    ``readings``, so this keeps the fit's input consistent with the evidence the
    correlation used instead of inventing a second interpretation.
    """
    raw = row.get("s", row.get("state"))
    if raw is None or isinstance(raw, bool):
        return False
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return False
    return math.isfinite(value)
