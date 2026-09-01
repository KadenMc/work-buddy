from __future__ import annotations

import sqlite3


def test_personal_knowledge_database_is_registered_for_backup_and_restore():
    from work_buddy.backups.local import VITAL_DBS
    from work_buddy.backups.restore import _current_known_max_schema_versions
    from work_buddy.paths import RESOURCES

    assert RESOURCES["db/personal-knowledge"] == "db/personal_knowledge.db"
    assert VITAL_DBS["personal_knowledge"] == "db/personal-knowledge"
    assert _current_known_max_schema_versions()["personal_knowledge"] == 3


def test_restore_migration_opens_personal_database(personal_store):
    from work_buddy.backups.restore import _apply_migrations_inplace

    _apply_migrations_inplace("personal_knowledge", personal_store.db_path)
    assert personal_store.schema_version() == 3
    personal_store.validate()


def test_hot_backup_round_trips_personal_rows(personal_store, tmp_path):
    from work_buddy.backups.local import _hot_backup
    from work_buddy.backups.restore import _apply_migrations_inplace

    personal_store.create_unit(
        logical_path="personal/preferences/backed-up",
        name="Backed up",
        body="Private local prose",
        idempotency_key="backup-fixture",
    )
    restored = tmp_path / "restored_personal_knowledge.db"
    _hot_backup(personal_store.db_path, restored)
    _apply_migrations_inplace("personal_knowledge", restored)
    conn = sqlite3.connect(restored)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM personal_units").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM personal_unit_revisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM personal_search_outbox").fetchone()[0] == 1
    finally:
        conn.close()
