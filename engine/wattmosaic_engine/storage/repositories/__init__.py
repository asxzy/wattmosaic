"""Per-aggregate DAOs over the one SQLite connection.

**Why a package.**  ``Database`` used to hold every SQL statement in one
1 500-line class, so every later storage task (S13-S19) would have edited the
same file.  Each aggregate now has its own module, and ``Database`` keeps only
the connection, the lock, the reader seam, migration/backup lifecycle and thin
delegating methods with the old names -- so no caller in Wave 0 changes.

**Extending it without editing this file.**  :class:`Repositories` resolves a
repository from its *module name* on first access:

    db.repos.samples.insert_readings(readings)
    db.repos.entities.endpoint_revision_at(endpoint_id, t)   # added by S13

A later task drops ``repositories/<name>.py`` beside these modules, defines a
:class:`Repository` subclass in it, and reaches it as ``db.repos.<name>``.  This
module never needs an edit, which is what the plan's ownership rule 3 requires
of a package facade.

**Serialization helpers.**  ``iso``, ``enum_value`` and ``as_utc`` are here
rather than in ``database.py`` because every repository needs them; they are
re-exported from ``database`` for the callers that already name that module.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..database import Database

#: Rows per ``fetchmany`` during a bulk sample read.  Large enough that the
#: per-chunk overhead is noise, small enough that the GIL is released often
#: enough for the event loop to keep serving probes (see ``ct_samples_between``).
SAMPLE_FETCH_CHUNK = 4096

#: Public spelling of the chunk size; the underscore-prefixed name is what the
#: original single-module implementation used and is kept importable.
_SAMPLE_FETCH_CHUNK = SAMPLE_FETCH_CHUNK


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def enum_value(value: Any) -> str:
    """Plain string for a contract enum, tolerant of a bare string."""
    return str(getattr(value, "value", value) if value is not None else "unknown")


#: Original private spelling, kept because later tasks and tests import it.
_enum_value = enum_value


def as_utc(value: str) -> datetime:
    """Parse an ISO instant and normalise it to UTC for a storage comparison."""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


#: Original private spelling, kept because later tasks and tests import it.
_as_utc = as_utc


class Repository:
    """Base for one aggregate's DAO.

    The repository holds the ``Database`` rather than its connection: ``reset()``
    rebinds ``Database._conn`` in place, and every engine component keeps a
    reference to the same ``Database`` object.  Reading ``db._conn`` at call time
    (the ``_conn`` property below) is what makes a repository survive a reset.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def _conn(self) -> Any:
        return self.db._conn

    @property
    def _lock(self) -> Any:
        return self.db._lock

    def _reader(self) -> Any:
        return self.db._reader()


class Repositories:
    """Lazy ``db.repos.<module>`` gateway with one instance per module."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[str, Repository] = {}

    def __getattr__(self, name: str) -> Repository:
        if name.startswith("_"):
            raise AttributeError(name)
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        module_name = f"{__name__}.{name}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            raise AttributeError(f"no repository module named {name!r}") from None
        repository = _repository_class(module)
        instance = repository(self._db)
        self._cache[name] = instance
        return instance

    def loaded(self) -> tuple[str, ...]:
        """Names of the repositories instantiated so far, sorted."""
        return tuple(sorted(self._cache))


def _repository_class(module: object) -> type[Repository]:
    """The :class:`Repository` subclass a repository module defines."""
    candidates = [
        attribute
        for attribute in vars(module).values()
        if isinstance(attribute, type)
        and issubclass(attribute, Repository)
        and attribute is not Repository
        and attribute.__module__ == getattr(module, "__name__", "")
    ]
    if len(candidates) != 1:
        raise TypeError(
            f"{getattr(module, '__name__', module)} must define exactly one "
            f"Repository subclass, found {len(candidates)}"
        )
    return candidates[0]


def backup_pattern(db_path: Path) -> str:
    """Glob pattern for the pre-migration backups of ``db_path``."""
    return f"{db_path.name}.pre-v*.bak"


__all__ = [
    "Repositories",
    "Repository",
    "SAMPLE_FETCH_CHUNK",
    "_SAMPLE_FETCH_CHUNK",
    "_as_utc",
    "_enum_value",
    "as_utc",
    "backup_pattern",
    "enum_value",
    "iso",
]
