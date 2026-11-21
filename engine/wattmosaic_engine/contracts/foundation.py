"""Foundation contracts (owned by K00).

Version-id aliases and helpers, typed evidence references, provenance/status
enums, justified ``Bounds``, the v3 seven-component ``ConfidenceVector`` and a
small null-policy gate used by the reflective tests.

It also owns the single Pydantic base :class:`Model` (``extra="forbid"``,
``use_enum_values=True``) required by the interfaces spec, the house clock
helper :func:`utcnow` and ``CONTRACT_VERSION``.  The seven-component confidence
vector is spelled :class:`ConfidenceVectorV3` (the contract-2 six-dimension
vector was removed at 3.0).

Null policy (HB §18): an absent measurement or estimate is ``None``, never a
zero.  Every field here that represents a score or a bound defaults to ``None``.
"""

from __future__ import annotations

import hashlib
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Contract version.  Canonical source for the cutover (DEC-01).  The
#: integration mirrors this value in ``custom_components/wattmosaic/const.py``;
#: a major mismatch is refused by the integration's ``_check_contract``.
#: 4.0.0 splits the topology state enum from the mapping lifecycle enum.
#: 4.1.0 adds optional companion-quantity entity ids to endpoint config revisions
#: (ADR-0044): voltage, current and power-factor entities declared by the
#: integration, never read from HA's registries by the engine.  Additive change.
CONTRACT_VERSION = "4.1.0"

__all__: list[str] = [
    # Base, clock and version
    "CONTRACT_VERSION",
    "Model",
    "utcnow",
    # Version-id types and helpers
    "TopologyVersionId",
    "MappingVersionId",
    "ModelVersionId",
    "ModelSetId",
    "SnapshotId",
    "topology_version_id",
    "parse_version_number",
    "model_set_id",
    # Version triple and evidence/provenance
    "VersionTriple",
    "EvidenceRef",
    "EvidenceKind",
    "EvidenceRefParseResult",
    "parse_evidence_ref",
    # Status enums
    "EstimateStatus",
    "IdentifiabilityStatus",
    "InferenceMethod",
    # Bounds
    "BoundsBasis",
    "Bounds",
    # Confidence
    "ConfidenceVectorV3",
    # Null policy
    "NULL_SUFFIXES",
    "NULL_FIELD_NAMES",
    "NULL_EXEMPT_FIELD_NAMES",
    "is_null_sensitive_field_name",
    "iter_null_sensitive_fields",
    "audit_model_null_policy",
    "audit_models_null_policy",
]



def utcnow() -> datetime:
    """The house clock: an aware UTC ``datetime``."""
    return datetime.now(UTC)


class Model(BaseModel):
    """The single Pydantic base of every contract model."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True, frozen=False)


def _document(enum_cls: type[Enum], /, **docs: str) -> None:
    """Attach a docstring to each enum member.

    ``str, Enum`` keeps the class docstring on every *member* as well (a string
    literal after an assignment is not retained at runtime), so without this the
    per-member prose would exist only in the source.
    """
    for name, value in docs.items():
        enum_cls[name].__doc__ = value


# --------------------------------------------------------------------------- #
# Version-id types (DEC-09): human-readable, monotonically numbered ids.
# --------------------------------------------------------------------------- #

TopologyVersionId = str  # "topo-<n>"
MappingVersionId = str  # "map-<n>"
ModelVersionId = str  # "mdl-<subject_id>-<n>"
ModelSetId = str  # "mset-<sha256[:12]>" of sorted active ModelVersionIds
SnapshotId = str  # "snap-<ulid>"

# Ids are opaque strings.  The numbered id families carry a trailing decimal
# companion number (DEC-09's ``*_number: int``); a snapshot id carries a ULID and
# has no number.  ``fullmatch`` keeps "topo-1junk" from parsing as 1.
_NUMBERED_PREFIXES = ("topo-", "map-", "mdl-")
_NUMBERED_ID_RE = re.compile(r"^(?:topo|map)-\d+$|^mdl-.+-\d+$")


def _version_number(version_id: str) -> int:
    """Companion number of a numbered v3 version id (the trailing decimal)."""
    text = str(version_id)
    if not text.startswith(_NUMBERED_PREFIXES) or _NUMBERED_ID_RE.fullmatch(text) is None:
        raise ValueError(f"not a numbered v3 version id: {version_id!r}")
    return int(text.rsplit("-", 1)[1])


def topology_version_id(n: int) -> TopologyVersionId:
    """Build a topology version id from its monotone number (``topo-<n>``)."""
    number = int(n)
    if number < 0:
        raise ValueError("topology version number must be non-negative")
    return f"topo-{number}"


def parse_version_number(version_id: str) -> int:
    """Return the monotone companion number embedded in a v3 version id."""
    return _version_number(version_id)


def model_set_id(version_ids: Any) -> ModelSetId:
    """Deterministic id of a set of active model version ids.

    The ids are deduplicated and sorted, so the result is independent of the
    input order.  Only the hash prefix is exposed (``mset-<sha256[:12]>``).
    """
    ids = list(version_ids)
    if not all(isinstance(v, str) for v in ids):
        raise TypeError("model_set_id expects an iterable of string version ids")
    canonical = "\0".join(sorted(set(ids)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"mset-{digest[:12]}"


# --------------------------------------------------------------------------- #
# Version triple and typed evidence references.
# --------------------------------------------------------------------------- #


class VersionTriple(Model):
    """The independent topology/mapping/model version triple (rule 20)."""

    topology_version_id: TopologyVersionId
    mapping_version_id: MappingVersionId
    model_set_id: ModelSetId


class EvidenceKind(str, Enum):
    """The closed set of things an :class:`EvidenceRef` can resolve to (§1)."""

    SAMPLE = "sample"
    EVENT_EVIDENCE = "event_evidence"
    EVENT_GROUP = "event_group"
    HA_EVENT = "ha_event"
    EDGE = "edge"
    MAPPING_EDGE = "mapping_edge"
    MODEL_VERSION = "model_version"
    CONFIGURATION_EVENT = "configuration_event"
    OBSERVATION = "observation"


class EvidenceRef(Model):
    """A typed, resolvable reference to a piece of evidence (D4's resolver)."""

    kind: Literal[
        "sample",
        "event_evidence",
        "event_group",
        "ha_event",
        "edge",
        "mapping_edge",
        "model_version",
        "configuration_event",
        "observation",
    ]
    id: str


class EvidenceRefParseResult(Model):
    """The result of parsing a textual evidence reference (``kind:id``)."""

    kind: EvidenceKind
    id: str


def parse_evidence_ref(text: str) -> EvidenceRefParseResult:
    """Parse a ``"kind:id"`` reference, rejecting unknown kinds/empty ids."""
    raw = str(text)
    if ":" not in raw:
        raise ValueError(f"evidence reference must be 'kind:id': {text!r}")
    kind, _, identifier = raw.partition(":")
    if kind not in {member.value for member in EvidenceKind}:
        raise ValueError(f"unknown evidence kind: {kind!r}")
    if not identifier:
        raise ValueError(f"evidence reference has an empty id: {text!r}")
    return EvidenceRefParseResult(kind=EvidenceKind(kind), id=identifier)


# --------------------------------------------------------------------------- #
# Status enums.
# --------------------------------------------------------------------------- #


class EstimateStatus(str, Enum):
    """What kind of value an estimate is (HB §8.5)."""

    MEASURED = "measured"
    ESTIMATED = "estimated"
    ALLOCATED = "allocated"
    UNKNOWN = "unknown"


class IdentifiabilityStatus(str, Enum):
    """Identifiability of an estimate (HB §8.4)."""

    IDENTIFIABLE = "identifiable"
    PARTIALLY_IDENTIFIABLE = "partially_identifiable"
    GROUP_IDENTIFIABLE = "group_identifiable"
    NON_IDENTIFIABLE = "non_identifiable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class InferenceMethod(str, Enum):
    """How an estimate was obtained."""

    DIRECT_READING = "direct_reading"
    JOINT_DELTA = "joint_delta"
    JOINT_ABSOLUTE = "joint_absolute"
    MODEL_PRIOR = "model_prior"
    DECLARED = "declared"
    NONE = "none"


# --------------------------------------------------------------------------- #
# Justified bounds.
# --------------------------------------------------------------------------- #


class BoundsBasis(str, Enum):
    """What justifies a :class:`Bounds` interval (§1's ``basis`` vocabulary)."""

    NON_NEGATIVITY = "non_negativity"
    GROUP_SUM = "group_sum"
    READER_CEILING = "reader_ceiling"
    POSTERIOR = "posterior"
    DECLARED_CEILING = "declared_ceiling"


# A context-local gate, set only by the private ``Bounds._point`` constructor.
# It is deliberately not a model field, so a zero-width interval can never be
# introduced through a payload, JSON or ``model_validate`` -- only the trusted
# Python constructor can open it, and the flag is reset before construction ends.
_BOUNDS_ALLOW_POINT: ContextVar[bool] = ContextVar("_bounds_allow_point", default=False)


class Bounds(Model):
    """A justified interval for an estimate.

    ``lower_w`` and ``upper_w`` are nullable (HB §18).  When both are present
    the validator requires ``lower_w <= upper_w``.  A zero-width interval
    (a point) is only possible through the private :meth:`_point` constructor,
    which represents an IDENTIFIABLE or MEASURED result; the ``allow_point``
    flag is *not* a field and never appears in the model or its JSON schema.
    """

    lower_w: float | None = Field(default=None)
    upper_w: float | None = Field(default=None)
    basis: Literal[
        "non_negativity",
        "group_sum",
        "reader_ceiling",
        "posterior",
        "declared_ceiling",
    ] | None = None

    def __init__(self, **data: Any) -> None:
        token = _BOUNDS_ALLOW_POINT.set(bool(data.pop("allow_point", False)))
        try:
            super().__init__(**data)
        finally:
            _BOUNDS_ALLOW_POINT.reset(token)

    @model_validator(mode="after")
    def _validate_interval(self) -> Bounds:
        lower, upper = self.lower_w, self.upper_w
        if lower is not None and upper is not None:
            if lower > upper:
                raise ValueError("lower_w must not exceed upper_w")
            if lower == upper and not _BOUNDS_ALLOW_POINT.get():
                raise ValueError("a zero-width interval requires the private allow_point path")
        return self

    @classmethod
    def _point(
        cls,
        value: float,
        basis: Literal[
            "non_negativity",
            "group_sum",
            "reader_ceiling",
            "posterior",
            "declared_ceiling",
        ]
        | None = None,
    ) -> Bounds:
        """Private constructor for a point interval (IDENTIFIABLE/MEASURED).

        I5 (and only I5) calls this for an estimate that is known exactly; the
        ``allow_point`` flag is consumed by ``__init__`` and is never a field,
        so it cannot be set through a payload.
        """
        return cls(lower_w=value, upper_w=value, basis=basis, allow_point=True)


# --------------------------------------------------------------------------- #
# Confidence: seven scoped components, never blended (DEC-24, ADR-0007).
# --------------------------------------------------------------------------- #


class ConfidenceVectorV3(Model):
    """Seven-component confidence vector for a v3 estimate.

    Each component is an *uncalibrated engineering score* in ``[0, 1]`` or
    ``None`` when not assessed.  **This is not a probability** (ADR-0007): the
    numbers are not calibrated, do not sum to one, and must not be interpreted
    as likelihoods.  There is deliberately **no blended scalar** field, no
    ``__float__`` and no arithmetic -- a consumer must name the component it
    means (DEC-24; HB §9.6).
    """

    measurement_quality: float | None = Field(default=None, ge=0.0, le=1.0)
    event_association: float | None = Field(default=None, ge=0.0, le=1.0)
    model_fit: float | None = Field(default=None, ge=0.0, le=1.0)
    topology_consistency: float | None = Field(default=None, ge=0.0, le=1.0)
    identifiability: float | None = Field(default=None, ge=0.0, le=1.0)
    sample_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    freshness: float | None = Field(default=None, ge=0.0, le=1.0)


# --------------------------------------------------------------------------- #
# Null-policy gate (HB §18).
# --------------------------------------------------------------------------- #

# Field-name suffixes whose numeric value may never default to a non-None
# number: watts, kilowatt-hours, watt-hours, amperes, volt-amperes-reactive.
NULL_SUFFIXES: tuple[str, ...] = ("_w", "_kwh", "_wh", "_a", "_var")
# Plus the un-suffixed confidence fields.
NULL_FIELD_NAMES: frozenset[str] = frozenset({"confidence"})


#: Dimensionless evidence *weights* are not measurements (HB §18): a zero
#: weight means "no recent verdict", never an absent 0 W reading.  They are
#: therefore exempt from the ``_w`` null-policy rule.
NULL_EXEMPT_FIELD_NAMES: frozenset[str] = frozenset({"support_w", "conflict_w"})


def is_null_sensitive_field_name(name: str) -> bool:
    """Whether ``name`` is subject to the null policy (HB §18)."""
    return (
        name not in NULL_EXEMPT_FIELD_NAMES
        and (name in NULL_FIELD_NAMES or name.endswith(NULL_SUFFIXES))
    )


def iter_null_sensitive_fields(model: type[Any]) -> list[tuple[str, Any]]:
    """Return ``(name, field_info)`` for a Pydantic model's null-sensitive fields."""
    fields = getattr(model, "model_fields", None)
    if fields is None:
        return []
    return [
        (name, info)
        for name, info in fields.items()
        if is_null_sensitive_field_name(name)
    ]


def audit_model_null_policy(model: type[Any]) -> list[str]:
    """List null-sensitive fields of ``model`` with a non-None numeric default.

    A numeric default other than ``None`` (including ``0`` / ``0.0``) on a
    measurement or estimate field violates HB §18.  The empty list means the
    model is compliant.
    """
    violations: list[str] = []
    for name, info in iter_null_sensitive_fields(model):
        default = info.default
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            violations.append(f"{model.__name__}.{name}={default!r}")
        elif callable(getattr(info, "get_default", None)):
            try:
                resolved = info.get_default(call_default_factory=False)
            except Exception:  # pragma: no cover - defensive for unusual defaults
                resolved = None
            if isinstance(resolved, (int, float)) and not isinstance(resolved, bool):
                violations.append(f"{model.__name__}.{name}={resolved!r}")
    return violations


def audit_models_null_policy(models: Any) -> dict[str, list[str]]:
    """Map model name -> null-policy violations for every model in ``models``."""
    return {
        model.__name__: found
        for model in models
        if (found := audit_model_null_policy(model))
    }


# --------------------------------------------------------------------------- #
# Generated JSON-schema exports (see tools/dev/export_schemas.py).
# --------------------------------------------------------------------------- #

SCHEMA_EXPORTS: list[tuple[str, type]] = [
    ("version_triple", VersionTriple),
    ("evidence_ref", EvidenceRef),
    ("bounds", Bounds),
    ("confidence_vector_v3", ConfidenceVectorV3),
]
