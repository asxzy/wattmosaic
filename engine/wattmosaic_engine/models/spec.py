"""Versioned, lossless codec for the v3 power-model spec union (L1).

The storage layer persists a :data:`~wattmosaic_engine.contracts.PowerModelSpec`
as a JSON blob in ``power_model_versions_v3.spec_json``.  This module is the
single place that knows how to turn that blob back into a typed spec and how to
write it out again.  It is deliberately strict about the schema version: a blob
written by a *newer* codec is never silently decoded with defaults, because
that would be how DEF-02 (a model that reloads with the wrong parameters or
predicts 0 W) creeps back in.

Every spec member is a Pydantic model with a ``kind`` discriminator, so a
round-trip is exactly lossless::

    decode(encode(spec)) == spec

and, crucially, the predictions derived from the spec are identical after the
round-trip (the property test in ``tests/unit/test_model_codec.py``).

This module is pure: no DB access, no wall clock, no runtime fakes.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, TypeAdapter

from ..contracts import (
    SPEC_SCHEMA_VERSION,
    PowerModelSpec,
    PowerModelVersion,
)
from .serde import SchemaVersionError

_SPEC_ADAPTER: TypeAdapter[PowerModelSpec] = TypeAdapter(PowerModelSpec)

#: Exposed so callers do not need to know the alias lives here.
SPEC_SCHEMA_VERSION = SPEC_SCHEMA_VERSION

__all__ = [
    "SPEC_SCHEMA_VERSION",
    "SchemaVersionError",
    "encode_spec",
    "decode_spec",
    "version_from_row_v3",
]


def encode_spec(spec: PowerModelSpec) -> dict[str, Any]:
    """Serialise a spec to a plain, JSON-safe dict.

    ``sort_keys`` is applied by :func:`json.dumps` at the storage boundary; here
    we keep insertion order so the dict is easy to read in tests and diagnostics.
    Every value is produced by Pydantic's ``mode='json'`` dump, so the output is
    directly serialisable with the standard library.
    """
    return _SPEC_ADAPTER.dump_python(spec, mode="json")


def decode_spec(raw: Any, schema_version: int | None = None) -> PowerModelSpec:
    """Decode a stored spec, validating both the discriminator and the schema.

    ``raw`` may be a JSON string, a mapping, or an already-typed spec.  A spec
    written with a schema newer than this codec raises :class:`SchemaVersionError`
    rather than being silently downgraded: an unknown field must never be
    reinterpreted as a default (HB §16, DEF-02).
    """
    if isinstance(raw, BaseModel):
        spec = _SPEC_ADAPTER.validate_python(raw.model_dump())
    else:
        payload = raw
        if isinstance(raw, (str, bytes, bytearray)):
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("spec_json is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("spec must be a JSON object")
        spec = _SPEC_ADAPTER.validate_python(payload)

    if schema_version is not None:
        version = int(schema_version)
        if version > SPEC_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"power-model spec schema {version} is newer than this codec "
                f"({SPEC_SCHEMA_VERSION}); refusing to decode with silent defaults"
            )
    return spec


def version_from_row_v3(row: Any) -> PowerModelVersion | None:
    """Rebuild a :class:`PowerModelVersion` from a v3 repository row.

    The repository has already decoded the spec via :func:`decode_spec` (so a
    legacy ``kind='legacy'`` row never reaches this function); this helper
    handles the remaining JSON columns (``evidence_refs_json``) and validates
    the typed contract.  Corrupt rows return ``None`` rather than raising into
    the inference loop, preserving the legacy serde's tolerance contract.
    """
    from ..contracts import EvidenceRef

    if row is None:
        return None
    data = dict(row)
    try:
        schema_version = int(data.get("schema_version", SPEC_SCHEMA_VERSION))
        spec_raw = data.pop("spec_json", None)
        if spec_raw is not None:
            data["spec"] = decode_spec(spec_raw, schema_version)
        elif "spec" not in data:
            raise ValueError("row has no spec")
        refs_raw = data.pop("evidence_refs_json", None)
        if refs_raw is not None:
            if isinstance(refs_raw, str):
                refs_raw = json.loads(refs_raw)
            data["evidence_refs"] = [EvidenceRef.model_validate(ref) for ref in refs_raw]
        # A legacy row uses a spec this codec cannot interpret; skip it rather
        # than fabricate a prediction (DEC-31).
        if data.get("basis") == "LEGACY_ABSOLUTE":
            return None
        return PowerModelVersion.model_validate(data)
    except (TypeError, ValueError, KeyError):
        return None
