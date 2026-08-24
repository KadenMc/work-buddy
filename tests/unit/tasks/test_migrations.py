from __future__ import annotations

import hashlib
import sqlite3

from work_buddy.obsidian.tasks.migrations import TASK_MIGRATIONS as LEGACY_MIGRATIONS
from work_buddy.obsidian.tasks.store import _migrate_schema as legacy_store_migrate
from work_buddy.tasks.migrations import NativeTaskMigrationRunner, TASK_MIGRATIONS
from work_buddy.tasks.migration import (
    LegacyManifestEntry,
    LegacyTaskInventoryBuilder,
    TaskMigrationLedger,
)
from work_buddy.tasks.store import TaskStore, default_task_db_path


def _legacy_v11(path) -> None:
    conn = sqlite3.connect(path)
    try:
        LEGACY_MIGRATIONS.run(conn)
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO task_metadata (
                task_id, state, urgency, created_at, updated_at, description,
                deadline_date, has_deadline, deleted_at, archived_at,
                created_by_session
            ) VALUES
                ('t-live', 'focused', 'high', '2026-01-01', '2026-01-02',
                 'Keep this row', '2026-12-01', 1, NULL, NULL, 'session-a'),
                ('t-deleted', 'inbox', 'medium', '2026-01-03', '2026-01-04',
                 'Keep tombstone', NULL, 0, '2026-02-01', NULL, NULL),
                ('t-archived-open', 'inbox', 'low', '2026-01-05', '2026-01-06',
                 'Keep anomaly', NULL, 0, NULL, '2026-02-02', NULL)
            """
        )
        conn.execute(
            "INSERT INTO task_tags (task_id, tag, is_namespace) "
            "VALUES ('t-live', 'research/native', 1)"
        )
        conn.execute(
            "INSERT INTO task_state_history "
            "(task_id, old_state, new_state, changed_at, reason) "
            "VALUES ('t-live', 'inbox', 'focused', '2026-01-02', 'legacy')"
        )
        conn.execute(
            """
            INSERT INTO task_action_items (
                task_id, sequence, description, state, authorship,
                created_at, updated_at
            ) VALUES ('t-live', 1, 'First step', 'pending', 'user',
                      '2026-01-02', '2026-01-02')
            """
        )
        conn.execute("COMMIT")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    finally:
        conn.close()


def test_forward_migration_preserves_v11_rows_and_history(tmp_path):
    path = tmp_path / "task_metadata.db"
    _legacy_v11(path)

    store = TaskStore(path)
    store.initialize()
    store.initialize()  # idempotent reopen

    conn = store.connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == TASK_MIGRATIONS.target_version == 19
        assert conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM task_tags").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_action_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_state_history").fetchone()[0] == 1
        live = conn.execute("SELECT * FROM task_metadata WHERE task_id = 't-live'").fetchone()
        assert live["revision"] == 1
        assert live["deadline_date"] == "2026-12-01"
        assert live["due_date"] is None
        assert live["created_by_session"] == "session-a"
        assert conn.execute("SELECT COUNT(*) FROM task_metadata WHERE deleted_at IS NOT NULL").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_metadata WHERE archived_at IS NOT NULL").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        native_versions = {
            row[0]
            for row in conn.execute(
                "SELECT version FROM _migration_history WHERE version >= 12"
            )
        }
        assert native_versions == {12, 13, 14, 15, 16, 17, 18, 19}
    finally:
        conn.close()


def test_v11_inventory_can_stage_evolved_rows_after_forward_migration(tmp_path):
    path = tmp_path / "task_metadata.db"
    _legacy_v11(path)
    conn = sqlite3.connect(path)
    try:
        for table in ("task_tags", "task_state_history", "task_action_items"):
            conn.execute(
                f"UPDATE {table} SET task_id='t-a1' WHERE task_id='t-live'"
            )
        conn.execute("UPDATE task_metadata SET task_id='t-a1' WHERE task_id='t-live'")
        conn.execute(
            "UPDATE task_metadata SET task_id='t-a2' "
            "WHERE task_id='t-archived-open'"
        )
        conn.commit()
    finally:
        conn.close()
    source = tmp_path / "legacy-tasks"
    source.mkdir()
    files = {
        "master-task-list.md": b"- [ ] #todo Keep this row \xf0\x9f\x86\x94 t-a1\n",
        "archive.md": b"- [ ] #todo Keep anomaly \xf0\x9f\x86\x94 t-a2\n",
    }
    manifest: list[LegacyManifestEntry] = []
    for relative, content in files.items():
        (source / relative).write_bytes(content)
        manifest.append(
            LegacyManifestEntry(
                relative_path=relative,
                byte_length=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )

    inventory = LegacyTaskInventoryBuilder(
        cohort_id="v11-forward-stage",
        source_root=source,
        task_db_path=path,
        manifest=manifest,
    ).build()
    assert inventory.valid, inventory.errors
    assert inventory.source_db_schema_version == 11

    store = TaskStore(path)
    store.initialize()
    TaskMigrationLedger(store).begin_shadow(
        inventory,
        actor="test-operator",
        session_id="test-session",
        backup_receipts=({"receipt_id": "fixture-backup", "verified": True},),
    )

    conn = store.connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
        assert conn.execute(
            "SELECT COUNT(*) FROM task_migration_existing_task_stage "
            "WHERE cohort_id='v11-forward-stage'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_fresh_store_contains_native_support_tables(task_store):
    conn = task_store.connect()
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "task_document_links",
            "recovered_task_documents",
            "task_local_file_roots",
            "task_local_file_links",
            "task_mutation_receipts",
            "task_collection_state",
            "task_event_outbox",
            "task_system_state",
            "task_migration_cohorts",
            "task_migration_inventory",
            "task_migration_idless_stage",
            "task_migration_existing_task_stage",
            "task_migration_document_stage",
            "task_migration_local_link_stage",
            "task_migration_binding_transitions",
            "task_migration_gates",
            "task_migration_receipts",
        } <= tables
        assert task_store.system_state().authority_epoch == "legacy"
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_task_local_links_document" in indexes
        document_stage_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(task_migration_document_stage)")
        }
        assert "source_receipt_id" in document_stage_columns
    finally:
        conn.close()


def test_native_upgrade_remains_openable_through_legacy_store_seam(task_store):
    conn = sqlite3.connect(task_store.path)
    try:
        legacy_store_migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
    finally:
        conn.close()


def test_v19_safely_backfills_document_stage_source_receipt(tmp_path):
    path = tmp_path / "v18-task-metadata.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        v18 = NativeTaskMigrationRunner(
            "task_metadata",
            migrations=TASK_MIGRATIONS.migrations[:-1],
        )
        v18.run(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
        conn.execute(
            """
            INSERT INTO task_migration_cohorts (
                cohort_id, schema_version, state, inventory_sha256,
                manifest_sha256, source_file_count, source_tree_bytes,
                source_root_fingerprint, source_db_sha256, source_db_integrity,
                source_db_schema_version, previous_authority_epoch, actor,
                retention_policy, counts_json, created_at, updated_at
            ) VALUES ('cohort-v18', 1, 'shadow', ?, ?, 1, 1, ?, ?, 'ok',
                      11, 'legacy', 'test', 'until_explicit_user_approval',
                      '{}', '2026-01-01', '2026-01-01')
            """,
            ("a" * 64, "b" * 64, "c" * 64, "d" * 64),
        )
        conn.execute(
            """
            INSERT INTO task_migration_document_stage (
                cohort_id, note_uuid, task_id, store_id, document_id,
                binding_id, source_ref, source_content_sha256,
                normalized_content_sha256, document_content_sha256,
                document_head_sha256, rewrite_manifest_json, lifecycle,
                classification, byte_parity, normalized_parity, imported_at
            ) VALUES ('cohort-v18', 'note-v18', 't-v18', 'store-v18',
                      'document-v18', 'binding-v18', 'source-v18', ?, ?, ?, ?,
                      '[]', 'current', 'task_note_live', 1, 1, '2026-01-01')
            """,
            ("e" * 64, "f" * 64, "1" * 64, "2" * 64),
        )
        conn.execute(
            """
            INSERT INTO task_migration_document_stage (
                cohort_id, note_uuid, task_id, store_id, document_id,
                binding_id, source_ref, source_content_sha256,
                normalized_content_sha256, document_content_sha256,
                document_head_sha256, rewrite_manifest_json, lifecycle,
                classification, byte_parity, normalized_parity, imported_at
            ) VALUES ('cohort-v18', 'note-decoy', NULL, 'store-v18',
                      'document-wanted', NULL, 'source-decoy', ?, ?, ?, ?,
                      '[]', 'recovered', 'recovered_task_document', 1, 1,
                      '2026-01-01')
            """,
            ("4" * 64, "5" * 64, "6" * 64, "7" * 64),
        )
        conn.execute(
            """
            INSERT INTO recovered_task_documents (
                recovery_id, note_uuid, store_id, document_id,
                source_receipt_id, classification, lifecycle, imported_at
            ) VALUES ('recovery-decoy', 'note-decoy', 'store-v18',
                      'document-other', 'receipt-wrong-document',
                      'recovered_task_document', 'recovered', '2026-01-01')
            """
        )
        conn.execute(
            """
            INSERT INTO task_migration_local_link_stage (
                cohort_id, link_id, task_id, note_uuid, store_id, document_id,
                root_id, relative_path, display_name, suffix, media_type,
                byte_length, sha256, sensitivity, allowed_action,
                policy_revision, source_receipt_id
            ) VALUES ('cohort-v18', 'link-v18', 't-v18', 'note-v18',
                      'store-v18', 'document-v18', 'root-v18', 'asset.pdf',
                      'asset.pdf', '.pdf', 'application/pdf', 1, ?, 'private',
                      'open', 1, 'receipt-v18')
            """,
            ("3" * 64,),
        )
        conn.commit()

        TASK_MIGRATIONS.run(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
        assert conn.execute(
            "SELECT source_receipt_id FROM task_migration_document_stage "
            "WHERE cohort_id='cohort-v18' AND note_uuid='note-v18'"
        ).fetchone()[0] == "receipt-v18"
        assert conn.execute(
            "SELECT source_receipt_id FROM task_migration_document_stage "
            "WHERE cohort_id='cohort-v18' AND note_uuid='note-decoy'"
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_default_path_keeps_registered_task_metadata_resource(monkeypatch, tmp_path):
    monkeypatch.setenv("WORK_BUDDY_DATA_DIR", str(tmp_path))
    assert default_task_db_path() == tmp_path / "db" / "task_metadata.db"
