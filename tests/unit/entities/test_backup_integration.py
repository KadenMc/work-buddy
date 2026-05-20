"""Regression guard: the entity registry DB is a backed-up vital DB.

The entity registry is durable, authored user data — losing it means
the user has to re-teach an agent every name in their world. It must
travel with the off-machine snapshot system. This test pins that.
"""

from __future__ import annotations


def test_entities_is_a_vital_db():
    """entities.db is declared in the backup system's VITAL_DBS set."""
    from work_buddy.backups.local import VITAL_DBS
    assert "entities" in VITAL_DBS
    assert VITAL_DBS["entities"] == "db/entities"


def test_entities_resource_resolves():
    """The db/entities resource id resolves to a concrete path —
    a VITAL_DBS entry whose resource id is missing from
    paths.RESOURCES would be silently skipped by _resolve_vital_dbs."""
    from work_buddy.backups.local import _resolve_vital_dbs
    resolved = _resolve_vital_dbs()
    assert "entities" in resolved
    assert resolved["entities"].name == "entities.db"


def test_entities_db_path_registered_in_paths():
    """The db/entities resource id is registered in work_buddy.paths."""
    from work_buddy.paths import resolve
    path = resolve("db/entities")
    assert path.name == "entities.db"


# ─── Restore side ───────────────────────────────────────────────────
#
# Being in VITAL_DBS only guarantees the SNAPSHOT captures entities.db.
# Restore is the other half: a staged entities.db must be rolled
# forward through ENTITY_MIGRATIONS, and the forward-time-travel guard
# must know entities' schema ceiling. Both are keyed by the logical
# name, and a missing wiring fails silently — hence these guards.


def test_entities_migration_runner_wired_into_restore(tmp_path):
    """_apply_migrations_inplace must roll a staged entities.db forward
    through ENTITY_MIGRATIONS. A missing branch would leave a restored
    snapshot un-migrated until the store lazily fixed it — bypassing
    restore's own post-migration integrity + row-count validation."""
    import sqlite3
    from work_buddy.backups.restore import _apply_migrations_inplace
    from work_buddy.entities.migrations import ENTITY_MIGRATIONS

    db = tmp_path / "entities.db"
    # A fresh, empty SQLite file — user_version 0, no tables.
    sqlite3.connect(str(db)).close()

    _apply_migrations_inplace("entities", db)

    conn = sqlite3.connect(str(db))
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert ver == ENTITY_MIGRATIONS.target_version
    assert "entities" in tables  # the ladder actually ran


def test_entities_schema_ceiling_known_to_restore():
    """The restore validator must know entities' max schema version,
    or the forward-time-travel guard (snap_v > known_max > 0) silently
    never fires for an entities snapshot from newer code."""
    from work_buddy.backups.restore import _current_known_max_schema_versions
    from work_buddy.entities.migrations import ENTITY_MIGRATIONS

    known = _current_known_max_schema_versions()
    assert known.get("entities") == ENTITY_MIGRATIONS.target_version
    assert known["entities"] > 0


def test_restore_schema_versions_keyed_by_vital_db_names():
    """_current_known_max_schema_versions must key strictly by the
    VITAL_DBS logical names — the manifest's schema_versions uses those.
    A stray key (e.g. an on-disk filename) leaves the real entry at 0,
    silently disabling that DB's forward-time-travel guard."""
    from work_buddy.backups.restore import _current_known_max_schema_versions
    from work_buddy.backups.local import VITAL_DBS

    known = _current_known_max_schema_versions()
    assert set(known.keys()) == set(VITAL_DBS.keys())
