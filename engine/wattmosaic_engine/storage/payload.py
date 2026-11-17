"""Payload encoding at rest: transparent zlib for JSON columns (`REQ-STORAGE-008`).

**Why this exists.**  ``topology_versions.payload_json`` stores a whole
topology snapshot as JSON text and is rewritten on every topology change; the
reference install's newest snapshot is 37 302 bytes and compresses to 3 151
bytes (zlib, 8.4 % of the original).  Nothing else in the schema is both large
and read back through a single API: ``device_events.attributes_json`` is a
handful of scalars, and the evidence columns are per-record blobs that are
small individually.  The task asks for compression "where it pays", so the
codec covers exactly the columns named here and nothing else.

**Why it is reversible without rewriting history.**  A stored value is either

* a *legacy* JSON document -- it starts with ``{``, ``[``, ``"``, a digit or
  ``n``/``t``/``f`` (the first byte of any JSON value), or
* an *encoded* document -- it starts with ``Z1:`` followed by base64 zlib data.

The prefix is a marker, not a separate column, so an older binary reads an
encoded row as opaque text only if it does not know the codec, and a current
binary reads a legacy row unchanged.  No historical row is rewritten by the
migration or by a read; encoding happens on write.

:func:`encode_payload` and :func:`decode_payload` are the whole contract.
:func:`encode_if_worthwhile` is what a writer calls: it encodes only when the
compressed form is smaller, so a payload that does not pay stays in the legacy
form and is still readable.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from typing import Any

#: Marker on an encoded payload.  Long enough and unusual enough that no JSON
#: document can begin with it.
ENCODED_PREFIX = "Z1:"

#: Encode only when this fraction of the original size is saved.  Anything
#: below 1.0 would store a larger value than it replaces.
_MIN_SAVING_FRACTION = 0.9


def encode_payload(raw: str) -> str:
    """``raw`` JSON text -> ``Z1:<base64 zlib>``.  Always encoded."""
    compressed = zlib.compress(raw.encode("utf-8"))
    return ENCODED_PREFIX + base64.b64encode(compressed).decode("ascii")


def decode_payload(stored: str | None) -> Any:
    """Decode a stored payload; **the same call reads both forms**.

    Returns the parsed JSON value.  A legacy (uncompressed) document is parsed
    directly; an encoded one is inflated first.  ``None`` and empty text return
    ``None``, matching the tolerant reader contract already used for these
    columns (a corrupt cell must not abort a load).
    """
    if stored is None or stored == "":
        return None
    if not stored.startswith(ENCODED_PREFIX):
        try:
            return json.loads(stored)
        except (TypeError, ValueError):
            return None
    try:
        inflated = zlib.decompress(base64.b64decode(stored[len(ENCODED_PREFIX):]))
        return json.loads(inflated.decode("utf-8"))
    except (ValueError, zlib.error, binascii.Error, UnicodeDecodeError):
        # An encoded cell that does not inflate is reported as absent rather
        # than raising: one damaged row must not make a whole configuration set
        # unreadable.  This cannot happen to a legacy row, which never carries
        # the prefix.
        return None


def payload_is_encoded(stored: str | None) -> bool:
    """Whether a stored value carries the encoding marker."""
    return stored is not None and stored.startswith(ENCODED_PREFIX)


def encode_if_worthwhile(raw: str) -> tuple[str, bool]:
    """Encode ``raw`` only when compression actually pays.

    Returns ``(stored, encoded)``.  A payload whose compressed form is not
    smaller than the original (short documents, already-compact ones) is stored
    verbatim and reported as not encoded, so the on-disk size never grows and
    the row stays readable by any reader.
    """
    encoded = encode_payload(raw)
    if len(encoded) < len(raw) * _MIN_SAVING_FRACTION:
        return encoded, True
    return raw, False


def decode_json_text(stored: str | None) -> str | None:
    """The stored value as JSON **text**, decoding when needed.

    For a caller that needs the text (a re-encode, a hash, a diff) rather than
    the parsed object.
    """
    if stored is None:
        return None
    if not payload_is_encoded(stored):
        return stored
    value = decode_payload(stored)
    if value is None:
        return None
    return json.dumps(value)


__all__ = [
    "ENCODED_PREFIX",
    "decode_json_text",
    "decode_payload",
    "encode_if_worthwhile",
    "encode_payload",
    "payload_is_encoded",
]