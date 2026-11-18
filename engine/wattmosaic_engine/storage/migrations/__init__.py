"""Versioned SQLite schema migrations, one module per version from 13 on.

**Layout.**  Released migrations 1-11 live in :mod:`.legacy`, moved there
verbatim.  Every later version is its own module named ``vNNN_<slug>.py`` beside
this file, so Wave-2 tasks (migration 13, 14, ... 19) each own one whole file and
never touch a shared list:

.. code-block:: text

    storage/migrations/
        __init__.py          this facade: discovery, apply, LATEST_VERSION
        legacy.py            versions 1-11, verbatim
        v013_entities.py     MIGRATION + TIERS
        ...

**Module contract.**  A discovered module exports two names:

``MIGRATION: Migration``
    the migration itself.  ``MIGRATION.version`` must equal the ``NNN`` in the
    file name -- a mismatch is refused rather than trusted, because a file whose
    name and body disagree is a version nobody can reason about.

``TIERS: dict[str, Tier]``
    the storage tier of every table the migration creates (see
    ``storage/tiers.py``).  An empty dict is allowed for a data-only migration.

**Ordering and refusals.**  Modules are ordered by ``NNN``.  A duplicate number
raises at import, whether the duplicate is another discovered module or one of
the released 1-11.  ``v012_*`` is refused outright: ``PAYLOAD_ENCODING_VERSION``
is 12 and is deliberately *not* a migration (the encoding is a write-path marker
with no DDL behind it), so a module numbered 12 would make "schema 12" mean two
different things.

**Atomicity.**  Each migration runs in ``BEGIN IMMEDIATE`` with
``PRAGMA user_version`` set *inside* the transaction, so an interrupted
migration rolls back to the previous version instead of leaving a half-applied
schema.  Nothing here may run a statement that auto-commits (``VACUUM``); such a
statement would end the transaction early and defeat that guarantee.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: The four storage tiers (``docs/specs/load-disaggregation/08-storage...`` §2).
#: Declared here so this package stays importable on its own; ``storage/tiers.py``
#: publishes the same tuple as ``TIERS`` and validates migrations against it.
Tier = Literal["L1", "L2", "L3", "L4"]
TIER_NAMES: tuple[Tier, ...] = ("L1", "L2", "L3", "L4")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...] = ()
    #: Optional step run after ``statements`` inside the same transaction.
    #: Needed only where the SQL is not idempotent on its own -- SQLite has no
    #: ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, and a migration must still
    #: no-op on a database that already carries the column (a database created
    #: by a build whose ``CREATE TABLE`` included it, or a migration that was
    #: interrupted after the ``ALTER`` but before ``user_version`` advanced).
    apply: Callable[[sqlite3.Connection], None] | None = None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table``; empty when the table does not exist."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    """``ALTER TABLE ... ADD COLUMN`` only when the column is absent.

    ``ALTER TABLE ADD COLUMN`` in SQLite is metadata-only: it does not rewrite
    the table, so this is cheap on a production database with ~12k rows in
    ``reconciliation_results``.  Existing rows get NULL.
    """
    if column in table_columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


#: ``vNNN_<slug>``: three digits, underscore, a lower-case slug.
_MODULE_PATTERN = re.compile(r"^v(\d{3})_([a-z0-9_]+)$")

#: Numbers that are deliberately *not* schema versions produced by a migration.
#:
#: ``12`` is the payload-encoding marker (``PAYLOAD_ENCODING_VERSION``): a
#: write-path flag with no DDL behind it.  Because the released migrations stop
#: at 11, a module numbered 12 would make "schema 12" mean two different things.
#: Discovery still refuses a module with a reserved number (below).
RESERVED_VERSIONS: frozenset[int] = frozenset({12})

#: Numbers set aside for future W3/W4 tasks that need a new table.  Unlike
#: :data:`RESERVED_VERSIONS`, these may legitimately become migrations later; a
#: gap in this range must therefore not be read as "a migration is missing".
SPARE_VERSIONS: frozenset[int] = frozenset(range(21, 25))

#: Human-readable reasons, keyed by reserved number, used only by discovery's
#: refusal message.  Kept private so the public ``RESERVED_VERSIONS`` stays a
#: simple set of numbers for schema-version tests.
_RESERVED_REASONS: dict[int, str] = {
    12: "PAYLOAD_ENCODING_VERSION is a write-path marker, not a migration",
}


@dataclass(frozen=True, slots=True)
class MigrationModule:
    """One discovered ``vNNN_<slug>.py`` module and what it declares."""

    path: Path
    migration: Migration
    tiers: Mapping[str, Tier]


def _load_module(path: Path) -> object:
    """Execute one migration module under a name unique to its path.

    Loading from the file location (rather than by package import) is what lets
    the discovery test point at a temporary directory without a package on
    ``sys.path``.  The name embeds a digest so the same file loaded through two
    different directories cannot collide in ``sys.modules``.
    """
    digest = f"{abs(hash(str(path.resolve()))):x}"
    name = f"wattmosaic_engine.storage.migrations._discovered_{path.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a .py file always has a loader
        raise ImportError(f"cannot load migration module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_modules(directory: str | Path | None = None) -> tuple[MigrationModule, ...]:
    """Every ``vNNN_<slug>.py`` module in ``directory``, ordered by version.

    ``directory`` defaults to this package's own directory (the production
    layout); a test passes a temporary directory.

    Raises :class:`ValueError` for a reserved number, a duplicate number, a
    filename/version mismatch, or a module that does not declare ``MIGRATION``
    and ``TIERS``.  Raising at discovery (which happens at package import) is
    deliberate: a silently skipped or silently reordered migration is far worse
    than a failure to start.
    """
    base = Path(directory) if directory is not None else Path(__file__).parent
    found: dict[int, MigrationModule] = {}
    for path in sorted(base.glob("v*.py")):
        match = _MODULE_PATTERN.match(path.stem)
        if match is None:
            raise ValueError(
                f"{path.name}: migration modules must be named vNNN_<slug>.py"
            )
        number = int(match.group(1))
        if number in RESERVED_VERSIONS:
            raise ValueError(
                f"{path.name}: version {number} is reserved -- {_RESERVED_REASONS[number]}"
            )
        if number in found:
            raise ValueError(
                f"duplicate migration version {number}: {found[number].path.name} and {path.name}"
            )
        module = _load_module(path)
        migration = getattr(module, "MIGRATION", None)
        if not isinstance(migration, Migration):
            raise ValueError(f"{path.name}: module must export MIGRATION: Migration")
        if migration.version != number:
            raise ValueError(
                f"{path.name}: MIGRATION.version is {migration.version}, filename says {number}"
            )
        tiers = getattr(module, "TIERS", None)
        if not isinstance(tiers, Mapping):
            raise ValueError(f"{path.name}: module must export TIERS: dict[str, Tier]")
        for table, tier in tiers.items():
            if not isinstance(table, str) or not table:
                raise ValueError(f"{path.name}: TIERS keys must be table names")
            if tier not in TIER_NAMES:
                raise ValueError(f"{path.name}: {table!r} declares unknown tier {tier!r}")
        found[number] = MigrationModule(path=path, migration=migration, tiers=dict(tiers))
    return tuple(found[number] for number in sorted(found))


def discover_migrations(directory: str | Path | None = None) -> tuple[Migration, ...]:
    """The migrations declared by ``vNNN_*`` modules, ordered by version."""
    return tuple(module.migration for module in discover_modules(directory))


def _legacy_migrations() -> tuple[Migration, ...]:
    from . import legacy

    return legacy.MIGRATIONS


def _registry(
    legacy: tuple[Migration, ...] | None = None,
    discovered: tuple[MigrationModule, ...] | None = None,
) -> tuple[tuple[Migration, ...], dict[str, Tier]]:
    """Legacy versions plus discovered modules, checked, ordered and tier-merged.

    The two inputs are parameters so a test can drive the duplicate and
    tier-conflict checks with synthetic modules instead of a real directory.
    """
    if legacy is None:
        legacy = _legacy_migrations()
    if discovered is None:
        discovered = discover_modules()
    seen: dict[int, str] = {}
    for migration, source in [(m, "legacy.py") for m in legacy] + [
        (module.migration, module.path.name) for module in discovered
    ]:
        if migration.version in seen:
            raise ValueError(
                f"duplicate migration version {migration.version}: "
                f"{seen[migration.version]} and {source}"
            )
        seen[migration.version] = source
    tiers: dict[str, Tier] = {}
    for module in discovered:
        for table, tier in module.tiers.items():
            previous = tiers.get(table)
            if previous is not None and previous != tier:
                raise ValueError(
                    f"table {table!r} is classified {previous} and {tier} by migration modules"
                )
            tiers[table] = tier
    migrations = tuple(sorted((*legacy, *(m.migration for m in discovered)), key=lambda m: m.version))
    return migrations, tiers


MIGRATIONS, MIGRATION_TIERS = _registry()

#: Version that introduced the payload encoding (`REQ-STORAGE-008`).  Recorded
#: beside :data:`MIGRATIONS` rather than as a thirteenth migration: the encoding
#: is a **write-path** marker (``storage/payload.py`` prefixes a compressed
#: value with ``Z1:``) and the decoder reads both forms, so there is no column
#: to add and -- per §11.1, "a migration must not rewrite historical evidence" --
#: no historical payload may be rewritten.  A migration that did nothing but
#: bump ``user_version`` would advance the version on a database whose bytes
#: never changed and make ``schema_version`` a lie about the data.
PAYLOAD_ENCODING_VERSION = 12

LATEST_VERSION = max(m.version for m in MIGRATIONS)


def migration_tier_tables() -> dict[str, Tier]:
    """``{table: tier}`` declared by every discovered migration module.

    ``storage/tiers.py`` merges this into its own registry, so a later migration
    task classifies its tables in the file it owns and the tier-completeness test
    passes without editing ``tiers.py``.
    """
    return dict(MIGRATION_TIERS)


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def pending_migrations(conn: sqlite3.Connection) -> tuple[Migration, ...]:
    """Migrations this connection still has to apply, oldest first."""
    start = current_version(conn)
    return tuple(m for m in MIGRATIONS if m.version > start)


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations atomically.  Returns the resulting version."""
    for migration in pending_migrations(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in migration.statements:
                conn.execute(stmt)
            if migration.apply is not None:
                migration.apply(conn)
            conn.execute(f"PRAGMA user_version = {migration.version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return current_version(conn)


# Imported after ``Migration`` exists (``legacy`` imports it back from here), and
# re-exported because ``Database.prune`` exempts these tables by name.
from .legacy import AUDIT_TABLES  # noqa: E402

__all__ = [
    "AUDIT_TABLES",
    "LATEST_VERSION",
    "MIGRATIONS",
    "MIGRATION_TIERS",
    "Migration",
    "MigrationModule",
    "PAYLOAD_ENCODING_VERSION",
    "RESERVED_VERSIONS",
    "SPARE_VERSIONS",
    "TIER_NAMES",
    "Tier",
    "add_column_if_missing",
    "apply_migrations",
    "current_version",
    "discover_migrations",
    "discover_modules",
    "migration_tier_tables",
    "pending_migrations",
    "table_columns",
]
