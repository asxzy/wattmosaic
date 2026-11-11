"""Confirm the engine topology from the stored config-entry declaration.

Why this exists
---------------
``POST /topology/confirm`` has always existed on the engine, and the
config-flow wizard calls it.  But the confirmation flag lives in the *config
entry* (``CONF_TOPOLOGY_CONFIRMED``), and nothing could flip it after setup:

* the wizard is the only writer, so a user who never re-ran it — or whose entry
  was created before the confirm step existed — stays ``candidate`` forever;
* the setup-time replay re-applies the *stored* flag, which is ``false``;
* and an unconfirmed topology makes the sample filter reject **every** learning
  sample as ``ambiguous_topology``.  Learning runs, accepts nothing, and the
  ``active_learning`` binary sensor used to report "on".

So the missing piece is not engine capability, it is a *user action*.  This
module implements that action for the ``button`` platform:

* it reuses :func:`~.flow_logic.async_replay_topology`, which itself reuses
  :func:`~.flow_logic.build_topology_update` — the same merge rule as the
  wizard.  There is no second merge implementation here;
* it only ever confirms the declaration the user already stored
  (``CONF_CT_ASSIGNMENTS``).  With no stored declaration it refuses and says so:
  confirming an engine-invented topology would be fabricating user intent;
* it is idempotent: pressing twice on a confirmed engine performs no write.

Pure Python.  No Home Assistant import, so every branch is unit-testable, and
the ``button`` entity in ``button.py`` is thin wiring around it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import CONF_TOPOLOGY_CONFIRMED, CONF_TOPOLOGY_VERSION
from .flow_logic import TopologyPushState, async_replay_topology

#: Engine status the button is offered for.  ``confirmed`` means there is
#: nothing left to do; anything else is not a topology we can confirm.
CANDIDATE_STATUS = "candidate"

#: Why the button is (not) actionable.  Stable keys, used as attributes.
REASON_AVAILABLE = "candidate_topology"
REASON_ALREADY_CONFIRMED = "already_confirmed"
REASON_NO_DECLARATION = "no_declaration"
REASON_ENGINE_UNREACHABLE = "engine_unreachable"


def topology_status(topology: Mapping[str, Any] | None) -> str:
    """The engine's topology status, lower-cased, ``unknown`` when absent."""
    if not isinstance(topology, Mapping):
        return "unknown"
    raw = topology.get("status")
    value = getattr(raw, "value", raw)
    text = str(value or "").strip().lower()
    return text or "unknown"


def declaration_present(assignments: Any) -> bool:
    """Whether the entry stores a CT declaration worth replaying."""
    return isinstance(assignments, Mapping) and bool(assignments)


@dataclass(frozen=True)
class ConfirmAvailability:
    """Whether ``button.wattmosaic_confirm_topology`` should be actionable."""

    available: bool
    reason: str
    status: str

    def as_attributes(self) -> dict[str, Any]:
        return {"topology_status": self.status, "confirm_status": self.reason}


def confirm_availability(
    topology: Mapping[str, Any] | None,
    assignments: Any,
    *,
    engine_available: bool = True,
) -> ConfirmAvailability:
    """Availability rule for the confirm button.

    Offered only while the engine reports ``candidate`` **and** the entry holds
    a declaration.  An unavailable button is better than one that refuses on
    press: ``unavailable`` is what a dashboard shows as "nothing to do here".
    """
    status = topology_status(topology)
    if not engine_available:
        return ConfirmAvailability(False, REASON_ENGINE_UNREACHABLE, status)
    if not declaration_present(assignments):
        return ConfirmAvailability(False, REASON_NO_DECLARATION, status)
    if status == "confirmed":
        return ConfirmAvailability(False, REASON_ALREADY_CONFIRMED, status)
    if status != CANDIDATE_STATUS:
        # ``unknown``/absent: we do not know what we would confirm, so do not
        # offer to confirm it.  The engine being reachable will fix this.
        return ConfirmAvailability(False, REASON_ENGINE_UNREACHABLE, status)
    return ConfirmAvailability(True, REASON_AVAILABLE, status)


@dataclass
class ConfirmResult:
    """Outcome of one button press.  All fields are user-safe."""

    ok: bool
    status: str
    message: str
    #: ``True`` when a write (PUT and/or confirm) was actually attempted.
    attempted: bool = False
    #: Engine topology version after the action, when known.
    version: int | None = None
    #: ``True`` when the entry must record ``CONF_TOPOLOGY_CONFIRMED = True``.
    entry_confirmed: bool = False
    #: Whether the engine is (still) a candidate after the press.
    still_candidate: bool = True

    def as_attributes(self) -> dict[str, Any]:
        return {
            "topology_status": self.status,
            "confirm_attempted": self.attempted,
            "topology_version": self.version,
            "entry_confirmed": self.entry_confirmed,
            "message": self.message,
        }


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def async_confirm_topology(
    client: Any,
    assignments: Mapping[str, Mapping[str, Any]] | None,
    *,
    stored_version: Any = None,
    state: TopologyPushState | None = None,
    observed_version: Any = None,
) -> ConfirmResult:
    """Confirm the stored declaration on the engine.  Never raises.

    Sequence (all inside ``async_replay_topology``, so the merge rule is the
    wizard's):

    1. ``GET /topology`` — nothing to do when it is already ``confirmed``;
    2. ``PUT /topology`` with the stored declaration and ``confirmed=True``;
    3. ``POST /topology/confirm``;
    4. re-read the topology and report the status actually observed.

    Step 4 is why the entry flag is only set on ``entry_confirmed=True``: the
    engine's word, not our request, decides whether learning is unblocked.
    """
    if not declaration_present(assignments):
        return ConfirmResult(
            ok=False,
            status="unknown",
            message=(
                "No stored CT declaration to confirm. Run the WattMosaic setup "
                "wizard and confirm the channel mapping there."
            ),
        )

    try:
        before = await client.topology()
    except Exception:  # noqa: BLE001 - a press must never raise into HA
        return ConfirmResult(
            ok=False,
            status="unknown",
            message="The engine's topology could not be read; try again when it is reachable.",
        )

    status_before = topology_status(before)
    if status_before == "confirmed":
        return ConfirmResult(
            ok=True,
            status="confirmed",
            message="The topology is already confirmed; nothing was written.",
            version=_as_int((before or {}).get("version")),
            entry_confirmed=True,
            still_candidate=False,
        )

    replay_state = state if state is not None else TopologyPushState()
    version = observed_version if observed_version is not None else (before or {}).get("version")
    replay_state = await async_replay_topology(
        client,
        assignments,
        confirmed=True,
        stored_version=stored_version,
        state=replay_state,
        observed_version=version,
    )

    # Read back what the engine actually holds.  A PUT that landed but was not
    # confirmed must not flip the entry flag: the user would then believe
    # learning is enabled while the engine still rejects every sample.
    after: Mapping[str, Any] | None = None
    try:
        after = await client.topology()
    except Exception:  # noqa: BLE001 - report the write, degrade the read
        after = None

    status_after = topology_status(after) if after is not None else "unknown"
    version_after = _as_int((after or {}).get("version")) if after is not None else None
    if version_after is None:
        version_after = replay_state.pushed_version

    if status_after == "confirmed":
        return ConfirmResult(
            ok=True,
            status="confirmed",
            message=(
                "Topology confirmed. Learning will start accepting samples once "
                "evidence arrives."
            ),
            attempted=replay_state.attempts > 0,
            version=version_after,
            entry_confirmed=True,
            still_candidate=False,
        )

    if replay_state.error:
        message = f"The engine did not accept the confirmation: {replay_state.error}"
    elif replay_state.attempts == 0:
        # The replay declined to write (no matching channel, an empty engine
        # topology, ...).  Quote its own reason instead of blaming the engine.
        message = (
            "Nothing was written: the stored declaration could not be applied "
            f"to the engine topology ({replay_state.reason})."
        )
    elif after is None:
        message = (
            "The confirmation was written but the engine could not be re-read; "
            "the next poll will retry."
        )
    else:
        message = (
            "The engine still reports the topology as a candidate. "
            "Check the engine log; the next poll will retry."
        )
    return ConfirmResult(
        ok=False,
        status=status_after,
        message=message,
        attempted=replay_state.attempts > 0,
        version=version_after,
        entry_confirmed=False,
        still_candidate=True,
    )


def confirmed_entry_updates(
    entry_data: Mapping[str, Any] | None,
    entry_options: Mapping[str, Any] | None,
    *,
    version: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(data, options)`` with ``CONF_TOPOLOGY_CONFIRMED = True`` applied.

    The flag lives in ``data`` for a wizard-created entry and in ``options`` for
    one whose settings were last saved from the options flow.  Because the
    coordinator reads ``{**data, **options}``, setting only one container would
    be silently overridden by the other — so the key is written wherever it
    already exists, and into ``data`` when it exists nowhere.
    """
    data = dict(entry_data or {})
    options = dict(entry_options or {})
    in_data = CONF_TOPOLOGY_CONFIRMED in data
    in_options = CONF_TOPOLOGY_CONFIRMED in options
    if not in_data and not in_options:
        in_data = True
    if in_data:
        data[CONF_TOPOLOGY_CONFIRMED] = True
    if in_options:
        options[CONF_TOPOLOGY_CONFIRMED] = True
    parsed = _as_int(version)
    if parsed is not None:
        if in_data:
            data[CONF_TOPOLOGY_VERSION] = parsed
        if in_options:
            options[CONF_TOPOLOGY_VERSION] = parsed
    return data, options


__all__ = [
    "CANDIDATE_STATUS",
    "ConfirmAvailability",
    "ConfirmResult",
    "REASON_ALREADY_CONFIRMED",
    "REASON_AVAILABLE",
    "REASON_ENGINE_UNREACHABLE",
    "REASON_NO_DECLARATION",
    "async_confirm_topology",
    "confirm_availability",
    "confirmed_entry_updates",
    "declaration_present",
    "topology_status",
]
