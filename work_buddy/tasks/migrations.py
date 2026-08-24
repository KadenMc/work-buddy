"""Native task-schema migrations layered on the established v11 resource.

The original task database entered the versioned migration framework at v11.
This module deliberately owns the v12+ ladder without importing a vault
integration.  Its runner accepts the historical audit rows as an immutable
baseline and continues strict hash verification for every native migration.
"""

from __future__ import annotations

import sqlite3

from work_buddy.storage.migrations import (
    HASH_FORMAT_CURRENT,
    Migration,
    MigrationHashMismatch,
    MigrationRunner,
)

LEGACY_SCHEMA_VERSION = 11


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(
    conn: sqlite3.Connection,
    table: str,
    name: str,
    declaration: str,
) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _m001_bootstrap_v11(conn: sqlite3.Connection) -> None:
    """Create the final historical v11 shape for a genuinely new database."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_metadata (
            task_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'inbox',
            urgency TEXT NOT NULL DEFAULT 'medium',
            complexity TEXT,
            contract TEXT,
            note_uuid TEXT,
            snooze_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            archived_at TEXT,
            task_kind TEXT NOT NULL DEFAULT 'task',
            density TEXT NOT NULL DEFAULT 'sparse',
            outcome_text TEXT,
            next_action_text TEXT,
            definition_of_done TEXT,
            creation_effort TEXT NOT NULL DEFAULT 'developed',
            user_involvement TEXT NOT NULL DEFAULT 'high',
            creation_provenance TEXT NOT NULL DEFAULT 'manual',
            has_deadline INTEGER NOT NULL DEFAULT 0,
            deadline_date TEXT,
            has_dependency INTEGER NOT NULL DEFAULT 0,
            dependency_hint TEXT,
            description TEXT,
            risk_profile_json TEXT,
            automation_tier_achievable INTEGER,
            last_actor TEXT,
            agent_required_contexts TEXT,
            user_required_contexts TEXT,
            required_contexts_source TEXT,
            current_action_item_id INTEGER,
            deleted_at TEXT,
            created_by_session TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_state_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            old_state TEXT,
            new_state TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            reason TEXT,
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id),
            UNIQUE(task_id, session_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_tags (
            task_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            is_namespace INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, tag),
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_action_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            description TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            risk_profile_json TEXT,
            agent_required_contexts TEXT,
            user_required_contexts TEXT,
            definition_of_done TEXT,
            authorship TEXT NOT NULL DEFAULT 'agent_unapproved',
            completed_at TEXT,
            handoff_package_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id),
            UNIQUE(task_id, sequence)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_sync_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_full_sync_at TEXT,
            last_sync_created INTEGER NOT NULL DEFAULT 0,
            last_sync_updated INTEGER NOT NULL DEFAULT 0,
            last_sync_deleted INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lww_meta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            row_pk TEXT NOT NULL,
            field TEXT NOT NULL,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '[]',
            process TEXT NOT NULL,
            from_surface TEXT,
            to_surface TEXT NOT NULL
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_task_state ON task_metadata(state)",
        "CREATE INDEX IF NOT EXISTS idx_task_contract ON task_metadata(contract)",
        "CREATE INDEX IF NOT EXISTS idx_task_history ON task_state_history(task_id, changed_at)",
        "CREATE INDEX IF NOT EXISTS idx_task_sessions_task ON task_sessions(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_task_sessions_session ON task_sessions(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag)",
        "CREATE INDEX IF NOT EXISTS idx_task_tags_ns ON task_tags(is_namespace, tag)",
        "CREATE INDEX IF NOT EXISTS idx_action_items_task ON task_action_items(task_id, sequence)",
        "CREATE INDEX IF NOT EXISTS idx_action_items_state ON task_action_items(state)",
        "CREATE INDEX IF NOT EXISTS idx_lww_meta_latest ON lww_meta(table_name, row_pk, field, to_surface, ts)",
    ):
        conn.execute(sql)


def _historical_noop(conn: sqlite3.Connection) -> None:
    """Reconcile a partially-versioned historical database to the v11 shape.

    Some supported installations were left at an early user_version while
    already using the corresponding partial schema.  The old ladder used one
    function per version; the neutral runner cannot import that integration
    module, so this idempotent compatibility finalizer supplies every missing
    historical column/table before native v12 runs.
    """
    _m001_bootstrap_v11(conn)
    task_columns = (
        ("task_kind", "TEXT NOT NULL DEFAULT 'task'"),
        ("density", "TEXT NOT NULL DEFAULT 'sparse'"),
        ("outcome_text", "TEXT"),
        ("next_action_text", "TEXT"),
        ("definition_of_done", "TEXT"),
        ("creation_effort", "TEXT NOT NULL DEFAULT 'developed'"),
        ("user_involvement", "TEXT NOT NULL DEFAULT 'high'"),
        ("creation_provenance", "TEXT NOT NULL DEFAULT 'manual'"),
        ("has_deadline", "INTEGER NOT NULL DEFAULT 0"),
        ("deadline_date", "TEXT"),
        ("has_dependency", "INTEGER NOT NULL DEFAULT 0"),
        ("dependency_hint", "TEXT"),
        ("description", "TEXT"),
        ("risk_profile_json", "TEXT"),
        ("automation_tier_achievable", "INTEGER"),
        ("last_actor", "TEXT"),
        ("agent_required_contexts", "TEXT"),
        ("user_required_contexts", "TEXT"),
        ("required_contexts_source", "TEXT"),
        ("current_action_item_id", "INTEGER"),
        ("deleted_at", "TEXT"),
        ("created_by_session", "TEXT"),
    )
    for name, declaration in task_columns:
        _add_column(conn, "task_metadata", name, declaration)
    for name, declaration in (
        ("risk_profile_json", "TEXT"),
        ("agent_required_contexts", "TEXT"),
        ("user_required_contexts", "TEXT"),
        ("definition_of_done", "TEXT"),
        ("authorship", "TEXT NOT NULL DEFAULT 'agent_unapproved'"),
        ("completed_at", "TEXT"),
        ("handoff_package_path", "TEXT"),
        ("deleted_at", "TEXT"),
    ):
        _add_column(conn, "task_action_items", name, declaration)


def _m012_native_columns(conn: sqlite3.Connection) -> None:
    """Add native task lifecycle, date, import, and CAS columns."""
    _add_column(conn, "task_metadata", "revision", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "task_metadata", "due_date", "TEXT")
    _add_column(conn, "task_metadata", "snooze_resume_state", "TEXT")
    _add_column(conn, "task_metadata", "restored_at", "TEXT")
    _add_column(conn, "task_metadata", "legacy_import_receipt_id", "TEXT")
    conn.execute("UPDATE task_metadata SET revision = 1 WHERE revision IS NULL OR revision < 1")
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS task_revision_insert_guard
        BEFORE INSERT ON task_metadata
        WHEN NEW.revision IS NULL OR NEW.revision < 1
        BEGIN
            SELECT RAISE(ABORT, 'task revision must be positive');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS task_revision_update_guard
        BEFORE UPDATE OF revision ON task_metadata
        WHEN NEW.revision IS NULL OR NEW.revision < 1
        BEGIN
            SELECT RAISE(ABORT, 'task revision must be positive');
        END
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_due_date ON task_metadata(due_date)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_native_lifecycle "
        "ON task_metadata(deleted_at, archived_at, state, snooze_until)"
    )


def _m013_document_catalogs(conn: sqlite3.Connection) -> None:
    """Add canonical task/document associations and recovered-note catalog."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_document_links (
            task_id TEXT PRIMARY KEY REFERENCES task_metadata(task_id),
            note_uuid TEXT NOT NULL UNIQUE,
            store_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            binding_id TEXT NOT NULL UNIQUE,
            lifecycle TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            retired_at TEXT,
            UNIQUE (store_id, document_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recovered_task_documents (
            recovery_id TEXT PRIMARY KEY,
            note_uuid TEXT NOT NULL UNIQUE,
            store_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            source_receipt_id TEXT NOT NULL,
            classification TEXT NOT NULL,
            lifecycle TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            claimed_task_id TEXT REFERENCES task_metadata(task_id),
            UNIQUE (store_id, document_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recovered_task_docs_lifecycle "
        "ON recovered_task_documents(lifecycle, imported_at)"
    )


def _m014_local_file_catalog(conn: sqlite3.Connection) -> None:
    """Add metadata-only file references; no file bytes or host paths."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_local_file_roots (
            root_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            policy_revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_local_file_links (
            link_id TEXT PRIMARY KEY,
            task_id TEXT REFERENCES task_metadata(task_id),
            store_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            root_id TEXT NOT NULL REFERENCES task_local_file_roots(root_id),
            relative_path TEXT NOT NULL,
            display_name TEXT NOT NULL,
            suffix TEXT NOT NULL,
            media_type TEXT NOT NULL,
            byte_length INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            sensitivity TEXT NOT NULL,
            allowed_action TEXT NOT NULL CHECK (allowed_action IN ('open', 'reveal')),
            policy_revision INTEGER NOT NULL,
            source_receipt_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (store_id, document_id, relative_path)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_local_links_task "
        "ON task_local_file_links(task_id, created_at)"
    )


def _m015_mutation_infrastructure(conn: sqlite3.Connection) -> None:
    """Add idempotency receipts, collection revision, outbox, and epoch state."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_mutation_receipts (
            receipt_id TEXT PRIMARY KEY,
            client_mutation_id TEXT NOT NULL UNIQUE,
            actor TEXT NOT NULL,
            session_id TEXT,
            mutation TEXT NOT NULL,
            task_id TEXT,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_collection_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO task_collection_state (id, revision, updated_at) "
        "VALUES (1, 0, datetime('now'))"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_event_outbox (
            event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            mutation TEXT NOT NULL,
            task_revision INTEGER NOT NULL,
            collection_revision INTEGER NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            published_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            FOREIGN KEY (task_id) REFERENCES task_metadata(task_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_outbox_pending "
        "ON task_event_outbox(published_at, collection_revision)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_system_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            authority_epoch TEXT NOT NULL,
            cowork_task_store_id TEXT,
            cutover_receipt_id TEXT,
            rollback_fence INTEGER NOT NULL DEFAULT 0,
            process_generation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO task_system_state "
        "(id, authority_epoch, rollback_fence, process_generation, updated_at) "
        "VALUES (1, 'legacy', 0, 0, datetime('now'))"
    )


def _m016_history_enrichment(conn: sqlite3.Connection) -> None:
    """Enrich the existing append-only history with native receipt metadata."""
    _add_column(conn, "task_state_history", "mutation", "TEXT")
    _add_column(conn, "task_state_history", "actor", "TEXT")
    _add_column(conn, "task_state_history", "session_id", "TEXT")
    _add_column(conn, "task_state_history", "receipt_id", "TEXT")
    _add_column(conn, "task_state_history", "task_revision", "INTEGER")
    _add_column(conn, "task_state_history", "collection_revision", "INTEGER")
    _add_column(conn, "task_state_history", "details_json", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_history_collection "
        "ON task_state_history(collection_revision)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_history_receipt "
        "ON task_state_history(receipt_id)"
    )


def _m017_authoring_fields(conn: sqlite3.Connection) -> None:
    """Preserve distinct React authoring fields without collapsing prose."""
    _add_column(conn, "task_metadata", "summary_text", "TEXT")
    _add_column(conn, "task_metadata", "dependencies_json", "TEXT")


def _m018_legacy_cutover_ledger(conn: sqlite3.Connection) -> None:
    """Add the resumable, cohort-scoped legacy import/cutover ledger.

    These tables contain relative source coordinates, hashes, parsed task
    metadata, and operation receipts.  They deliberately contain neither a
    vault root nor attachment bytes.  Absolute legacy-root bindings belong to
    the separately protected local-file resolver, not the task database.
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_migration_cohorts (
            cohort_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            state TEXT NOT NULL,
            inventory_sha256 TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            source_file_count INTEGER NOT NULL,
            source_tree_bytes INTEGER NOT NULL,
            source_root_fingerprint TEXT NOT NULL,
            source_db_sha256 TEXT NOT NULL,
            source_db_integrity TEXT NOT NULL,
            source_db_schema_version INTEGER NOT NULL,
            previous_authority_epoch TEXT NOT NULL,
            target_authority_epoch TEXT,
            rollback_authority_epoch TEXT,
            cowork_task_store_id TEXT,
            cutover_receipt_id TEXT,
            fence_receipt_id TEXT,
            expected_process_generation INTEGER,
            actor TEXT NOT NULL,
            session_id TEXT,
            retention_policy TEXT NOT NULL
                DEFAULT 'until_explicit_user_approval',
            counts_json TEXT NOT NULL,
            approved_exceptions_json TEXT NOT NULL DEFAULT '[]',
            backup_receipts_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            activated_at TEXT,
            aborted_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_migration_inventory (
            cohort_id TEXT NOT NULL REFERENCES task_migration_cohorts(cohort_id),
            item_key TEXT NOT NULL,
            item_kind TEXT NOT NULL,
            relative_path TEXT,
            line_number INTEGER,
            task_id TEXT,
            note_uuid TEXT,
            content_sha256 TEXT,
            byte_length INTEGER,
            classification TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_bytes BLOB,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (cohort_id, item_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_migration_inventory_class "
        "ON task_migration_inventory(cohort_id, classification, item_kind)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_migration_idless_stage (
            cohort_id TEXT NOT NULL REFERENCES task_migration_cohorts(cohort_id),
            source_key TEXT NOT NULL,
            task_id TEXT NOT NULL,
            exact_line BLOB NOT NULL,
            line_sha256 TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            activated_at TEXT,
            PRIMARY KEY (cohort_id, source_key),
            UNIQUE (cohort_id, task_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_migration_existing_task_stage (
            cohort_id TEXT NOT NULL REFERENCES task_migration_cohorts(cohort_id),
            source_key TEXT NOT NULL,
            task_id TEXT NOT NULL,
            expected_row_sha256 TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            activated_at TEXT,
            PRIMARY KEY (cohort_id, task_id),
            UNIQUE (cohort_id, source_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_migration_document_stage (
            cohort_id TEXT NOT NULL REFERENCES task_migration_cohorts(cohort_id),
            note_uuid TEXT NOT NULL,
            task_id TEXT,
            store_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            binding_id TEXT,
            source_ref TEXT NOT NULL,
            source_content_sha256 TEXT NOT NULL,
            normalized_content_sha256 TEXT NOT NULL,
            document_content_sha256 TEXT NOT NULL,
            document_head_sha256 TEXT NOT NULL,
            rewrite_manifest_json TEXT NOT NULL DEFAULT '[]',
            lifecycle TEXT NOT NULL,
            classification TEXT NOT NULL,
            byte_parity INTEGER NOT NULL,
            normalized_parity INTEGER NOT NULL,
            imported_at TEXT NOT NULL,
            activated_at TEXT,
            PRIMARY KEY (cohort_id, note_uuid),
            UNIQUE (cohort_id, store_id, document_id),
            UNIQUE (cohort_id, binding_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_migration_local_link_stage (
            cohort_id TEXT NOT NULL REFERENCES task_migration_cohorts(cohort_id),
            link_id TEXT NOT NULL,
            task_id TEXT,
            note_uuid TEXT NOT NULL,
            store_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            root_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            display_name TEXT NOT NULL,
            suffix TEXT NOT NULL,
            media_type TEXT NOT NULL,
            byte_length INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            sensitivity TEXT NOT NULL,
            allowed_action TEXT NOT NULL,
            policy_revision INTEGER NOT NULL,
            source_receipt_id TEXT NOT NULL,
            activated_at TEXT,
            PRIMARY KEY (cohort_id, link_id),
            UNIQUE (cohort_id, note_uuid, relative_path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_migration_binding_transitions (
            cohort_id TEXT NOT NULL REFERENCES task_migration_cohorts(cohort_id),
            binding_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            before_authority TEXT NOT NULL,
            before_epoch INTEGER NOT NULL,
            after_authority TEXT NOT NULL,
            after_epoch INTEGER NOT NULL,
            domain_revision TEXT NOT NULL,
            result TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            PRIMARY KEY (cohort_id, binding_id, direction)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_migration_gates (
            cohort_id TEXT NOT NULL REFERENCES task_migration_cohorts(cohort_id),
            gate_name TEXT NOT NULL,
            required INTEGER NOT NULL,
            passed INTEGER NOT NULL,
            evidence_sha256 TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (cohort_id, gate_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_migration_receipts (
            receipt_id TEXT PRIMARY KEY,
            cohort_id TEXT NOT NULL REFERENCES task_migration_cohorts(cohort_id),
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            actor TEXT NOT NULL,
            session_id TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_migration_receipts_cohort "
        "ON task_migration_receipts(cohort_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_local_links_document "
        "ON task_local_file_links(store_id, document_id, created_at, link_id)"
    )


def _m019_document_stage_replay_integrity(conn: sqlite3.Connection) -> None:
    """Persist source receipts needed for exact document-stage replay checks."""

    _add_column(conn, "task_migration_document_stage", "source_receipt_id", "TEXT")
    # Recovery rows already retained this receipt.  Local-link rows also carry
    # it, but only use them when every row for the note agrees.  Rows with no
    # durable historical source remain NULL and are filled exactly once by a
    # verified replay of record_document_stage.
    conn.execute(
        """
        UPDATE task_migration_document_stage
        SET source_receipt_id = (
            SELECT recovered.source_receipt_id
            FROM recovered_task_documents AS recovered
            WHERE recovered.note_uuid = task_migration_document_stage.note_uuid
              AND recovered.store_id = task_migration_document_stage.store_id
              AND recovered.document_id = task_migration_document_stage.document_id
        )
        WHERE source_receipt_id IS NULL
          AND EXISTS (
            SELECT 1 FROM recovered_task_documents AS recovered
            WHERE recovered.note_uuid = task_migration_document_stage.note_uuid
              AND recovered.store_id = task_migration_document_stage.store_id
              AND recovered.document_id = task_migration_document_stage.document_id
          )
        """
    )
    conn.execute(
        """
        UPDATE task_migration_document_stage
        SET source_receipt_id = (
            SELECT MIN(link.source_receipt_id)
            FROM task_migration_local_link_stage AS link
            WHERE link.cohort_id = task_migration_document_stage.cohort_id
              AND link.note_uuid = task_migration_document_stage.note_uuid
        )
        WHERE source_receipt_id IS NULL
          AND 1 = (
            SELECT COUNT(DISTINCT link.source_receipt_id)
            FROM task_migration_local_link_stage AS link
            WHERE link.cohort_id = task_migration_document_stage.cohort_id
              AND link.note_uuid = task_migration_document_stage.note_uuid
          )
        """
    )


class NativeTaskMigrationRunner(MigrationRunner):
    """Migration runner that treats versions 1..11 as an immutable baseline."""

    def _verify_history_hashes(
        self,
        conn: sqlite3.Connection,
        current_version: int,
    ) -> None:
        rows = conn.execute(
            "SELECT version, code_hash, hash_format FROM _migration_history "
            "WHERE version > ? AND version <= ? ORDER BY version",
            (LEGACY_SCHEMA_VERSION, current_version),
        ).fetchall()
        recorded = {int(row[0]): (str(row[1]), row[2]) for row in rows}
        for migration in self.migrations:
            if migration.version <= LEGACY_SCHEMA_VERSION:
                continue
            if migration.version > current_version:
                break
            entry = recorded.get(migration.version)
            if entry is None:
                continue
            stored_hash, stored_format = entry
            current_hash = self._hash_callable(migration.fn)
            if stored_format != HASH_FORMAT_CURRENT:
                conn.execute(
                    "UPDATE _migration_history SET code_hash = ?, hash_format = ? "
                    "WHERE version = ?",
                    (current_hash, HASH_FORMAT_CURRENT, migration.version),
                )
            elif stored_hash != current_hash:
                raise MigrationHashMismatch(
                    f"{self.name}: native migration v{migration.version} was "
                    "applied from different source; add a new migration instead."
                )

    def _infer_baseline_version(self, conn: sqlite3.Connection) -> int:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != '_migration_history'"
            )
        }
        if not tables:
            return 0
        if "task_metadata" not in tables:
            raise RuntimeError("task database contains tables but no task_metadata table")
        columns = _columns(conn, "task_metadata")
        if "revision" in columns and "task_mutation_receipts" in tables:
            if (
                "task_migration_document_stage" in tables
                and "source_receipt_id"
                not in _columns(conn, "task_migration_document_stage")
            ):
                return 18
            return self.target_version
        return LEGACY_SCHEMA_VERSION


TASK_MIGRATIONS = NativeTaskMigrationRunner(
    "task_metadata",
    migrations=[
        Migration(1, "bootstrap historical v11 schema", _m001_bootstrap_v11),
        *[
            Migration(version, f"historical v{version} included in bootstrap", _historical_noop)
            for version in range(2, LEGACY_SCHEMA_VERSION + 1)
        ],
        Migration(12, "native lifecycle and CAS columns", _m012_native_columns),
        Migration(13, "task document and recovery catalogs", _m013_document_catalogs),
        Migration(14, "metadata-only local file catalog", _m014_local_file_catalog),
        Migration(15, "mutation receipts, revision, outbox, and epoch state", _m015_mutation_infrastructure),
        Migration(16, "native task history enrichment", _m016_history_enrichment),
        Migration(17, "summary and dependency authoring fields", _m017_authoring_fields),
        Migration(18, "legacy task import and cutover cohort ledger", _m018_legacy_cutover_ledger),
        Migration(19, "document-stage replay integrity", _m019_document_stage_replay_integrity),
    ],
)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a task connection to the native schema."""
    TASK_MIGRATIONS.run(conn)
