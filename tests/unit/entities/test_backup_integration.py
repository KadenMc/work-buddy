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
