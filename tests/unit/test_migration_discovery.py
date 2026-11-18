"""Migration discovery, TIERS merging, and the pre-migration backup (DEC-02).

**Why this file exists.**  X05 turns ``storage/migrations.py`` into a package in
which every later version is one ``vNNN_<slug>.py`` module owned by one task.
That only works if the *rules* are enforced somewhere that does not need editing
per version:

* modules are discovered and ordered by ``NNN``, and a duplicate number fails at
  import instead of silently shadowing another version;
* ``v012`` is refused -- 12 is ``PAYLOAD_ENCODING_VERSION``, a write-path marker
  with no DDL behind it, so a module numbered 12 would make "schema 12" mean two
  things (``analysis/A-…`` §5 T3);
* a module declares the storage tier of every table it creates, and
  ``storage/tiers.py`` merges that in, so ``test_every_table_is_classified``
  passes without a later edit (T8, plan §5);
* ``Database.__init__`` backs the file up **before** a migration numbered 13 or
  higher runs, and keeps the newest three (DEC-02, HB §16.8).  There are no
  down-migrations, so the backup *is* the rollback procedure.

**How the backup is tested without a real v013.**  X05 ships no migration ≥ 13.
The test builds a genuine ``v013_*.py`` module in a temporary directory, appends
its ``Migration`` to ``migrations.MIGRATIONS`` and opens a schema-11 database:
both ``pending_migrations`` and ``apply_migrations`` resolve ``MIGRATIONS`` at
call time, so the real constructor path runs unmodified.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from wattmosaic_engine import storage
from wattmosaic_engine.storage import database as database_module
from wattmosaic_engine.storage import migrations as migrations_module
from wattmosaic_engine.storage.database import BACKUP_FROM_VERSION, BACKUP_KEEP, Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    discover_migrations,
    discover_modules,
    migration_tier_tables,
)
from wattmosaic_engine.storage.tiers import BASE_TIER_TABLES, merge_tier_tables

#: The version this wave migrates *from*: the last released one.
PRE_MIGRATION_VERSION = 11

NOW = "2026-11-01T00:00:00+00:00"

_MODULE_TEMPLATE = '''"""Test migration module: {slug}."""

from __future__ import annotations

import sqlite3

from wattmosaic_engine.storage.migrations import Migration

TIERS = {tiers}

MIGRATION = Migration(
    version={version},
    name="{slug}",
    statements={statements},
    apply={apply},
)
'''


def _write_module(
    directory: Path,
    filename: str,
    *,
    version: int | None = None,
    slug: str | None = None,
    statements: tuple[str, ...] = (),
    tiers: dict[str, str] | None = None,
) -> Path:
    """Write a migration module from the template into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    stem = path.stem
    if version is None:
        digits = stem[:4]
        version = int(digits[1:]) if digits.startswith("v") and digits[1:].isdigit() else 1
    path.write_text(
        _MODULE_TEMPLATE.format(
            slug=slug or stem,
            version=version,
            statements=statements,
            tiers=repr(tiers or {}),
            apply=None,
        )
    )
    return path


# --------------------------------------------------------------------------- #
# Discovery and ordering
# --------------------------------------------------------------------------- #


def test_a_module_is_discovered_and_ordered_by_number(tmp_path: Path) -> None:
    """Two modules are found whatever their names sort like as strings."""
    directory = tmp_path / "migrations"
    _write_module(directory, "v900_test.py", tiers={"v900_table": "L4"})
    _write_module(directory, "v950_second.py", tiers={"v950_table": "L2"})

    found = discover_modules(directory)

    assert [module.migration.version for module in found] == [900, 950]
    assert [module.migration.name for module in found] == ["v900_test", "v950_second"]
    assert found[0].tiers == {"v900_table": "L4"}
    # The version-ordered projection is what the package appends to 1-11.
    assert [m.version for m in discover_migrations(directory)] == [900, 950]


def test_discovery_skips_non_migration_files(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    _write_module(directory, "v900_test.py")
    (directory / "__init__.py").write_text("")
    (directory / "legacy.py").write_text("MIGRATIONS = ()\n")
    (directory / "notes.txt").write_text("not a module\n")

    assert [m.migration.version for m in discover_modules(directory)] == [900]


def test_the_production_package_discovers_its_own_modules() -> None:
    """Against the real directory: every discovered version is unique and ≥ 13."""
    found = discover_modules()
    versions = [module.migration.version for module in found]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert all(version >= 13 for version in versions), versions


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_duplicate_numbers_raise(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    _write_module(directory, "v900_test.py")
    _write_module(directory, "v900_other.py")

    with pytest.raises(ValueError, match="duplicate migration version 900"):
        discover_modules(directory)


def test_a_duplicate_of_a_released_version_raises(tmp_path: Path) -> None:
    """A module numbered 13 collides once 13 is released -- no silent skip.

    ``apply_migrations`` skips any version at or below ``user_version``, so a
    duplicate number is not a re-run: it is a migration that would never apply on
    an install that already ran the other one.  The check is on numbers, not file
    names, so a released 13 and a discovered 13 collide.
    """
    _write_module(tmp_path / "migrations", "v013_entities.py")
    module = discover_modules(tmp_path / "migrations")[0]
    released_13 = Migration(version=13, name="released_entities")

    with pytest.raises(ValueError, match="duplicate migration version 13"):
        migrations_module._registry((*MIGRATIONS, released_13), (module,))


def test_v012_is_refused(tmp_path: Path) -> None:
    """12 is the payload-encoding marker, never a migration."""
    directory = tmp_path / "migrations"
    _write_module(directory, "v012_payload.py")

    with pytest.raises(ValueError, match="reserved"):
        discover_modules(directory)


def test_a_bad_filename_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    _write_module(directory, "v13_too_short.py")

    with pytest.raises(ValueError, match=r"vNNN_<slug>\.py"):
        discover_modules(directory)


def test_filename_and_body_version_must_agree(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    _write_module(directory, "v900_test.py", version=901)

    with pytest.raises(ValueError, match="filename says 900"):
        discover_modules(directory)


def test_a_module_without_tiers_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    (directory / "v900_test.py").parent.mkdir(parents=True, exist_ok=True)
    (directory / "v900_test.py").write_text(
        "from wattmosaic_engine.storage.migrations import Migration\n"
        "MIGRATION = Migration(version=900, name='test')\n"
    )

    with pytest.raises(ValueError, match="TIERS"):
        discover_modules(directory)


def test_a_module_with_an_unknown_tier_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    _write_module(directory, "v900_test.py", tiers={"v900_table": "L5"})

    with pytest.raises(ValueError, match="unknown tier"):
        discover_modules(directory)


# --------------------------------------------------------------------------- #
# TIERS merging into the published registry
# --------------------------------------------------------------------------- #


def test_merge_adds_a_declared_table_to_its_tier() -> None:
    merged = merge_tier_tables({"L1": ("a",), "L2": ()}, {"new_table": "L2"})

    assert merged["L2"] == ("new_table",)
    assert merged["L1"] == ("a",)


def test_merge_refuses_a_table_classified_twice() -> None:
    with pytest.raises(ValueError, match="classified more than once"):
        merge_tier_tables({"L1": ("dup",), "L2": ()}, {"dup": "L2"})


def test_merge_refuses_an_unknown_tier() -> None:
    with pytest.raises(ValueError, match="unknown tier"):
        merge_tier_tables({"L1": (), "L2": ()}, {"t": "L9"})


def test_the_published_registry_is_base_plus_migration_tiers() -> None:
    """``TIER_TABLES`` is not a second hand-maintained list."""
    from wattmosaic_engine.storage.tiers import TIER_TABLES

    declared = migration_tier_tables()
    assert merge_tier_tables(BASE_TIER_TABLES, declared) == TIER_TABLES
    for table, tier in declared.items():
        assert TIER_TABLES[tier].count(table) == 1


# --------------------------------------------------------------------------- #
# The pre-migration backup (DEC-02)
# --------------------------------------------------------------------------- #


def _schema11_database(path: Path) -> Path:
    """A schema-11 database with one row, built without ``Database``.

    ``Database.__init__`` migrates immediately, so a plain connection is the only
    way to produce a fixture that is genuinely *pre*-migration.
    """
    conn = sqlite3.connect(path)
    try:
        for migration in MIGRATIONS:
            if migration.version > PRE_MIGRATION_VERSION:
                break
            for statement in migration.statements:
                conn.execute(statement)
            if migration.apply is not None:
                migration.apply(conn)
        conn.execute(f"PRAGMA user_version = {PRE_MIGRATION_VERSION}")
        conn.execute(
            "INSERT INTO engine_state(key, value, updated_at) VALUES('legacy', ?, ?)",
            (json.dumps({"kept": True}), NOW),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _pending(monkeypatch: pytest.MonkeyPatch, *versions: int) -> None:
    """Make the constructor see exactly these migrations as pending."""
    pending = tuple(Migration(version=version, name=f"pending_{version}") for version in versions)
    monkeypatch.setattr(database_module, "pending_migrations", lambda conn: pending)


def test_a_fresh_database_is_not_backed_up(tmp_path: Path) -> None:
    """Nothing to lose, so no file is copied on a brand-new install."""
    path = tmp_path / "fresh.db"

    db = Database(path)
    try:
        assert db.backup_path is None
    finally:
        db.close()
    assert list(tmp_path.glob("*.bak")) == []


def test_an_up_to_date_database_is_not_backed_up_on_every_start(tmp_path: Path) -> None:
    """The check runs on the pre-migration version, so a reopen copies nothing."""
    path = _schema11_database(tmp_path / "current.db")
    first = Database(path)
    try:
        assert first.backup_path is None
    finally:
        first.close()

    second = Database(path)
    try:
        assert second.backup_path is None
        assert second.schema_version == LATEST_VERSION
    finally:
        second.close()
    assert list(tmp_path.glob("*.bak")) == []


def test_a_pending_migration_below_13_does_not_back_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Versions 1-11 shipped without a backup; only 13+ is destructive (DEC-02)."""
    path = _schema11_database(tmp_path / "old_pending.db")
    _pending(monkeypatch, 11)

    db = Database(path)
    try:
        assert db.backup_path is None
    finally:
        db.close()
    assert list(tmp_path.glob("*.bak")) == []


def test_a_pending_migration_at_13_backs_up_before_migrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _schema11_database(tmp_path / "pending.db")
    _pending(monkeypatch, 13)

    db = Database(path)
    try:
        backup = db.backup_path
        assert backup is not None
        # Naming is ``<db>.pre-v<NNN>.<UTC>.bak`` and it sits beside the database.
        assert backup.name.startswith(f"{path.name}.pre-v013.")
        assert backup.name.endswith(".bak")
        assert backup.parent == path.parent
        # The copy is a real, self-contained database carrying the pre-migration
        # rows and the pre-migration ``user_version``.
        conn = sqlite3.connect(backup)
        try:
            assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == PRE_MIGRATION_VERSION
            row = conn.execute("SELECT value FROM engine_state WHERE key='legacy'").fetchone()
            assert json.loads(row[0]) == {"kept": True}
        finally:
            conn.close()
    finally:
        db.close()


def test_a_real_v013_module_migrates_and_leaves_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unmodified constructor path: discover, back up, then apply."""
    migrations_dir = tmp_path / "migrations"
    _write_module(
        migrations_dir,
        "v013_probe.py",
        statements=("CREATE TABLE v013_probe (id INTEGER PRIMARY KEY)",),
        tiers={"v013_probe": "L4"},
    )
    discovered = discover_modules(migrations_dir)
    monkeypatch.setattr(
        migrations_module, "MIGRATIONS", (*MIGRATIONS, discovered[0].migration)
    )

    path = _schema11_database(tmp_path / "upgrading.db")
    db = Database(path)
    try:
        assert db.schema_version == 13
        assert db.backup_path is not None and db.backup_path.exists()
        tables = {
            row[0]
            for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "v013_probe" in tables
    finally:
        db.close()


def test_backup_rotation_keeps_the_newest_three(tmp_path: Path) -> None:
    """Five old backups collapse to the newest three; older ones are removed."""
    path = _schema11_database(tmp_path / "rotate.db")
    db = Database(path)
    try:
        assert BACKUP_KEEP == 3
        base = 1_700_000_000
        for index in range(5):
            backup = tmp_path / f"{path.name}.pre-v013.2026010{index}T000000Z.bak"
            backup.write_text("old")
            # Distinct mtimes, youngest last: the rotation orders by age.
            os.utime(backup, ns=(base + index, base + index))
        # A sidecar of a removed backup must go with it.
        sidecar = tmp_path / f"{path.name}.pre-v013.20260100T000000Z.bak-wal"
        sidecar.write_text("old")

        removed = db._rotate_backups()

        remaining = sorted(p.name for p in tmp_path.glob(f"{path.name}.pre-v*.bak"))
        assert remaining == [
            f"{path.name}.pre-v013.20260102T000000Z.bak",
            f"{path.name}.pre-v013.20260103T000000Z.bak",
            f"{path.name}.pre-v013.20260104T000000Z.bak",
        ]
        assert len(removed) == 2
        assert not sidecar.exists()
    finally:
        db.close()


def test_a_new_backup_prunes_older_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each new pre-migration backup goes through the same rotation."""
    path = _schema11_database(tmp_path / "rotation_on_create.db")
    for index in range(4):
        (tmp_path / f"{path.name}.pre-v013.2026010{index}T000000Z.bak").write_text("old")
    _pending(monkeypatch, 13)

    db = Database(path)
    try:
        assert db.backup_path is not None
        backups = sorted(tmp_path.glob(f"{path.name}.pre-v*.bak"))
        assert len(backups) == BACKUP_KEEP
        assert db.backup_path in backups
    finally:
        db.close()


def test_the_backup_threshold_is_the_first_canonical_migration() -> None:
    """13 is the first destructive canonical-model rebuild (plan §5)."""
    assert BACKUP_FROM_VERSION == 13
    # And 12 stays reserved rather than becoming a migration.
    assert migrations_module.PAYLOAD_ENCODING_VERSION == 12
    assert all(m.version != 12 for m in MIGRATIONS)


def test_the_package_facade_reexports_the_released_migrations() -> None:
    """``from wattmosaic_engine.storage import …`` keeps working after the split."""
    assert storage.LATEST_VERSION == LATEST_VERSION == max(m.version for m in MIGRATIONS)
    assert [m.version for m in MIGRATIONS[:11]] == list(range(1, 12))
    assert migrations_module.AUDIT_TABLES  # declared beside the DDL that made them
