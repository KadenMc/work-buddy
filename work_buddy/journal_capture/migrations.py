"""Versioned migrations for the native Journal domain store.

Versions 1 through 7 adopt the historical inline schema that predates
``PRAGMA user_version``.  The adoption runner reads the old
``journal_meta.schema_version`` marker, stamps only the versions that were
actually present, and then applies the remaining idempotent steps.  Native
Journal concepts begin at v8.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from work_buddy.storage.migrations import (
    HASH_FORMAT_CURRENT,
    Migration,
    MigrationHashMismatch,
    MigrationRunner,
)


LEGACY_SCHEMA_VERSION = 7


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != '_migration_history'"
        )
    }


def _set_legacy_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO journal_meta(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )


def _add_column(
    conn: sqlite3.Connection,
    table: str,
    name: str,
    declaration: str,
) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _m001_bootstrap_historical_v7(conn: sqlite3.Connection) -> None:
    """Create the complete historical v7 shape for a fresh database."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS journal_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS journal_captures (
            capture_id TEXT PRIMARY KEY,
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            representation_id TEXT NOT NULL,
            submission_id TEXT NOT NULL UNIQUE,
            command_id TEXT NOT NULL UNIQUE,
            source_effect_id TEXT NOT NULL UNIQUE,
            source_usage_id TEXT,
            day_id TEXT NOT NULL,
            requested_target TEXT NOT NULL,
            resolved_target TEXT,
            mode TEXT NOT NULL,
            input_mode TEXT NOT NULL,
            stated_at TEXT,
            submitted_at TEXT NOT NULL,
            persistence_status TEXT NOT NULL DEFAULT 'persisted',
            processing_status TEXT NOT NULL,
            processing_error_code TEXT,
            annotation_json TEXT,
            entry_id TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (requested_target IN ('auto','log','running_notes')),
            CHECK (resolved_target IS NULL OR resolved_target IN ('log','running_notes')),
            CHECK (mode IN ('dumb','smart')),
            CHECK (persistence_status = 'persisted'),
            CHECK (processing_status IN (
                'not_requested','pending','running','succeeded','failed'
            ))
        );

        CREATE TABLE IF NOT EXISTS journal_entries (
            entry_id TEXT PRIMARY KEY,
            capture_id TEXT NOT NULL UNIQUE REFERENCES journal_captures(capture_id),
            day_id TEXT NOT NULL,
            entry_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            markdown TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            resolution_state TEXT NOT NULL DEFAULT 'open',
            processing_status TEXT NOT NULL,
            annotation_json TEXT,
            processing_error_code TEXT,
            projection_state TEXT NOT NULL DEFAULT 'pending',
            projection_marker TEXT NOT NULL UNIQUE,
            projection_base_sha256 TEXT,
            projection_result_sha256 TEXT,
            CHECK (entry_kind IN ('log','running_notes')),
            CHECK (processing_status IN (
                'not_requested','pending','running','succeeded','failed'
            )),
            CHECK (projection_state IN (
                'pending','prepared','committed','failed','paused_diverged'
            ))
        );

        CREATE TABLE IF NOT EXISTS journal_effects (
            effect_id TEXT PRIMARY KEY,
            capture_id TEXT NOT NULL REFERENCES journal_captures(capture_id),
            effect_type TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            authorization_fingerprint TEXT NOT NULL,
            authorization_expires_at TEXT,
            lease_owner TEXT,
            lease_expires_at TEXT,
            error_code TEXT,
            payload_json TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(capture_id, effect_type),
            CHECK (state IN ('pending','running','succeeded','failed','paused'))
        );

        CREATE TABLE IF NOT EXISTS journal_note_tombstones (
            entry_id TEXT PRIMARY KEY,
            capture_id TEXT NOT NULL,
            item_json TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            deleted_version INTEGER NOT NULL,
            deleted_by_json TEXT NOT NULL,
            reason TEXT NOT NULL CHECK(reason = 'user_deleted')
        );

        CREATE TABLE IF NOT EXISTS journal_source_redactions (
            redaction_event_id TEXT PRIMARY KEY,
            source_effect_id TEXT NOT NULL UNIQUE,
            source_usage_id TEXT NOT NULL UNIQUE,
            source_ref TEXT NOT NULL,
            capture_id TEXT,
            entry_id TEXT,
            redaction_epoch INTEGER NOT NULL,
            result_sha256 TEXT NOT NULL,
            completed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS journal_document_bindings (
            entry_id TEXT PRIMARY KEY REFERENCES journal_entries(entry_id),
            binding_id TEXT NOT NULL UNIQUE,
            store_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            change_id TEXT NOT NULL,
            source_consumer_id TEXT NOT NULL UNIQUE,
            source_usage_id TEXT NOT NULL UNIQUE,
            source_use_kind TEXT NOT NULL DEFAULT 'exact_insertion',
            source_disclosure_kind TEXT NOT NULL DEFAULT 'exact_readable_copy',
            source_redaction_policy TEXT NOT NULL DEFAULT 'scrub',
            source_maintenance_state TEXT NOT NULL DEFAULT 'clean',
            source_maintenance_json TEXT NOT NULL DEFAULT '{}',
            cowork_href TEXT NOT NULL,
            content_authority_epoch INTEGER NOT NULL,
            entry_version INTEGER NOT NULL,
            inspection_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'current',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (content_authority_epoch >= 1),
            CHECK (state IN ('current','paused_diverged','retired')),
            CHECK (source_maintenance_state IN ('clean','review_required'))
        );

        CREATE TABLE IF NOT EXISTS journal_document_usage_transitions (
            transition_id TEXT PRIMARY KEY,
            entry_id TEXT NOT NULL UNIQUE REFERENCES journal_document_bindings(entry_id),
            binding_id TEXT NOT NULL UNIQUE,
            change_id TEXT NOT NULL,
            prior_usage_id TEXT NOT NULL UNIQUE,
            next_usage_id TEXT NOT NULL UNIQUE,
            next_use_kind TEXT NOT NULL,
            next_disclosure_kind TEXT NOT NULL,
            next_redaction_policy TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'mirror_updated',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (state IN ('mirror_updated','complete'))
        );

        CREATE TABLE IF NOT EXISTS journal_mutations (
            client_mutation_id TEXT PRIMARY KEY,
            request_sha256 TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS journal_content_migrations (
            entity_kind TEXT NOT NULL CHECK(entity_kind IN (
                'running_note','logical_day_log'
            )),
            entity_id TEXT NOT NULL,
            day_id TEXT NOT NULL,
            marker_id TEXT NOT NULL UNIQUE,
            selection_start INTEGER,
            selection_end INTEGER,
            selected_file_sha256 TEXT,
            selected_section_sha256 TEXT,
            source_ref TEXT,
            representation_id TEXT,
            source_content_sha256 TEXT,
            binding_id TEXT UNIQUE,
            store_id TEXT,
            document_id TEXT,
            comparison_state TEXT NOT NULL DEFAULT 'pending' CHECK(
                comparison_state IN ('pending','parity','mismatch')
            ),
            byte_parity INTEGER,
            normalized_parity INTEGER,
            structural_parity INTEGER,
            rollback_deadline TEXT,
            mirrored_state TEXT NOT NULL DEFAULT 'selected' CHECK(
                mirrored_state IN (
                    'selected','shadow_imported','cowork_authoritative',
                    'legacy_authoritative','paused_diverged','retired'
                )
            ),
            mirrored_authority_epoch INTEGER NOT NULL DEFAULT 0 CHECK(
                mirrored_authority_epoch >= 0
            ),
            projection_state TEXT NOT NULL DEFAULT 'none' CHECK(
                projection_state IN (
                    'none','pending','committed','paused_diverged','failed'
                )
            ),
            divergence_source_ref TEXT,
            operation_id TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(entity_kind,entity_id),
            CHECK (
                (selection_start IS NULL AND selection_end IS NULL)
                OR (selection_start >= 0 AND selection_end > selection_start)
            )
        );

        CREATE TABLE IF NOT EXISTS journal_migration_operations (
            operation_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            action TEXT NOT NULL CHECK(action IN (
                'select','shadow_import','cutover','rollback','reconcile'
            )),
            entity_kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'prepared','document_committed','epoch_committed',
                'projection_committed','completed','recoverable','paused_diverged'
            )),
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS journal_exit_evidence (
            receipt_id TEXT PRIMARY KEY,
            inventory_sha256 TEXT NOT NULL,
            callsite_inventory_sha256 TEXT NOT NULL,
            authority_summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS journal_captures_day_idx
            ON journal_captures(day_id, submitted_at DESC);
        CREATE INDEX IF NOT EXISTS journal_entries_day_idx
            ON journal_entries(day_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS journal_effects_state_idx
            ON journal_effects(state, updated_at);
        CREATE INDEX IF NOT EXISTS journal_migration_day_idx
            ON journal_content_migrations(day_id,entity_kind,entity_id);
        CREATE UNIQUE INDEX IF NOT EXISTS journal_running_selection_idx
            ON journal_content_migrations(
                day_id,selection_start,selection_end,selected_section_sha256
            ) WHERE entity_kind='running_note';
        CREATE INDEX IF NOT EXISTS journal_migration_recovery_idx
            ON journal_migration_operations(state,updated_at);
        """
    )
    _set_legacy_version(conn, 1)


def _m002_source_usage(conn: sqlite3.Connection) -> None:
    _add_column(conn, "journal_captures", "source_usage_id", "TEXT")
    _set_legacy_version(conn, 2)


def _m003_historical_noop(conn: sqlite3.Connection) -> None:
    _set_legacy_version(conn, 3)


def _m004_document_dependency_metadata(conn: sqlite3.Connection) -> None:
    additions = (
        ("source_use_kind", "TEXT NOT NULL DEFAULT 'exact_insertion'"),
        ("source_disclosure_kind", "TEXT NOT NULL DEFAULT 'exact_readable_copy'"),
        ("source_redaction_policy", "TEXT NOT NULL DEFAULT 'scrub'"),
        ("source_maintenance_state", "TEXT NOT NULL DEFAULT 'clean'"),
        ("source_maintenance_json", "TEXT NOT NULL DEFAULT '{}'"),
    )
    for name, declaration in additions:
        _add_column(conn, "journal_document_bindings", name, declaration)
    _set_legacy_version(conn, 4)


def _m005_migration_ledgers(conn: sqlite3.Connection) -> None:
    # The bootstrap DDL is idempotent and creates the v5 ledger/evidence tables
    # for a historical database that predates them.
    _m001_bootstrap_historical_v7(conn)
    _set_legacy_version(conn, 5)


def _m006_structural_parity(conn: sqlite3.Connection) -> None:
    _add_column(conn, "journal_content_migrations", "structural_parity", "INTEGER")
    _set_legacy_version(conn, 6)


def _m007_effect_receipts(conn: sqlite3.Connection) -> None:
    _add_column(conn, "journal_effects", "payload_json", "TEXT")
    _add_column(conn, "journal_effects", "result_json", "TEXT")
    _set_legacy_version(conn, 7)


def _immutable_triggers(conn: sqlite3.Connection, tables: tuple[str, ...]) -> None:
    for table in tables:
        conn.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_immutable_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table}_is_immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_immutable_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table}_is_immutable');
            END;
            """
        )


def _m008_immutable_configuration(conn: sqlite3.Connection) -> None:
    """Add immutable interaction, function, field, module, and profile revisions."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS journal_interaction_behavior_revisions (
            behavior_id TEXT NOT NULL,
            behavior_version INTEGER NOT NULL CHECK(behavior_version >= 1),
            definition_json TEXT NOT NULL,
            definition_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            supersedes_version INTEGER,
            PRIMARY KEY(behavior_id, behavior_version)
        );

        CREATE TABLE IF NOT EXISTS journal_function_contract_revisions (
            function_id TEXT NOT NULL,
            function_version INTEGER NOT NULL CHECK(function_version >= 1),
            value_kind TEXT NOT NULL,
            unit TEXT,
            cardinality TEXT NOT NULL CHECK(cardinality IN ('single','multiple')),
            definition_json TEXT NOT NULL,
            definition_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            supersedes_version INTEGER,
            PRIMARY KEY(function_id, function_version)
        );

        CREATE TABLE IF NOT EXISTS journal_module_type_revisions (
            module_type_id TEXT NOT NULL,
            module_type_version INTEGER NOT NULL CHECK(module_type_version >= 1),
            definition_json TEXT NOT NULL,
            definition_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            supersedes_version INTEGER,
            PRIMARY KEY(module_type_id, module_type_version)
        );

        CREATE TABLE IF NOT EXISTS journal_field_definition_versions (
            field_id TEXT NOT NULL,
            definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
            owner TEXT NOT NULL,
            stable_key TEXT NOT NULL,
            label TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            value_kind TEXT NOT NULL CHECK(value_kind IN (
                'short_text','long_text','number','scale','boolean','single_select',
                'multi_select','local_time','instant','date','duration',
                'entity_reference','reference'
            )),
            unit TEXT,
            constraints_json TEXT NOT NULL DEFAULT '{}',
            value_codec_version INTEGER NOT NULL CHECK(value_codec_version >= 1),
            function_id TEXT,
            function_version INTEGER,
            behavior_id TEXT NOT NULL,
            behavior_version INTEGER NOT NULL,
            privacy_class TEXT NOT NULL CHECK(privacy_class IN (
                'private','sensitive','internal'
            )),
            search_mode TEXT NOT NULL CHECK(search_mode IN (
                'structured_only','lexical','dense','lexical_dense','excluded'
            )),
            disclosure_policy_id TEXT NOT NULL,
            definition_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            supersedes_version INTEGER,
            PRIMARY KEY(field_id, definition_version),
            UNIQUE(owner, stable_key, definition_version),
            FOREIGN KEY(function_id, function_version) REFERENCES
                journal_function_contract_revisions(function_id, function_version),
            FOREIGN KEY(behavior_id, behavior_version) REFERENCES
                journal_interaction_behavior_revisions(behavior_id, behavior_version),
            CHECK ((function_id IS NULL) = (function_version IS NULL))
        );

        CREATE TABLE IF NOT EXISTS journal_prompt_definition_versions (
            prompt_id TEXT NOT NULL,
            prompt_version INTEGER NOT NULL CHECK(prompt_version >= 1),
            field_id TEXT,
            field_definition_version INTEGER,
            wording TEXT NOT NULL,
            help_text TEXT NOT NULL DEFAULT '',
            requiredness TEXT NOT NULL CHECK(requiredness IN (
                'required','optional','conditional'
            )),
            schedule_kind TEXT NOT NULL CHECK(schedule_kind IN (
                'always','weekdays','date_range','manual_only'
            )),
            schedule_json TEXT NOT NULL DEFAULT '{}',
            disposition_policy_json TEXT NOT NULL DEFAULT '{}',
            definition_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            supersedes_version INTEGER,
            PRIMARY KEY(prompt_id, prompt_version),
            FOREIGN KEY(field_id, field_definition_version) REFERENCES
                journal_field_definition_versions(field_id, definition_version),
            CHECK ((field_id IS NULL) = (field_definition_version IS NULL))
        );

        CREATE TABLE IF NOT EXISTS journal_marker_definition_versions (
            marker_id TEXT NOT NULL,
            marker_version INTEGER NOT NULL CHECK(marker_version >= 1),
            matcher_kind TEXT NOT NULL CHECK(matcher_kind IN ('exact','bounded_regex')),
            matcher_value TEXT NOT NULL,
            field_id TEXT NOT NULL,
            field_definition_version INTEGER NOT NULL,
            prompt_id TEXT,
            prompt_version INTEGER,
            effective_start_date TEXT,
            effective_end_date TEXT,
            import_only INTEGER NOT NULL DEFAULT 1 CHECK(import_only IN (0,1)),
            definition_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(marker_id, marker_version),
            FOREIGN KEY(field_id, field_definition_version) REFERENCES
                journal_field_definition_versions(field_id, definition_version),
            FOREIGN KEY(prompt_id, prompt_version) REFERENCES
                journal_prompt_definition_versions(prompt_id, prompt_version),
            CHECK ((prompt_id IS NULL) = (prompt_version IS NULL))
        );

        CREATE TABLE IF NOT EXISTS journal_profile_revisions (
            profile_id TEXT NOT NULL,
            profile_revision INTEGER NOT NULL CHECK(profile_revision >= 1),
            format_version INTEGER NOT NULL CHECK(format_version >= 1),
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            canonical_order_json TEXT NOT NULL,
            pack_id TEXT,
            pack_version INTEGER,
            pack_digest TEXT,
            profile_digest TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            supersedes_revision INTEGER,
            PRIMARY KEY(profile_id, profile_revision),
            UNIQUE(profile_digest)
        );

        CREATE TABLE IF NOT EXISTS journal_module_instance_versions (
            module_instance_id TEXT NOT NULL,
            instance_version INTEGER NOT NULL CHECK(instance_version >= 1),
            module_type_id TEXT NOT NULL,
            module_type_version INTEGER NOT NULL,
            label TEXT NOT NULL,
            settings_schema_version INTEGER NOT NULL CHECK(settings_schema_version >= 1),
            settings_json TEXT NOT NULL DEFAULT '{}',
            settings_sha256 TEXT NOT NULL,
            behavior_id TEXT,
            behavior_version INTEGER,
            schedule_kind TEXT NOT NULL CHECK(schedule_kind IN (
                'always','weekdays','date_range','manual_only'
            )),
            schedule_json TEXT NOT NULL DEFAULT '{}',
            reveal_policy_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            supersedes_version INTEGER,
            PRIMARY KEY(module_instance_id, instance_version),
            FOREIGN KEY(module_type_id, module_type_version) REFERENCES
                journal_module_type_revisions(module_type_id, module_type_version),
            FOREIGN KEY(behavior_id, behavior_version) REFERENCES
                journal_interaction_behavior_revisions(behavior_id, behavior_version),
            CHECK ((behavior_id IS NULL) = (behavior_version IS NULL))
        );

        CREATE TABLE IF NOT EXISTS journal_profile_module_slots (
            profile_id TEXT NOT NULL,
            profile_revision INTEGER NOT NULL,
            slot_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            module_instance_id TEXT NOT NULL,
            module_instance_version INTEGER NOT NULL,
            required INTEGER NOT NULL DEFAULT 0 CHECK(required IN (0,1)),
            PRIMARY KEY(profile_id, profile_revision, slot_id),
            UNIQUE(profile_id, profile_revision, ordinal),
            FOREIGN KEY(profile_id, profile_revision) REFERENCES
                journal_profile_revisions(profile_id, profile_revision),
            FOREIGN KEY(module_instance_id, module_instance_version) REFERENCES
                journal_module_instance_versions(module_instance_id, instance_version)
        );

        CREATE TABLE IF NOT EXISTS journal_module_field_slots (
            module_instance_id TEXT NOT NULL,
            module_instance_version INTEGER NOT NULL,
            slot_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            field_id TEXT NOT NULL,
            field_definition_version INTEGER NOT NULL,
            prompt_id TEXT,
            prompt_version INTEGER,
            PRIMARY KEY(module_instance_id, module_instance_version, slot_id),
            FOREIGN KEY(module_instance_id, module_instance_version) REFERENCES
                journal_module_instance_versions(module_instance_id, instance_version),
            FOREIGN KEY(field_id, field_definition_version) REFERENCES
                journal_field_definition_versions(field_id, definition_version),
            FOREIGN KEY(prompt_id, prompt_version) REFERENCES
                journal_prompt_definition_versions(prompt_id, prompt_version),
            CHECK ((prompt_id IS NULL) = (prompt_version IS NULL))
        );

        CREATE TABLE IF NOT EXISTS journal_profile_activation_epochs (
            activation_revision INTEGER PRIMARY KEY CHECK(activation_revision >= 1),
            profile_id TEXT NOT NULL,
            profile_revision INTEGER NOT NULL,
            profile_digest TEXT NOT NULL,
            effective_local_date TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id, profile_revision) REFERENCES
                journal_profile_revisions(profile_id, profile_revision)
        );
        CREATE INDEX IF NOT EXISTS journal_profile_activation_date_idx
            ON journal_profile_activation_epochs(effective_local_date, activation_revision);
        """
    )
    _immutable_triggers(
        conn,
        (
            "journal_interaction_behavior_revisions",
            "journal_function_contract_revisions",
            "journal_module_type_revisions",
            "journal_field_definition_versions",
            "journal_prompt_definition_versions",
            "journal_marker_definition_versions",
            "journal_profile_revisions",
            "journal_module_instance_versions",
            "journal_profile_module_slots",
            "journal_module_field_slots",
            "journal_profile_activation_epochs",
        ),
    )
    _seed_simple_profile(conn)
    _set_legacy_version(conn, 8)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _seed_simple_profile(conn: sqlite3.Connection) -> None:
    """Seed the removable, prose-neutral Simple Journal starting profile."""

    now = "1970-01-01T00:00:00+00:00"
    behavior = {
        "aiContribution": "forbidden",
        "aiRead": "policy_controlled",
        "bodyMode": "plain_value",
        "profile": "human_value/v1",
        "truthEligibility": "unsupported",
    }
    conn.execute(
        "INSERT OR IGNORE INTO journal_interaction_behavior_revisions "
        "(behavior_id,behavior_version,definition_json,definition_sha256,created_at) "
        "VALUES(?,?,?,?,?)",
        ("human_value", 1, _canonical(behavior), _digest(behavior), now),
    )
    module_types = {
        "capture": {"family": "capture", "multiplicity": "multiple"},
        "day_stream": {"family": "day_stream", "multiplicity": "single"},
        "record_collection": {"family": "record_collection", "multiplicity": "multiple"},
        "field_group": {"family": "field_group", "multiplicity": "multiple"},
        "prompt_result": {"family": "prompt_result", "multiplicity": "multiple"},
        "generated_projection": {
            "family": "generated_projection",
            "multiplicity": "multiple",
        },
        "document": {"family": "document", "multiplicity": "multiple"},
        "relations": {"family": "relations", "multiplicity": "multiple"},
    }
    for module_type_id, definition in module_types.items():
        conn.execute(
            "INSERT OR IGNORE INTO journal_module_type_revisions "
            "(module_type_id,module_type_version,definition_json,definition_sha256,created_at) "
            "VALUES(?,?,?,?,?)",
            (module_type_id, 1, _canonical(definition), _digest(definition), now),
        )
    instances = (
        ("simple.capture", "capture", "Quick Capture", {}),
        ("simple.stream", "day_stream", "Day Stream", {}),
        ("simple.notes", "record_collection", "Notes", {"destinationId": "notes"}),
    )
    for instance_id, module_type, label, settings in instances:
        conn.execute(
            "INSERT OR IGNORE INTO journal_module_instance_versions "
            "(module_instance_id,instance_version,module_type_id,module_type_version,label,"
            "settings_schema_version,settings_json,settings_sha256,behavior_id,behavior_version,"
            "schedule_kind,schedule_json,reveal_policy_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                instance_id,
                1,
                module_type,
                1,
                label,
                1,
                _canonical(settings),
                _digest(settings),
                "human_value",
                1,
                "always",
                "{}",
                "{}",
                now,
            ),
        )
    canonical_order = ["capture", "day-stream", "notes"]
    profile_payload = {
        "formatVersion": 1,
        "modules": [item[0] for item in instances],
        "order": canonical_order,
    }
    profile_digest = _digest(profile_payload)
    conn.execute(
        "INSERT OR IGNORE INTO journal_profile_revisions "
        "(profile_id,profile_revision,format_version,name,description,canonical_order_json,"
        "pack_id,pack_version,pack_digest,profile_digest,created_by,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "simple-journal",
            1,
            1,
            "Simple Journal",
            "A removable quick-capture, day-stream, and notes starting profile.",
            _canonical(canonical_order),
            "simple-journal",
            1,
            _digest({"pack": "simple-journal", "version": 1}),
            profile_digest,
            "work-buddy",
            now,
        ),
    )
    for ordinal, (slot_id, instance) in enumerate(
        zip(canonical_order, ("simple.capture", "simple.stream", "simple.notes"), strict=True)
    ):
        conn.execute(
            "INSERT OR IGNORE INTO journal_profile_module_slots "
            "(profile_id,profile_revision,slot_id,ordinal,module_instance_id,"
            "module_instance_version,required) VALUES(?,?,?,?,?,?,0)",
            ("simple-journal", 1, slot_id, ordinal, instance, 1),
        )
    conn.execute(
        "INSERT OR IGNORE INTO journal_profile_activation_epochs "
        "(activation_revision,profile_id,profile_revision,profile_digest,effective_local_date,"
        "actor_json,client_mutation_id,request_sha256,activated_at) "
        "VALUES(1,?,?,?,?,?,?,?,?)",
        (
            "simple-journal",
            1,
            profile_digest,
            "0001-01-01",
            _canonical({"kind": "system", "subject": "work-buddy"}),
            "bootstrap:simple-journal/v1",
            _digest(profile_payload),
            now,
        ),
    )


def _m009_day_compositions(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS journal_days (
            day_id TEXT PRIMARY KEY,
            local_date TEXT NOT NULL UNIQUE,
            timezone TEXT NOT NULL,
            boundary TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            boundary_policy_revision TEXT,
            lifecycle TEXT NOT NULL DEFAULT 'current' CHECK(lifecycle IN (
                'current','archived','tombstoned'
            )),
            current_revision INTEGER NOT NULL DEFAULT 1 CHECK(current_revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS journal_day_overrides (
            override_id TEXT PRIMARY KEY,
            local_date TEXT NOT NULL,
            override_revision INTEGER NOT NULL CHECK(override_revision >= 1),
            base_profile_id TEXT NOT NULL,
            base_profile_revision INTEGER NOT NULL,
            modules_json TEXT NOT NULL,
            override_digest TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(local_date, override_revision),
            FOREIGN KEY(base_profile_id, base_profile_revision) REFERENCES
                journal_profile_revisions(profile_id, profile_revision)
        );

        CREATE TABLE IF NOT EXISTS journal_day_composition_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            day_id TEXT NOT NULL UNIQUE REFERENCES journal_days(day_id),
            snapshot_version INTEGER NOT NULL DEFAULT 1 CHECK(snapshot_version >= 1),
            profile_id TEXT NOT NULL,
            profile_revision INTEGER NOT NULL,
            profile_digest TEXT NOT NULL,
            activation_revision INTEGER NOT NULL,
            override_id TEXT,
            composition_digest TEXT NOT NULL,
            search_recipe_version INTEGER NOT NULL DEFAULT 1,
            schedule_timezone TEXT NOT NULL,
            schedule_window_start TEXT NOT NULL,
            schedule_window_end TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(profile_id, profile_revision) REFERENCES
                journal_profile_revisions(profile_id, profile_revision),
            FOREIGN KEY(activation_revision) REFERENCES journal_profile_activation_epochs(
                activation_revision
            ),
            FOREIGN KEY(override_id) REFERENCES journal_day_overrides(override_id)
        );

        CREATE TABLE IF NOT EXISTS journal_day_composition_modules (
            snapshot_id TEXT NOT NULL REFERENCES journal_day_composition_snapshots(snapshot_id),
            slot_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            module_instance_id TEXT NOT NULL,
            module_instance_version INTEGER NOT NULL,
            module_type_id TEXT NOT NULL,
            module_type_version INTEGER NOT NULL,
            semantic_membership TEXT NOT NULL CHECK(semantic_membership IN (
                'included','excluded_by_schedule','unavailable'
            )),
            schedule_kind TEXT NOT NULL,
            schedule_evidence_json TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, slot_id),
            UNIQUE(snapshot_id, ordinal)
        );

        CREATE TABLE IF NOT EXISTS journal_day_composition_fields (
            snapshot_id TEXT NOT NULL REFERENCES journal_day_composition_snapshots(snapshot_id),
            composition_slot_id TEXT NOT NULL,
            module_slot_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            field_id TEXT NOT NULL,
            field_definition_version INTEGER NOT NULL,
            prompt_id TEXT,
            prompt_version INTEGER,
            PRIMARY KEY(snapshot_id, composition_slot_id),
            FOREIGN KEY(field_id, field_definition_version) REFERENCES
                journal_field_definition_versions(field_id, definition_version),
            FOREIGN KEY(prompt_id, prompt_version) REFERENCES
                journal_prompt_definition_versions(prompt_id, prompt_version),
            CHECK ((prompt_id IS NULL) = (prompt_version IS NULL))
        );
        """
    )
    _immutable_triggers(
        conn,
        (
            "journal_day_overrides",
            "journal_day_composition_snapshots",
            "journal_day_composition_modules",
            "journal_day_composition_fields",
        ),
    )
    _set_legacy_version(conn, 9)


def _m010_native_content(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS journal_items (
            item_id TEXT PRIMARY KEY,
            local_date TEXT NOT NULL,
            day_id TEXT,
            composition_snapshot_id TEXT,
            module_instance_id TEXT,
            module_instance_version INTEGER,
            item_kind TEXT NOT NULL CHECK(item_kind IN (
                'record','running_note','generated_artifact','prompt_input','prompt_result'
            )),
            classification_id TEXT,
            authority_kind TEXT NOT NULL CHECK(authority_kind IN (
                'legacy_entry','native_plain','cowork_document','generated'
            )),
            legacy_entry_id TEXT UNIQUE REFERENCES journal_entries(entry_id),
            current_plain_value TEXT,
            current_content_sha256 TEXT,
            interaction_behavior_id TEXT NOT NULL,
            interaction_behavior_version INTEGER NOT NULL,
            privacy_class TEXT NOT NULL CHECK(privacy_class IN (
                'private','sensitive','internal'
            )),
            search_mode TEXT NOT NULL CHECK(search_mode IN (
                'structured_only','lexical','dense','lexical_dense','excluded'
            )),
            source_ref TEXT,
            lifecycle TEXT NOT NULL DEFAULT 'current' CHECK(lifecycle IN (
                'current','resolved','archived','tombstoned','superseded'
            )),
            current_revision INTEGER NOT NULL DEFAULT 1 CHECK(current_revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(day_id) REFERENCES journal_days(day_id),
            FOREIGN KEY(composition_snapshot_id) REFERENCES
                journal_day_composition_snapshots(snapshot_id),
            FOREIGN KEY(interaction_behavior_id, interaction_behavior_version) REFERENCES
                journal_interaction_behavior_revisions(behavior_id, behavior_version),
            CHECK (
                (authority_kind = 'legacy_entry' AND legacy_entry_id IS NOT NULL
                    AND current_plain_value IS NULL AND current_content_sha256 IS NULL)
                OR
                (authority_kind IN ('native_plain','generated') AND legacy_entry_id IS NULL
                    AND current_plain_value IS NOT NULL AND current_content_sha256 IS NOT NULL)
                OR
                (authority_kind = 'cowork_document' AND legacy_entry_id IS NULL
                    AND current_plain_value IS NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS journal_items_date_idx
            ON journal_items(local_date, lifecycle, created_at, item_id);

        CREATE TABLE IF NOT EXISTS journal_item_revisions (
            item_id TEXT NOT NULL REFERENCES journal_items(item_id),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            authority_kind TEXT NOT NULL,
            plain_value TEXT,
            content_sha256 TEXT,
            lifecycle TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            source_ref TEXT,
            authorship TEXT NOT NULL CHECK(authorship IN (
                'human','ai','mixed','unknown','generated'
            )),
            review_state TEXT NOT NULL CHECK(review_state IN (
                'not_applicable','unreviewed','reviewed','rejected'
            )),
            intent_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(item_id, revision)
        );

        CREATE TABLE IF NOT EXISTS journal_relations (
            relation_id TEXT PRIMARY KEY,
            source_item_id TEXT NOT NULL REFERENCES journal_items(item_id),
            relation_kind TEXT NOT NULL,
            target_domain TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target_revision TEXT,
            lifecycle TEXT NOT NULL DEFAULT 'current' CHECK(lifecycle IN (
                'current','archived','tombstoned'
            )),
            revision INTEGER NOT NULL DEFAULT 1,
            actor_json TEXT NOT NULL,
            source_ref TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_item_id, relation_kind, target_domain, target_id)
        );

        CREATE TABLE IF NOT EXISTS journal_relation_revisions (
            relation_id TEXT NOT NULL REFERENCES journal_relations(relation_id),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            relation_kind TEXT NOT NULL,
            target_domain TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target_revision TEXT,
            lifecycle TEXT NOT NULL CHECK(lifecycle IN (
                'current','archived','tombstoned'
            )),
            actor_json TEXT NOT NULL,
            source_ref TEXT,
            intent_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(relation_id, revision)
        );

        CREATE TABLE IF NOT EXISTS journal_field_values (
            value_id TEXT PRIMARY KEY,
            local_date TEXT NOT NULL,
            day_id TEXT,
            composition_snapshot_id TEXT,
            composition_slot_id TEXT,
            module_instance_id TEXT NOT NULL,
            module_instance_version INTEGER NOT NULL,
            field_id TEXT NOT NULL,
            field_definition_version INTEGER NOT NULL,
            prompt_id TEXT,
            prompt_version INTEGER,
            value_codec_version INTEGER NOT NULL,
            value_kind TEXT NOT NULL,
            disposition TEXT CHECK(disposition IN ('missing','skipped','declined')),
            text_value TEXT,
            number_value REAL,
            boolean_value INTEGER CHECK(boolean_value IN (0,1)),
            temporal_value TEXT,
            duration_seconds INTEGER CHECK(duration_seconds >= 0),
            option_value TEXT,
            collection_present INTEGER NOT NULL DEFAULT 0 CHECK(collection_present IN (0,1)),
            interaction_ref TEXT,
            source_ref TEXT,
            authorship TEXT NOT NULL CHECK(authorship IN (
                'human','ai','mixed','unknown','generated'
            )),
            review_state TEXT NOT NULL CHECK(review_state IN (
                'not_applicable','unreviewed','reviewed','rejected'
            )),
            observed_at TEXT,
            stated_at TEXT,
            ingested_at TEXT NOT NULL,
            lifecycle TEXT NOT NULL DEFAULT 'current' CHECK(lifecycle IN (
                'current','archived','tombstoned'
            )),
            current_revision INTEGER NOT NULL DEFAULT 1 CHECK(current_revision >= 1),
            updated_at TEXT NOT NULL,
            FOREIGN KEY(day_id) REFERENCES journal_days(day_id),
            FOREIGN KEY(composition_snapshot_id) REFERENCES
                journal_day_composition_snapshots(snapshot_id),
            FOREIGN KEY(field_id, field_definition_version) REFERENCES
                journal_field_definition_versions(field_id, definition_version),
            FOREIGN KEY(prompt_id, prompt_version) REFERENCES
                journal_prompt_definition_versions(prompt_id, prompt_version),
            CHECK ((prompt_id IS NULL) = (prompt_version IS NULL)),
            CHECK (
                (disposition IS NOT NULL
                    AND text_value IS NULL AND number_value IS NULL
                    AND boolean_value IS NULL AND temporal_value IS NULL
                    AND duration_seconds IS NULL AND option_value IS NULL
                    AND collection_present = 0)
                OR
                (disposition IS NULL AND (
                    (value_kind IN ('short_text','long_text') AND text_value IS NOT NULL
                        AND number_value IS NULL AND boolean_value IS NULL
                        AND temporal_value IS NULL AND duration_seconds IS NULL
                        AND option_value IS NULL AND collection_present = 0)
                    OR (value_kind IN ('number','scale') AND number_value IS NOT NULL
                        AND text_value IS NULL AND boolean_value IS NULL
                        AND temporal_value IS NULL AND duration_seconds IS NULL
                        AND option_value IS NULL AND collection_present = 0)
                    OR (value_kind = 'boolean' AND boolean_value IS NOT NULL
                        AND text_value IS NULL AND number_value IS NULL
                        AND temporal_value IS NULL AND duration_seconds IS NULL
                        AND option_value IS NULL AND collection_present = 0)
                    OR (value_kind IN ('local_time','instant','date')
                        AND temporal_value IS NOT NULL AND text_value IS NULL
                        AND number_value IS NULL AND boolean_value IS NULL
                        AND duration_seconds IS NULL AND option_value IS NULL
                        AND collection_present = 0)
                    OR (value_kind = 'duration' AND duration_seconds IS NOT NULL
                        AND text_value IS NULL AND number_value IS NULL
                        AND boolean_value IS NULL AND temporal_value IS NULL
                        AND option_value IS NULL AND collection_present = 0)
                    OR (value_kind = 'single_select' AND option_value IS NOT NULL
                        AND text_value IS NULL AND number_value IS NULL
                        AND boolean_value IS NULL AND temporal_value IS NULL
                        AND duration_seconds IS NULL AND collection_present = 0)
                    OR (value_kind IN ('multi_select','entity_reference','reference')
                        AND collection_present = 1 AND text_value IS NULL
                        AND number_value IS NULL AND boolean_value IS NULL
                        AND temporal_value IS NULL AND duration_seconds IS NULL
                        AND option_value IS NULL)
                ))
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS journal_field_values_current_slot_idx
            ON journal_field_values(local_date,module_instance_id,field_id,composition_slot_id)
            WHERE lifecycle='current';

        CREATE TABLE IF NOT EXISTS journal_field_value_options (
            value_id TEXT NOT NULL REFERENCES journal_field_values(value_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            option_id TEXT NOT NULL,
            PRIMARY KEY(value_id, ordinal),
            UNIQUE(value_id, option_id)
        );

        CREATE TABLE IF NOT EXISTS journal_field_value_references (
            value_id TEXT NOT NULL REFERENCES journal_field_values(value_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            reference_kind TEXT NOT NULL,
            reference_id TEXT NOT NULL,
            reference_revision TEXT,
            PRIMARY KEY(value_id, ordinal),
            UNIQUE(value_id, reference_kind, reference_id)
        );

        CREATE TABLE IF NOT EXISTS journal_field_value_revisions (
            value_id TEXT NOT NULL REFERENCES journal_field_values(value_id),
            revision INTEGER NOT NULL,
            value_json TEXT NOT NULL,
            value_sha256 TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            source_ref TEXT,
            intent_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(value_id, revision)
        );

        CREATE TABLE IF NOT EXISTS journal_prompt_interactions (
            interaction_id TEXT PRIMARY KEY,
            local_date TEXT NOT NULL,
            day_id TEXT,
            composition_snapshot_id TEXT,
            module_instance_id TEXT NOT NULL,
            module_instance_version INTEGER NOT NULL,
            prompt_id TEXT NOT NULL,
            prompt_version INTEGER NOT NULL,
            input_item_id TEXT,
            input_text TEXT NOT NULL,
            input_sha256 TEXT NOT NULL,
            source_ref TEXT,
            result_retention TEXT NOT NULL CHECK(result_retention IN (
                'latest_only','all_versions','policy_managed'
            )),
            result_search_mode TEXT NOT NULL CHECK(result_search_mode IN (
                'exclude','metadata_only','content'
            )),
            lifecycle TEXT NOT NULL DEFAULT 'current' CHECK(lifecycle IN (
                'current','accepted','archived','tombstoned'
            )),
            current_revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(prompt_id, prompt_version) REFERENCES
                journal_prompt_definition_versions(prompt_id, prompt_version),
            FOREIGN KEY(input_item_id) REFERENCES journal_items(item_id)
        );

        CREATE TABLE IF NOT EXISTS journal_prompt_runs (
            run_id TEXT PRIMARY KEY,
            interaction_id TEXT NOT NULL REFERENCES journal_prompt_interactions(interaction_id),
            run_ordinal INTEGER NOT NULL CHECK(run_ordinal >= 1),
            producer_id TEXT NOT NULL,
            provider_id TEXT,
            model_id TEXT,
            input_sha256 TEXT NOT NULL,
            context_manifest_sha256 TEXT NOT NULL,
            generation_receipt_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','succeeded','failed','canceled')),
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(interaction_id, run_ordinal)
        );

        CREATE TABLE IF NOT EXISTS journal_prompt_result_variants (
            variant_id TEXT PRIMARY KEY,
            interaction_id TEXT NOT NULL REFERENCES journal_prompt_interactions(interaction_id),
            run_id TEXT NOT NULL UNIQUE REFERENCES journal_prompt_runs(run_id),
            variant_ordinal INTEGER NOT NULL CHECK(variant_ordinal >= 1),
            result_authority TEXT NOT NULL CHECK(result_authority IN (
                'derived_value','domain_value','cowork_document'
            )),
            result_item_id TEXT,
            result_text TEXT,
            result_content_sha256 TEXT NOT NULL,
            lifecycle TEXT NOT NULL DEFAULT 'current' CHECK(lifecycle IN (
                'current','accepted','archived','rejected'
            )),
            current_revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(interaction_id, variant_ordinal),
            FOREIGN KEY(result_item_id) REFERENCES journal_items(item_id),
            CHECK (
                (result_authority = 'derived_value' AND result_text IS NOT NULL)
                OR (result_authority IN ('domain_value','cowork_document')
                    AND result_item_id IS NOT NULL)
            )
        );

        CREATE TABLE IF NOT EXISTS journal_prompt_decisions (
            decision_id TEXT PRIMARY KEY,
            interaction_id TEXT NOT NULL REFERENCES journal_prompt_interactions(interaction_id),
            variant_id TEXT NOT NULL REFERENCES journal_prompt_result_variants(variant_id),
            decision_kind TEXT NOT NULL CHECK(decision_kind IN (
                'accept','archive','reject'
            )),
            interaction_revision INTEGER NOT NULL,
            actor_json TEXT NOT NULL,
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    _immutable_triggers(
        conn,
        (
            "journal_item_revisions",
            "journal_relation_revisions",
            "journal_field_value_revisions",
            "journal_prompt_runs",
            "journal_prompt_decisions",
        ),
    )
    _set_legacy_version(conn, 10)


def _m011_search_outbox_and_legacy_bridge(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS journal_domain_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS journal_search_outbox (
            event_id TEXT PRIMARY KEY,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            aggregate_revision TEXT NOT NULL,
            event_kind TEXT NOT NULL CHECK(event_kind IN (
                'upsert','delete','composition_changed','backfill'
            )),
            content_sha256 TEXT NOT NULL,
            composition_digest TEXT,
            search_recipe_version INTEGER NOT NULL DEFAULT 1,
            privacy_class TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN (
                'pending','leased','delivered','failed'
            )),
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_expires_at TEXT,
            error_code TEXT,
            committed_at TEXT NOT NULL,
            delivered_at TEXT,
            UNIQUE(aggregate_type,aggregate_id,aggregate_revision,event_kind)
        );
        CREATE INDEX IF NOT EXISTS journal_search_outbox_state_idx
            ON journal_search_outbox(state, committed_at, event_id);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO journal_domain_state(key,value,updated_at) "
        "VALUES('content_authority','legacy_compatibility','1970-01-01T00:00:00+00:00')"
    )
    # Preserve one content authority: the generic item stores identity and
    # policy only while the historical journal_entries row remains the sole
    # prose/version authority.
    conn.execute(
        """
        INSERT OR IGNORE INTO journal_items(
            item_id,local_date,item_kind,authority_kind,legacy_entry_id,
            interaction_behavior_id,interaction_behavior_version,privacy_class,
            search_mode,source_ref,lifecycle,current_revision,created_at,updated_at
        )
        SELECT
            entry_id,
            CASE
                WHEN day_id LIKE 'journal-day:%' THEN substr(day_id,13,10)
                ELSE substr(day_id,1,10)
            END,
            CASE entry_kind WHEN 'log' THEN 'record' ELSE 'running_note' END,
            'legacy_entry',
            entry_id,
            'human_value',
            1,
            'private',
            'lexical_dense',
            source_ref,
            CASE resolution_state WHEN 'open' THEN 'current' ELSE 'resolved' END,
            version,
            created_at,
            updated_at
        FROM journal_entries
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO journal_search_outbox(
            event_id,aggregate_type,aggregate_id,aggregate_revision,event_kind,
            content_sha256,search_recipe_version,privacy_class,committed_at
        )
        SELECT
            'jso_backfill_' || entry_id,
            'item',
            entry_id,
            CAST(version AS TEXT),
            'backfill',
            content_sha256,
            1,
            'private',
            updated_at
        FROM journal_entries
        """
    )
    _set_legacy_version(conn, 11)


def _m012_staged_history_import_cohorts(conn: sqlite3.Connection) -> None:
    """Add a sealed publication boundary for private legacy-history imports.

    Imported prose is retained by Sources before any Journal publication.  The
    tables below contain only inventory, span, request, and receipt metadata;
    ordinary Journal items are created atomically by the cohort seal.
    """

    # ``unknown`` is distinct from ``unreviewed`` for historical material: we
    # do not know whether a legacy span was ever reviewed.  Rebuild instead of
    # changing the already published v10 migration.
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS journal_item_revisions_immutable_update;
        DROP TRIGGER IF EXISTS journal_item_revisions_immutable_delete;

        CREATE TABLE journal_item_revisions_v12 (
            item_id TEXT NOT NULL REFERENCES journal_items(item_id),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            authority_kind TEXT NOT NULL,
            plain_value TEXT,
            content_sha256 TEXT,
            lifecycle TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            source_ref TEXT,
            authorship TEXT NOT NULL CHECK(authorship IN (
                'human','ai','mixed','unknown','generated'
            )),
            review_state TEXT NOT NULL CHECK(review_state IN (
                'not_applicable','unknown','unreviewed','reviewed','rejected'
            )),
            intent_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(item_id, revision)
        );
        INSERT INTO journal_item_revisions_v12(
            item_id,revision,authority_kind,plain_value,content_sha256,lifecycle,
            actor_json,source_ref,authorship,review_state,intent_id,created_at
        )
        SELECT
            item_id,revision,authority_kind,plain_value,content_sha256,lifecycle,
            actor_json,source_ref,authorship,review_state,intent_id,created_at
        FROM journal_item_revisions;
        DROP TABLE journal_item_revisions;
        ALTER TABLE journal_item_revisions_v12 RENAME TO journal_item_revisions;

        CREATE TABLE journal_import_cohorts (
            cohort_id TEXT PRIMARY KEY,
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL UNIQUE,
            inventory_sha256 TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            mapping_version TEXT NOT NULL,
            mapping_sha256 TEXT NOT NULL,
            parse_report_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'prepared','staging','verified','sealed','aborted'
            )),
            state_revision INTEGER NOT NULL CHECK(state_revision >= 1),
            expected_file_count INTEGER NOT NULL CHECK(expected_file_count >= 0),
            expected_byte_count INTEGER NOT NULL CHECK(expected_byte_count >= 0),
            expected_span_count INTEGER NOT NULL CHECK(expected_span_count >= 0),
            expected_item_count INTEGER NOT NULL CHECK(expected_item_count >= 0),
            actor_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            verified_at TEXT,
            sealed_at TEXT,
            aborted_at TEXT,
            abort_code TEXT,
            seal_sha256 TEXT,
            CHECK ((state = 'sealed') = (sealed_at IS NOT NULL)),
            CHECK ((state = 'aborted') = (aborted_at IS NOT NULL))
        );

        CREATE TABLE journal_import_files (
            cohort_id TEXT NOT NULL REFERENCES journal_import_cohorts(cohort_id),
            file_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            local_date TEXT,
            byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
            mtime_ns INTEGER NOT NULL CHECK(mtime_ns >= 0),
            raw_sha256 TEXT NOT NULL,
            encoding TEXT NOT NULL,
            newline TEXT NOT NULL,
            expected_parse_sha256 TEXT NOT NULL,
            expected_span_count INTEGER NOT NULL CHECK(expected_span_count >= 0),
            state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN (
                'prepared','staged'
            )),
            ingress_client_mutation_id TEXT NOT NULL,
            stage_request_sha256 TEXT NOT NULL,
            source_ref TEXT,
            representation_id TEXT,
            submission_id TEXT,
            staged_at TEXT,
            PRIMARY KEY(cohort_id, file_id),
            UNIQUE(cohort_id, relative_path),
            CHECK (
                (state = 'prepared' AND source_ref IS NULL
                    AND representation_id IS NULL AND submission_id IS NULL
                    AND staged_at IS NULL)
                OR
                (state = 'staged' AND source_ref IS NOT NULL
                    AND representation_id IS NOT NULL AND submission_id IS NOT NULL
                    AND staged_at IS NOT NULL)
            )
        );

        CREATE TABLE journal_import_spans (
            cohort_id TEXT NOT NULL,
            logical_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            disposition TEXT NOT NULL,
            section_key TEXT,
            start_byte INTEGER NOT NULL CHECK(start_byte >= 0),
            end_byte INTEGER NOT NULL CHECK(end_byte >= start_byte),
            raw_sha256 TEXT NOT NULL,
            normalized_sha256 TEXT,
            structural_sha256 TEXT,
            managed_projections_json TEXT NOT NULL DEFAULT '[]',
            reason_code TEXT,
            materialize INTEGER NOT NULL CHECK(materialize IN (0,1)),
            item_id TEXT,
            item_kind TEXT,
            classification_id TEXT,
            module_instance_id TEXT,
            module_instance_version INTEGER,
            privacy_class TEXT,
            search_mode TEXT,
            interaction_behavior_id TEXT,
            interaction_behavior_version INTEGER,
            authorship TEXT NOT NULL DEFAULT 'unknown' CHECK(authorship = 'unknown'),
            review_state TEXT NOT NULL DEFAULT 'unknown' CHECK(review_state = 'unknown'),
            receipt_sha256 TEXT NOT NULL,
            materialized_at TEXT,
            PRIMARY KEY(cohort_id, logical_id),
            UNIQUE(cohort_id, file_id, start_byte, end_byte),
            FOREIGN KEY(cohort_id, file_id) REFERENCES
                journal_import_files(cohort_id, file_id),
            CHECK (
                (materialize = 0 AND item_id IS NULL AND item_kind IS NULL
                    AND privacy_class IS NULL AND search_mode IS NULL
                    AND interaction_behavior_id IS NULL
                    AND interaction_behavior_version IS NULL)
                OR
                (materialize = 1 AND item_id IS NOT NULL AND item_kind IS NOT NULL
                    AND privacy_class IS NOT NULL AND search_mode IS NOT NULL
                    AND interaction_behavior_id IS NOT NULL
                    AND interaction_behavior_version IS NOT NULL)
            ),
            CHECK ((module_instance_id IS NULL) = (module_instance_version IS NULL))
        );
        CREATE INDEX journal_import_spans_file_idx
            ON journal_import_spans(cohort_id,file_id,start_byte);

        CREATE TABLE journal_import_receipts (
            receipt_id TEXT PRIMARY KEY,
            cohort_id TEXT NOT NULL REFERENCES journal_import_cohorts(cohort_id),
            receipt_kind TEXT NOT NULL CHECK(receipt_kind IN (
                'prepared','staging_started','file_staged','verified','sealed','aborted'
            )),
            subject_id TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            result_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(cohort_id,receipt_kind,subject_id,request_sha256)
        );

        CREATE TABLE journal_import_progress (
            cohort_id TEXT NOT NULL REFERENCES journal_import_cohorts(cohort_id),
            phase TEXT NOT NULL CHECK(phase IN ('stage_file','verify','seal')),
            subject_id TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','succeeded','failed')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            receipt_id TEXT REFERENCES journal_import_receipts(receipt_id),
            error_code TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(cohort_id,phase,subject_id)
        );

        CREATE TABLE journal_import_state_transitions (
            cohort_id TEXT NOT NULL REFERENCES journal_import_cohorts(cohort_id),
            state_revision INTEGER NOT NULL CHECK(state_revision >= 1),
            from_state TEXT,
            to_state TEXT NOT NULL CHECK(to_state IN (
                'prepared','staging','verified','sealed','aborted'
            )),
            request_sha256 TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(cohort_id,state_revision),
            CHECK (
                (state_revision = 1 AND from_state IS NULL AND to_state = 'prepared')
                OR
                (state_revision > 1 AND from_state IS NOT NULL)
            )
        );
        """
    )
    _immutable_triggers(
        conn,
        (
            "journal_item_revisions",
            "journal_import_receipts",
            "journal_import_state_transitions",
        ),
    )
    _set_legacy_version(conn, 12)


def _m013_journal_authority_cutover(conn: sqlite3.Connection) -> None:
    """Add an explicit, fenced authority seal for database-only capture."""

    redacted_sha = "017ad73325fcf108a972edac618f9edfc957c5b1de10f8b371b0a8bfa4f59e2d"
    _add_column(conn, "journal_source_redactions", "native_item_id", "TEXT")
    conn.executescript(
        f"""
        CREATE TABLE journal_authority_control (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            mode TEXT NOT NULL CHECK(mode IN (
                'legacy_compatibility','database_only','recovery_fenced'
            )),
            authority_revision INTEGER NOT NULL CHECK(authority_revision >= 1),
            activated_cohort_id TEXT REFERENCES journal_import_cohorts(cohort_id),
            prior_mode TEXT CHECK(prior_mode IN (
                'legacy_compatibility','database_only'
            )),
            first_native_capture_id TEXT,
            first_native_item_id TEXT,
            first_native_write_at TEXT,
            fence_code TEXT,
            fenced_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (first_native_capture_id IS NULL AND first_native_item_id IS NULL
                    AND first_native_write_at IS NULL)
                OR
                (first_native_capture_id IS NOT NULL AND first_native_item_id IS NOT NULL
                    AND first_native_write_at IS NOT NULL)
            ),
            CHECK (
                (mode = 'recovery_fenced' AND prior_mode IS NOT NULL
                    AND fence_code IS NOT NULL AND fenced_at IS NOT NULL)
                OR
                (mode != 'recovery_fenced' AND prior_mode IS NULL
                    AND fence_code IS NULL AND fenced_at IS NULL)
            )
        );

        CREATE TABLE journal_authority_transitions (
            authority_revision INTEGER PRIMARY KEY CHECK(authority_revision >= 1),
            transition_kind TEXT NOT NULL CHECK(transition_kind IN (
                'bootstrap','activate','rollback','fence','recover','first_native_write'
            )),
            from_mode TEXT,
            to_mode TEXT NOT NULL CHECK(to_mode IN (
                'legacy_compatibility','database_only','recovery_fenced'
            )),
            cohort_id TEXT,
            capture_id TEXT,
            item_id TEXT,
            request_sha256 TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE journal_native_capture_bindings (
            capture_id TEXT PRIMARY KEY REFERENCES journal_captures(capture_id),
            item_id TEXT NOT NULL UNIQUE REFERENCES journal_items(item_id),
            target TEXT NOT NULL CHECK(target IN ('log','running_notes')),
            request_sha256 TEXT NOT NULL,
            authority_revision INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE journal_native_redactions (
            redaction_event_id TEXT PRIMARY KEY,
            capture_id TEXT NOT NULL REFERENCES journal_captures(capture_id),
            item_id TEXT NOT NULL REFERENCES journal_items(item_id),
            source_ref TEXT NOT NULL,
            redaction_epoch INTEGER NOT NULL CHECK(redaction_epoch >= 1),
            original_revision INTEGER NOT NULL CHECK(original_revision >= 1),
            scrubbed_revision INTEGER CHECK(scrubbed_revision > original_revision),
            state TEXT NOT NULL CHECK(state IN ('scrubbing','committed')),
            result_sha256 TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(item_id,redaction_epoch),
            CHECK (
                (state='scrubbing' AND scrubbed_revision IS NULL
                    AND result_sha256 IS NULL AND completed_at IS NULL)
                OR
                (state='committed' AND scrubbed_revision IS NOT NULL
                    AND result_sha256 IS NOT NULL AND completed_at IS NOT NULL)
            )
        );

        INSERT INTO journal_authority_control(
            singleton,mode,authority_revision,prior_mode,fence_code,fenced_at,
            created_at,updated_at
        )
        SELECT
            1,
            CASE value
                WHEN 'database_only' THEN 'recovery_fenced'
                ELSE 'legacy_compatibility'
            END,
            1,
            CASE value WHEN 'database_only' THEN 'database_only' ELSE NULL END,
            CASE value
                WHEN 'database_only' THEN 'pre_v13_authority_requires_reconciliation'
                ELSE NULL
            END,
            CASE value
                WHEN 'database_only' THEN '1970-01-01T00:00:00+00:00'
                ELSE NULL
            END,
            '1970-01-01T00:00:00+00:00',
            '1970-01-01T00:00:00+00:00'
        FROM journal_domain_state WHERE key='content_authority';
        INSERT INTO journal_authority_transitions(
            authority_revision,transition_kind,from_mode,to_mode,request_sha256,
            actor_json,created_at
        )
        SELECT
            1,'bootstrap',NULL,mode,
            '0000000000000000000000000000000000000000000000000000000000000000',
            '{{"kind":"journal_schema_migration","version":13}}',
            '1970-01-01T00:00:00+00:00'
        FROM journal_authority_control WHERE singleton=1;

        DROP TRIGGER IF EXISTS journal_item_revisions_immutable_update;
        CREATE TRIGGER journal_item_revisions_immutable_update
        BEFORE UPDATE ON journal_item_revisions
        WHEN NOT (
            NEW.item_id = OLD.item_id
            AND NEW.revision = OLD.revision
            AND NEW.authority_kind = OLD.authority_kind
            AND NEW.plain_value = '[redacted]'
            AND NEW.content_sha256 = '{redacted_sha}'
            AND NEW.lifecycle = 'tombstoned'
            AND NEW.actor_json = OLD.actor_json
            AND COALESCE(NEW.source_ref,'') = COALESCE(OLD.source_ref,'')
            AND NEW.authorship = OLD.authorship
            AND NEW.review_state = OLD.review_state
            AND NEW.intent_id = OLD.intent_id
            AND NEW.created_at = OLD.created_at
            AND EXISTS (
                SELECT 1 FROM journal_native_redactions AS redaction
                WHERE redaction.item_id=OLD.item_id AND redaction.state='scrubbing'
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal_item_revisions_is_immutable');
        END;
        """
    )
    _immutable_triggers(
        conn,
        (
            "journal_authority_transitions",
            "journal_native_capture_bindings",
        ),
    )
    _set_legacy_version(conn, 13)


def _m014_import_publication_visibility(conn: sqlite3.Connection) -> None:
    """Keep sealed import publications inactive until authority cutover."""

    _add_column(conn, "journal_items", "import_cohort_id", "TEXT")
    _add_column(conn, "journal_search_outbox", "visibility_cohort_id", "TEXT")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS journal_items_import_cohort_idx
            ON journal_items(import_cohort_id,local_date,lifecycle);
        CREATE INDEX IF NOT EXISTS journal_search_outbox_visibility_idx
            ON journal_search_outbox(visibility_cohort_id,state,committed_at);
        """
    )
    # Safely adopt any v13 rehearsal rows using the immutable import span/item
    # binding. Native captures and preexisting native rows remain unscoped.
    conn.execute(
        """
        UPDATE journal_items
        SET import_cohort_id=(
            SELECT span.cohort_id FROM journal_import_spans AS span
            WHERE span.item_id=journal_items.item_id AND span.materialize=1
            LIMIT 1
        )
        WHERE import_cohort_id IS NULL AND EXISTS(
            SELECT 1 FROM journal_import_spans AS span
            WHERE span.item_id=journal_items.item_id AND span.materialize=1
        )
        """
    )
    conn.execute(
        """
        UPDATE journal_search_outbox
        SET visibility_cohort_id=(
            SELECT item.import_cohort_id FROM journal_items AS item
            WHERE item.item_id=journal_search_outbox.aggregate_id
        )
        WHERE aggregate_type='item' AND visibility_cohort_id IS NULL
          AND EXISTS(
              SELECT 1 FROM journal_items AS item
              WHERE item.item_id=journal_search_outbox.aggregate_id
                AND item.import_cohort_id IS NOT NULL
          )
        """
    )
    _set_legacy_version(conn, 14)


def _m015_import_source_dependencies(conn: sqlite3.Connection) -> None:
    """Track and scrub retained Sources used by legacy-history imports.

    A staged file is an exact readable dependency of every native item
    materialized from its spans.  The dependency must be acknowledged before
    verification/cutover, and Source redaction gets a durable, content-free
    receipt while every current and historical Journal copy is scrubbed.
    """

    redacted_sha = "017ad73325fcf108a972edac618f9edfc957c5b1de10f8b371b0a8bfa4f59e2d"
    _add_column(conn, "journal_import_files", "source_usage_id", "TEXT")
    _add_column(conn, "journal_import_files", "source_usage_consumer_id", "TEXT")
    _add_column(
        conn,
        "journal_import_files",
        "source_usage_state",
        "TEXT NOT NULL DEFAULT 'unreserved' CHECK(source_usage_state IN ("
        "'unreserved','reserved','acknowledged','redaction_committed','released'))",
    )
    conn.executescript(
        f"""
        CREATE UNIQUE INDEX journal_import_files_source_usage_idx
            ON journal_import_files(source_usage_id)
            WHERE source_usage_id IS NOT NULL;
        CREATE UNIQUE INDEX journal_import_files_source_consumer_idx
            ON journal_import_files(source_usage_consumer_id)
            WHERE source_usage_consumer_id IS NOT NULL;

        CREATE TABLE journal_import_source_redactions (
            redaction_event_id TEXT PRIMARY KEY,
            cohort_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            source_usage_id TEXT NOT NULL,
            source_usage_consumer_id TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            redaction_epoch INTEGER NOT NULL CHECK(redaction_epoch >= 1),
            state TEXT NOT NULL CHECK(state IN ('scrubbing','committed')),
            scrubbed_item_count INTEGER,
            result_sha256 TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(source_usage_id, redaction_epoch),
            FOREIGN KEY(cohort_id, file_id) REFERENCES
                journal_import_files(cohort_id, file_id),
            CHECK (
                (state='scrubbing' AND scrubbed_item_count IS NULL
                    AND result_sha256 IS NULL AND completed_at IS NULL)
                OR
                (state='committed' AND scrubbed_item_count IS NOT NULL
                    AND result_sha256 IS NOT NULL AND completed_at IS NOT NULL)
            )
        );
        CREATE INDEX journal_import_source_redactions_file_idx
            ON journal_import_source_redactions(cohort_id,file_id,state);

        DROP TRIGGER IF EXISTS journal_item_revisions_immutable_update;
        CREATE TRIGGER journal_item_revisions_immutable_update
        BEFORE UPDATE ON journal_item_revisions
        WHEN NOT (
            NEW.item_id = OLD.item_id
            AND NEW.revision = OLD.revision
            AND NEW.authority_kind = OLD.authority_kind
            AND NEW.plain_value = '[redacted]'
            AND NEW.content_sha256 = '{redacted_sha}'
            AND NEW.lifecycle = 'tombstoned'
            AND NEW.actor_json = OLD.actor_json
            AND COALESCE(NEW.source_ref,'') = COALESCE(OLD.source_ref,'')
            AND NEW.authorship = OLD.authorship
            AND NEW.review_state = OLD.review_state
            AND NEW.intent_id = OLD.intent_id
            AND NEW.created_at = OLD.created_at
            AND (
                EXISTS (
                    SELECT 1 FROM journal_native_redactions AS redaction
                    WHERE redaction.item_id=OLD.item_id
                      AND redaction.state='scrubbing'
                )
                OR EXISTS (
                    SELECT 1
                    FROM journal_import_source_redactions AS redaction
                    JOIN journal_import_spans AS span
                      ON span.cohort_id=redaction.cohort_id
                     AND span.file_id=redaction.file_id
                     AND span.item_id=OLD.item_id
                    WHERE redaction.state='scrubbing'
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal_item_revisions_is_immutable');
        END;
        """
    )
    conn.execute(
        "UPDATE journal_import_files SET source_usage_consumer_id="
        "'journal-import-file:' || cohort_id || ':' || file_id "
        "WHERE source_usage_consumer_id IS NULL"
    )

    # A pre-v15 rehearsal may already have activated imported readable copies
    # without declaring their Source dependency.  The Journal DB cannot repair
    # the separate Sources DB during schema migration, so fail closed until an
    # operator reconciles the cohort instead of silently trusting that state.
    control = conn.execute(
        "SELECT mode,authority_revision,activated_cohort_id "
        "FROM journal_authority_control WHERE singleton=1"
    ).fetchone()
    missing = conn.execute(
        "SELECT 1 FROM journal_import_files WHERE state='staged' "
        "AND source_usage_state!='acknowledged' LIMIT 1"
    ).fetchone()
    if control is not None and str(control[0]) == "database_only" and missing:
        revision = int(control[1]) + 1
        at = "1970-01-01T00:00:00+00:00"
        request_sha = hashlib.sha256(
            b"journal-v15-import-source-dependencies-require-reconciliation"
        ).hexdigest()
        conn.execute(
            "UPDATE journal_authority_control SET mode='recovery_fenced',"
            "authority_revision=?,prior_mode='database_only',"
            "fence_code='pre_v15_import_source_dependencies_missing',"
            "fenced_at=?,updated_at=? WHERE singleton=1",
            (revision, at, at),
        )
        conn.execute(
            "INSERT INTO journal_authority_transitions("
            "authority_revision,transition_kind,from_mode,to_mode,cohort_id,"
            "request_sha256,actor_json,created_at) "
            "VALUES(?,'fence','database_only','recovery_fenced',?,?,?,?)",
            (
                revision,
                control[2],
                request_sha,
                '{"kind":"journal_schema_migration","version":15}',
                at,
            ),
        )
        conn.execute(
            "UPDATE journal_domain_state SET value='recovery_fenced',"
            "revision=revision+1,updated_at=? WHERE key='content_authority'",
            (at,),
        )
    _set_legacy_version(conn, 15)


def _m016_generic_native_source_dependencies(conn: sqlite3.Connection) -> None:
    """Bind non-capture native items to retained Sources and their erasure."""

    redacted_sha = "017ad73325fcf108a972edac618f9edfc957c5b1de10f8b371b0a8bfa4f59e2d"
    conn.executescript(
        f"""
        CREATE TABLE journal_native_source_dependencies (
            dependency_id TEXT PRIMARY KEY,
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL UNIQUE,
            source_usage_consumer_id TEXT NOT NULL UNIQUE,
            source_ref TEXT NOT NULL,
            representation_id TEXT NOT NULL,
            source_usage_id TEXT UNIQUE,
            purpose TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            item_id TEXT UNIQUE REFERENCES journal_items(item_id),
            state TEXT NOT NULL CHECK(state IN (
                'prepared','reserved','bound','acknowledged',
                'redaction_committed','released','aborted'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (state='prepared' AND source_usage_id IS NULL AND item_id IS NULL)
                OR
                (state='reserved' AND source_usage_id IS NOT NULL AND item_id IS NULL)
                OR
                (state IN ('bound','acknowledged')
                    AND source_usage_id IS NOT NULL AND item_id IS NOT NULL)
                OR
                (state IN ('redaction_committed','released','aborted'))
            )
        );
        CREATE INDEX journal_native_source_dependencies_state_idx
            ON journal_native_source_dependencies(state,updated_at);

        CREATE TABLE journal_native_source_redactions (
            redaction_event_id TEXT PRIMARY KEY,
            dependency_id TEXT NOT NULL REFERENCES
                journal_native_source_dependencies(dependency_id),
            source_usage_id TEXT NOT NULL,
            source_usage_consumer_id TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            item_id TEXT REFERENCES journal_items(item_id),
            redaction_epoch INTEGER NOT NULL CHECK(redaction_epoch >= 1),
            state TEXT NOT NULL CHECK(state IN ('scrubbing','committed')),
            scrubbed_revision INTEGER,
            result_sha256 TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(source_usage_id,redaction_epoch),
            CHECK (
                (state='scrubbing' AND scrubbed_revision IS NULL
                    AND result_sha256 IS NULL AND completed_at IS NULL)
                OR
                (state='committed' AND result_sha256 IS NOT NULL
                    AND completed_at IS NOT NULL)
            )
        );

        DROP TRIGGER IF EXISTS journal_item_revisions_immutable_update;
        CREATE TRIGGER journal_item_revisions_immutable_update
        BEFORE UPDATE ON journal_item_revisions
        WHEN NOT (
            NEW.item_id = OLD.item_id
            AND NEW.revision = OLD.revision
            AND NEW.authority_kind = OLD.authority_kind
            AND NEW.plain_value = '[redacted]'
            AND NEW.content_sha256 = '{redacted_sha}'
            AND NEW.lifecycle = 'tombstoned'
            AND NEW.actor_json = OLD.actor_json
            AND COALESCE(NEW.source_ref,'') = COALESCE(OLD.source_ref,'')
            AND NEW.authorship = OLD.authorship
            AND NEW.review_state = OLD.review_state
            AND NEW.intent_id = OLD.intent_id
            AND NEW.created_at = OLD.created_at
            AND (
                EXISTS (
                    SELECT 1 FROM journal_native_redactions AS redaction
                    WHERE redaction.item_id=OLD.item_id
                      AND redaction.state='scrubbing'
                )
                OR EXISTS (
                    SELECT 1
                    FROM journal_import_source_redactions AS redaction
                    JOIN journal_import_spans AS span
                      ON span.cohort_id=redaction.cohort_id
                     AND span.file_id=redaction.file_id
                     AND span.item_id=OLD.item_id
                    WHERE redaction.state='scrubbing'
                )
                OR EXISTS (
                    SELECT 1 FROM journal_native_source_redactions AS redaction
                    WHERE redaction.item_id=OLD.item_id
                      AND redaction.state='scrubbing'
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal_item_revisions_is_immutable');
        END;
        """
    )
    behavior = {
        "aiContribution": "allowed",
        "aiRead": "policy_controlled",
        "bodyMode": "plain_value",
        "profile": "provenance_only/v1",
        "truthEligibility": "disabled",
    }
    conn.execute(
        "INSERT OR IGNORE INTO journal_interaction_behavior_revisions("
        "behavior_id,behavior_version,definition_json,definition_sha256,created_at) "
        "VALUES('provenance_only',1,?,?,?)",
        (
            _canonical(behavior),
            _digest(behavior),
            "1970-01-01T00:00:00+00:00",
        ),
    )
    _set_legacy_version(conn, 16)


def _m017_typed_field_source_dependencies(conn: sqlite3.Connection) -> None:
    """Track and erase Source-backed typed field-value revisions."""

    redacted_json = _canonical({"redacted": True})
    redacted_sha = hashlib.sha256(redacted_json.encode("utf-8")).hexdigest()
    conn.executescript(
        f"""
        CREATE TABLE journal_field_source_dependencies (
            dependency_id TEXT PRIMARY KEY,
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL UNIQUE,
            source_usage_consumer_id TEXT NOT NULL UNIQUE,
            source_ref TEXT NOT NULL,
            representation_id TEXT NOT NULL,
            source_usage_id TEXT UNIQUE,
            purpose TEXT NOT NULL,
            value_id TEXT NOT NULL,
            value_revision INTEGER,
            value_sha256 TEXT,
            state TEXT NOT NULL CHECK(state IN (
                'prepared','reserved','bound','acknowledged',
                'redaction_committed','released','aborted'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (state='prepared' AND source_usage_id IS NULL
                    AND value_revision IS NULL)
                OR
                (state='reserved' AND source_usage_id IS NOT NULL
                    AND value_revision IS NULL)
                OR
                (state IN ('bound','acknowledged')
                    AND source_usage_id IS NOT NULL
                    AND value_revision IS NOT NULL AND value_sha256 IS NOT NULL)
                OR
                (state IN ('redaction_committed','released','aborted'))
            )
        );
        CREATE INDEX journal_field_source_dependencies_value_idx
            ON journal_field_source_dependencies(value_id,value_revision,state);

        CREATE TABLE journal_field_source_redactions (
            redaction_event_id TEXT PRIMARY KEY,
            dependency_id TEXT NOT NULL REFERENCES
                journal_field_source_dependencies(dependency_id),
            source_usage_id TEXT NOT NULL,
            source_usage_consumer_id TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            value_id TEXT NOT NULL,
            value_revision INTEGER,
            scrubbed_revision INTEGER,
            redaction_epoch INTEGER NOT NULL CHECK(redaction_epoch >= 1),
            state TEXT NOT NULL CHECK(state IN ('scrubbing','committed')),
            result_sha256 TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(source_usage_id,redaction_epoch),
            CHECK (
                (state='scrubbing' AND scrubbed_revision IS NULL
                    AND result_sha256 IS NULL AND completed_at IS NULL)
                OR
                (state='committed' AND result_sha256 IS NOT NULL
                    AND completed_at IS NOT NULL)
            )
        );

        DROP TRIGGER IF EXISTS journal_field_value_revisions_immutable_update;
        CREATE TRIGGER journal_field_value_revisions_immutable_update
        BEFORE UPDATE ON journal_field_value_revisions
        WHEN NOT (
            NEW.value_id = OLD.value_id
            AND NEW.revision = OLD.revision
            AND NEW.value_json = '{redacted_json}'
            AND NEW.value_sha256 = '{redacted_sha}'
            AND NEW.actor_json = OLD.actor_json
            AND COALESCE(NEW.source_ref,'') = COALESCE(OLD.source_ref,'')
            AND NEW.intent_id = OLD.intent_id
            AND NEW.created_at = OLD.created_at
            AND EXISTS (
                SELECT 1 FROM journal_field_source_redactions AS redaction
                WHERE redaction.value_id=OLD.value_id
                  AND redaction.value_revision=OLD.revision
                  AND redaction.state='scrubbing'
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal_field_value_revisions_is_immutable');
        END;
        """
    )
    _set_legacy_version(conn, 17)


def _m018_field_revision_provenance(conn: sqlite3.Connection) -> None:
    """Retain authorship and review state on every typed-value revision."""

    redacted_json = _canonical({"redacted": True})
    redacted_sha = hashlib.sha256(redacted_json.encode("utf-8")).hexdigest()
    conn.executescript(
        f"""
        ALTER TABLE journal_field_value_revisions
            ADD COLUMN authorship TEXT NOT NULL DEFAULT 'unknown'
            CHECK(authorship IN ('human','ai','mixed','unknown','generated'));
        ALTER TABLE journal_field_value_revisions
            ADD COLUMN review_state TEXT NOT NULL DEFAULT 'unknown'
            CHECK(review_state IN (
                'not_applicable','unknown','unreviewed','reviewed','rejected'
            ));

        DROP TRIGGER IF EXISTS journal_field_value_revisions_immutable_update;
        CREATE TRIGGER journal_field_value_revisions_immutable_update
        BEFORE UPDATE ON journal_field_value_revisions
        WHEN NOT (
            NEW.value_id = OLD.value_id
            AND NEW.revision = OLD.revision
            AND NEW.value_json = '{redacted_json}'
            AND NEW.value_sha256 = '{redacted_sha}'
            AND NEW.actor_json = OLD.actor_json
            AND COALESCE(NEW.source_ref,'') = COALESCE(OLD.source_ref,'')
            AND NEW.intent_id = OLD.intent_id
            AND NEW.created_at = OLD.created_at
            AND NEW.authorship = OLD.authorship
            AND NEW.review_state = OLD.review_state
            AND EXISTS (
                SELECT 1 FROM journal_field_source_redactions AS redaction
                WHERE redaction.value_id=OLD.value_id
                  AND redaction.value_revision=OLD.revision
                  AND redaction.state='scrubbing'
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal_field_value_revisions_is_immutable');
        END;
        """
    )
    _set_legacy_version(conn, 18)


def _m019_staged_typed_import_profiles(conn: sqlite3.Connection) -> None:
    """Stage private profile mappings and typed observations with import cohorts."""

    redacted_json = _canonical({"redacted": True})
    redacted_sha = hashlib.sha256(redacted_json.encode("utf-8")).hexdigest()
    _add_column(conn, "journal_import_cohorts", "typed_mapping_sha256", "TEXT")
    _add_column(
        conn,
        "journal_import_cohorts",
        "expected_observation_count",
        "INTEGER NOT NULL DEFAULT 0 CHECK(expected_observation_count >= 0)",
    )
    for table in (
        "journal_field_definition_versions",
        "journal_module_instance_versions",
        "journal_profile_revisions",
        "journal_profile_activation_epochs",
        "journal_days",
        "journal_day_composition_snapshots",
    ):
        _add_column(
            conn,
            table,
            "import_cohort_id",
            "TEXT REFERENCES journal_import_cohorts(cohort_id)",
        )
    _add_column(
        conn,
        "journal_import_source_redactions",
        "scrubbed_field_value_count",
        "INTEGER NOT NULL DEFAULT 0 CHECK(scrubbed_field_value_count >= 0)",
    )
    conn.executescript(
        f"""
        CREATE TABLE journal_field_values_v19 (
            value_id TEXT PRIMARY KEY,
            local_date TEXT NOT NULL,
            day_id TEXT,
            composition_snapshot_id TEXT,
            composition_slot_id TEXT,
            module_instance_id TEXT NOT NULL,
            module_instance_version INTEGER NOT NULL,
            field_id TEXT NOT NULL,
            field_definition_version INTEGER NOT NULL,
            prompt_id TEXT,
            prompt_version INTEGER,
            value_codec_version INTEGER NOT NULL,
            value_kind TEXT NOT NULL,
            disposition TEXT CHECK(disposition IN ('missing','skipped','declined')),
            text_value TEXT,
            number_value REAL,
            boolean_value INTEGER CHECK(boolean_value IN (0,1)),
            temporal_value TEXT,
            duration_seconds INTEGER CHECK(duration_seconds >= 0),
            option_value TEXT,
            collection_present INTEGER NOT NULL DEFAULT 0 CHECK(collection_present IN (0,1)),
            interaction_ref TEXT,
            source_ref TEXT,
            authorship TEXT NOT NULL CHECK(authorship IN (
                'human','ai','mixed','unknown','generated'
            )),
            review_state TEXT NOT NULL CHECK(review_state IN (
                'not_applicable','unknown','unreviewed','reviewed','rejected'
            )),
            observed_at TEXT,
            stated_at TEXT,
            ingested_at TEXT NOT NULL,
            lifecycle TEXT NOT NULL DEFAULT 'current' CHECK(lifecycle IN (
                'current','archived','tombstoned'
            )),
            current_revision INTEGER NOT NULL DEFAULT 1 CHECK(current_revision >= 1),
            updated_at TEXT NOT NULL,
            import_cohort_id TEXT REFERENCES journal_import_cohorts(cohort_id),
            FOREIGN KEY(day_id) REFERENCES journal_days(day_id),
            FOREIGN KEY(composition_snapshot_id) REFERENCES
                journal_day_composition_snapshots(snapshot_id),
            FOREIGN KEY(field_id, field_definition_version) REFERENCES
                journal_field_definition_versions(field_id, definition_version),
            FOREIGN KEY(prompt_id, prompt_version) REFERENCES
                journal_prompt_definition_versions(prompt_id, prompt_version),
            CHECK ((prompt_id IS NULL) = (prompt_version IS NULL)),
            CHECK (
                (disposition IS NOT NULL
                    AND text_value IS NULL AND number_value IS NULL
                    AND boolean_value IS NULL AND temporal_value IS NULL
                    AND duration_seconds IS NULL AND option_value IS NULL
                    AND collection_present = 0)
                OR
                (disposition IS NULL AND (
                    (value_kind IN ('short_text','long_text') AND text_value IS NOT NULL
                        AND number_value IS NULL AND boolean_value IS NULL
                        AND temporal_value IS NULL AND duration_seconds IS NULL
                        AND option_value IS NULL AND collection_present = 0)
                    OR (value_kind IN ('number','scale') AND number_value IS NOT NULL
                        AND text_value IS NULL AND boolean_value IS NULL
                        AND temporal_value IS NULL AND duration_seconds IS NULL
                        AND option_value IS NULL AND collection_present = 0)
                    OR (value_kind = 'boolean' AND boolean_value IS NOT NULL
                        AND text_value IS NULL AND number_value IS NULL
                        AND temporal_value IS NULL AND duration_seconds IS NULL
                        AND option_value IS NULL AND collection_present = 0)
                    OR (value_kind IN ('local_time','instant','date')
                        AND temporal_value IS NOT NULL AND text_value IS NULL
                        AND number_value IS NULL AND boolean_value IS NULL
                        AND duration_seconds IS NULL AND option_value IS NULL
                        AND collection_present = 0)
                    OR (value_kind = 'duration' AND duration_seconds IS NOT NULL
                        AND text_value IS NULL AND number_value IS NULL
                        AND boolean_value IS NULL AND temporal_value IS NULL
                        AND option_value IS NULL AND collection_present = 0)
                    OR (value_kind = 'single_select' AND option_value IS NOT NULL
                        AND text_value IS NULL AND number_value IS NULL
                        AND boolean_value IS NULL AND temporal_value IS NULL
                        AND duration_seconds IS NULL AND collection_present = 0)
                    OR (value_kind IN ('multi_select','entity_reference','reference')
                        AND collection_present = 1 AND text_value IS NULL
                        AND number_value IS NULL AND boolean_value IS NULL
                        AND temporal_value IS NULL AND duration_seconds IS NULL
                        AND option_value IS NULL)
                ))
            )
        );
        INSERT INTO journal_field_values_v19(
            value_id,local_date,day_id,composition_snapshot_id,composition_slot_id,
            module_instance_id,module_instance_version,field_id,field_definition_version,
            prompt_id,prompt_version,value_codec_version,value_kind,disposition,text_value,
            number_value,boolean_value,temporal_value,duration_seconds,option_value,
            collection_present,interaction_ref,source_ref,authorship,review_state,
            observed_at,stated_at,ingested_at,lifecycle,current_revision,updated_at
        )
        SELECT
            value_id,local_date,day_id,composition_snapshot_id,composition_slot_id,
            module_instance_id,module_instance_version,field_id,field_definition_version,
            prompt_id,prompt_version,value_codec_version,value_kind,disposition,text_value,
            number_value,boolean_value,temporal_value,duration_seconds,option_value,
            collection_present,interaction_ref,source_ref,authorship,review_state,
            observed_at,stated_at,ingested_at,lifecycle,current_revision,updated_at
        FROM journal_field_values;
        DROP TABLE journal_field_values;
        ALTER TABLE journal_field_values_v19 RENAME TO journal_field_values;
        CREATE UNIQUE INDEX journal_field_values_current_slot_idx
            ON journal_field_values(local_date,module_instance_id,field_id,composition_slot_id)
            WHERE lifecycle='current';
        CREATE INDEX journal_field_values_import_cohort_idx
            ON journal_field_values(import_cohort_id,local_date);
        CREATE INDEX journal_profile_activation_import_cohort_idx
            ON journal_profile_activation_epochs(import_cohort_id,activation_revision);
        CREATE INDEX journal_days_import_cohort_idx
            ON journal_days(import_cohort_id,local_date);
        CREATE INDEX journal_day_snapshots_import_cohort_idx
            ON journal_day_composition_snapshots(import_cohort_id,day_id);

        CREATE TABLE journal_import_profile_mappings (
            cohort_id TEXT PRIMARY KEY REFERENCES journal_import_cohorts(cohort_id),
            mapping_version TEXT NOT NULL,
            mapping_sha256 TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            profile_revision INTEGER NOT NULL CHECK(profile_revision >= 1),
            profile_name TEXT NOT NULL,
            profile_description TEXT NOT NULL DEFAULT '',
            profile_digest TEXT NOT NULL,
            module_instance_id TEXT NOT NULL,
            module_instance_version INTEGER NOT NULL CHECK(module_instance_version >= 1),
            module_type_id TEXT NOT NULL,
            module_type_version INTEGER NOT NULL CHECK(module_type_version >= 1),
            module_label TEXT NOT NULL,
            module_slot_id TEXT NOT NULL,
            module_settings_json TEXT NOT NULL,
            module_settings_sha256 TEXT NOT NULL,
            profile_modules_json TEXT NOT NULL,
            profile_modules_sha256 TEXT NOT NULL,
            day_timezone TEXT NOT NULL,
            day_boundary TEXT NOT NULL,
            boundary_policy_revision TEXT NOT NULL,
            behavior_id TEXT NOT NULL,
            behavior_version INTEGER NOT NULL CHECK(behavior_version >= 1),
            authorship TEXT NOT NULL CHECK(authorship IN (
                'human','ai','mixed','unknown','generated'
            )),
            review_state TEXT NOT NULL CHECK(review_state IN (
                'not_applicable','unknown','unreviewed','reviewed','rejected'
            )),
            field_count INTEGER NOT NULL CHECK(field_count > 0),
            observation_set_sha256 TEXT,
            activation_revision INTEGER,
            composition_count INTEGER NOT NULL DEFAULT 0 CHECK(composition_count >= 0),
            created_at TEXT NOT NULL,
            materialized_at TEXT,
            FOREIGN KEY(activation_revision) REFERENCES
                journal_profile_activation_epochs(activation_revision),
            UNIQUE(cohort_id,mapping_sha256)
        );

        CREATE TABLE journal_import_field_mappings (
            cohort_id TEXT NOT NULL REFERENCES journal_import_profile_mappings(cohort_id),
            field_id TEXT NOT NULL,
            definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            slot_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            stable_key TEXT NOT NULL,
            label TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            value_kind TEXT NOT NULL CHECK(value_kind IN (
                'short_text','long_text','number','scale','boolean','single_select',
                'multi_select','local_time','instant','date','duration',
                'entity_reference','reference'
            )),
            unit TEXT,
            constraints_json TEXT NOT NULL,
            value_codec_version INTEGER NOT NULL CHECK(value_codec_version >= 1),
            function_id TEXT,
            function_version INTEGER,
            behavior_id TEXT NOT NULL,
            behavior_version INTEGER NOT NULL CHECK(behavior_version >= 1),
            privacy_class TEXT NOT NULL CHECK(privacy_class IN (
                'private','sensitive','internal'
            )),
            search_mode TEXT NOT NULL CHECK(search_mode IN (
                'structured_only','lexical','dense','lexical_dense','excluded'
            )),
            disclosure_policy_id TEXT NOT NULL,
            definition_sha256 TEXT NOT NULL,
            PRIMARY KEY(cohort_id,field_id,definition_version),
            UNIQUE(cohort_id,ordinal),
            UNIQUE(cohort_id,slot_id),
            UNIQUE(cohort_id,owner,stable_key,definition_version),
            CHECK ((function_id IS NULL) = (function_version IS NULL))
        );

        CREATE TABLE journal_import_typed_observations (
            cohort_id TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            value_id TEXT NOT NULL UNIQUE,
            local_date TEXT NOT NULL,
            field_id TEXT NOT NULL,
            field_definition_version INTEGER NOT NULL CHECK(field_definition_version >= 1),
            evidence_start_byte INTEGER NOT NULL CHECK(evidence_start_byte >= 0),
            evidence_end_byte INTEGER NOT NULL CHECK(evidence_end_byte > evidence_start_byte),
            evidence_sha256 TEXT NOT NULL,
            extractor_receipt_sha256 TEXT NOT NULL,
            value_json TEXT,
            disposition TEXT CHECK(disposition IN ('missing','skipped','declined')),
            frozen_value_sha256 TEXT NOT NULL,
            observed_at TEXT,
            stated_at TEXT,
            state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN (
                'prepared','materialized'
            )),
            receipt_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            materialized_at TEXT,
            PRIMARY KEY(cohort_id,observation_id),
            UNIQUE(cohort_id,local_date,field_id,field_definition_version),
            FOREIGN KEY(cohort_id,file_id) REFERENCES
                journal_import_files(cohort_id,file_id),
            FOREIGN KEY(cohort_id,field_id,field_definition_version) REFERENCES
                journal_import_field_mappings(cohort_id,field_id,definition_version),
            CHECK (
                (disposition IS NULL AND value_json IS NOT NULL)
                OR (disposition IS NOT NULL AND value_json IS NULL)
            ),
            CHECK (
                (state='prepared' AND materialized_at IS NULL)
                OR (state='materialized' AND materialized_at IS NOT NULL)
            )
        );
        CREATE INDEX journal_import_typed_observations_file_idx
            ON journal_import_typed_observations(cohort_id,file_id,state);

        DROP TRIGGER IF EXISTS journal_field_value_revisions_immutable_update;
        CREATE TRIGGER journal_field_value_revisions_immutable_update
        BEFORE UPDATE ON journal_field_value_revisions
        WHEN NOT (
            NEW.value_id = OLD.value_id
            AND NEW.revision = OLD.revision
            AND NEW.value_json = '{redacted_json}'
            AND NEW.value_sha256 = '{redacted_sha}'
            AND NEW.actor_json = OLD.actor_json
            AND COALESCE(NEW.source_ref,'') = COALESCE(OLD.source_ref,'')
            AND NEW.intent_id = OLD.intent_id
            AND NEW.created_at = OLD.created_at
            AND NEW.authorship = OLD.authorship
            AND NEW.review_state = OLD.review_state
            AND (
                EXISTS (
                    SELECT 1 FROM journal_field_source_redactions AS redaction
                    WHERE redaction.value_id=OLD.value_id
                      AND redaction.value_revision=OLD.revision
                      AND redaction.state='scrubbing'
                )
                OR EXISTS (
                    SELECT 1
                    FROM journal_import_source_redactions AS redaction
                    JOIN journal_import_typed_observations AS observation
                      ON observation.cohort_id=redaction.cohort_id
                     AND observation.file_id=redaction.file_id
                     AND observation.value_id=OLD.value_id
                    WHERE redaction.state='scrubbing'
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal_field_value_revisions_is_immutable');
        END;
        """
    )
    _set_legacy_version(conn, 19)


def _m020_cutover_ingress_pause(conn: sqlite3.Connection) -> None:
    """Add a durable pre-seal pause for every legacy Journal ingress path.

    The gate deliberately remains separate from content authority: an import
    may be prepared and verified while compatibility remains authoritative,
    but the cohort cannot be sealed/activated until capture and Markdown
    writers have crossed this durable quiescence barrier.  High-water values
    make an unexpected write after the pause detectable before activation.
    """

    conn.executescript(
        """
        CREATE TABLE journal_cutover_gate (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            state TEXT NOT NULL CHECK(state IN ('open','paused')),
            gate_revision INTEGER NOT NULL CHECK(gate_revision >= 1),
            cohort_id TEXT REFERENCES journal_import_cohorts(cohort_id),
            request_sha256 TEXT,
            capture_row_count INTEGER CHECK(capture_row_count >= 0),
            capture_row_high_water INTEGER CHECK(capture_row_high_water >= 0),
            entry_row_count INTEGER CHECK(entry_row_count >= 0),
            entry_row_high_water INTEGER CHECK(entry_row_high_water >= 0),
            paused_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (state='open' AND cohort_id IS NULL AND request_sha256 IS NULL
                    AND capture_row_count IS NULL
                    AND capture_row_high_water IS NULL
                    AND entry_row_count IS NULL
                    AND entry_row_high_water IS NULL
                    AND paused_at IS NULL)
                OR
                (state='paused' AND cohort_id IS NOT NULL
                    AND request_sha256 IS NOT NULL
                    AND capture_row_count IS NOT NULL
                    AND capture_row_high_water IS NOT NULL
                    AND entry_row_count IS NOT NULL
                    AND entry_row_high_water IS NOT NULL
                    AND paused_at IS NOT NULL)
            )
        );

        CREATE TABLE journal_cutover_gate_transitions (
            gate_revision INTEGER PRIMARY KEY CHECK(gate_revision >= 1),
            transition_kind TEXT NOT NULL CHECK(transition_kind IN (
                'bootstrap','pause','resume','activate'
            )),
            from_state TEXT,
            to_state TEXT NOT NULL CHECK(to_state IN ('open','paused')),
            cohort_id TEXT,
            request_sha256 TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            capture_row_count INTEGER CHECK(capture_row_count >= 0),
            capture_row_high_water INTEGER CHECK(capture_row_high_water >= 0),
            entry_row_count INTEGER CHECK(entry_row_count >= 0),
            entry_row_high_water INTEGER CHECK(entry_row_high_water >= 0),
            created_at TEXT NOT NULL
        );

        INSERT INTO journal_cutover_gate(
            singleton,state,gate_revision,created_at,updated_at
        ) VALUES(1,'open',1,'1970-01-01T00:00:00+00:00',
            '1970-01-01T00:00:00+00:00');
        INSERT INTO journal_cutover_gate_transitions(
            gate_revision,transition_kind,from_state,to_state,request_sha256,
            actor_json,created_at
        ) VALUES(
            1,'bootstrap',NULL,'open',
            '0000000000000000000000000000000000000000000000000000000000000000',
            '{"kind":"journal_schema_migration","version":20}',
            '1970-01-01T00:00:00+00:00'
        );

        CREATE TRIGGER journal_cutover_pause_capture_insert
        BEFORE INSERT ON journal_captures
        WHEN EXISTS(
            SELECT 1 FROM journal_cutover_gate
            WHERE singleton=1 AND state='paused'
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal_cutover_ingress_paused');
        END;

        CREATE TRIGGER journal_cutover_pause_legacy_entry_insert
        BEFORE INSERT ON journal_entries
        WHEN EXISTS(
            SELECT 1 FROM journal_cutover_gate
            WHERE singleton=1 AND state='paused'
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal_cutover_ingress_paused');
        END;
        """
    )
    _immutable_triggers(conn, ("journal_cutover_gate_transitions",))
    _set_legacy_version(conn, 20)


def _m021_public_actions_and_prompt_generation(conn: sqlite3.Connection) -> None:
    """Add revision Sources and a durable manual prompt-generation boundary."""

    redacted_sha = hashlib.sha256(b"[redacted]").hexdigest()
    _add_column(
        conn,
        "journal_prompt_result_variants",
        "source_ref",
        "TEXT",
    )
    _add_column(
        conn,
        "journal_prompt_result_variants",
        "authorship",
        "TEXT NOT NULL DEFAULT 'generated' CHECK(authorship IN ("
        "'human','ai','mixed','unknown','generated'))",
    )
    _add_column(
        conn,
        "journal_prompt_result_variants",
        "review_state",
        "TEXT NOT NULL DEFAULT 'unreviewed' CHECK(review_state IN ("
        "'not_applicable','unknown','unreviewed','reviewed','rejected'))",
    )
    _add_column(
        conn,
        "journal_prompt_runs",
        "generation_request_id",
        "TEXT",
    )
    conn.executescript(
        """
        CREATE TABLE journal_item_revision_source_dependencies (
            dependency_id TEXT PRIMARY KEY,
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL UNIQUE,
            source_usage_consumer_id TEXT NOT NULL UNIQUE,
            source_ref TEXT NOT NULL,
            representation_id TEXT NOT NULL,
            source_usage_id TEXT UNIQUE,
            purpose TEXT NOT NULL,
            item_id TEXT NOT NULL REFERENCES journal_items(item_id),
            expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
            item_revision INTEGER CHECK(item_revision >= 1),
            operation_kind TEXT NOT NULL CHECK(operation_kind IN (
                'create','edit','correct'
            )),
            content_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'prepared','reserved','bound','acknowledged',
                'redaction_committed','released','aborted'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(item_id,item_revision),
            CHECK (
                (state='prepared' AND source_usage_id IS NULL
                    AND item_revision IS NULL)
                OR
                (state='reserved' AND source_usage_id IS NOT NULL
                    AND item_revision IS NULL)
                OR
                (state IN ('bound','acknowledged')
                    AND source_usage_id IS NOT NULL AND item_revision IS NOT NULL)
                OR
                (state IN ('redaction_committed','released','aborted'))
            )
        );
        CREATE INDEX journal_item_revision_source_dependencies_item_idx
            ON journal_item_revision_source_dependencies(
                item_id,item_revision,state
            );

        CREATE TABLE journal_item_revision_source_redactions (
            redaction_event_id TEXT PRIMARY KEY,
            dependency_id TEXT NOT NULL REFERENCES
                journal_item_revision_source_dependencies(dependency_id),
            source_usage_id TEXT NOT NULL,
            source_usage_consumer_id TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            item_id TEXT NOT NULL REFERENCES journal_items(item_id),
            item_revision INTEGER NOT NULL CHECK(item_revision >= 1),
            scrubbed_current_revision INTEGER,
            redaction_epoch INTEGER NOT NULL CHECK(redaction_epoch >= 1),
            state TEXT NOT NULL CHECK(state IN ('scrubbing','committed')),
            result_sha256 TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(source_usage_id,redaction_epoch),
            CHECK (
                (state='scrubbing' AND result_sha256 IS NULL
                    AND completed_at IS NULL)
                OR
                (state='committed' AND result_sha256 IS NOT NULL
                    AND completed_at IS NOT NULL)
            )
        );

        INSERT INTO journal_item_revision_source_dependencies(
            dependency_id,client_mutation_id,request_sha256,
            source_usage_consumer_id,source_ref,representation_id,
            source_usage_id,purpose,item_id,expected_revision,item_revision,
            operation_kind,content_sha256,state,created_at,updated_at
        )
        SELECT
            dependency_id,client_mutation_id,request_sha256,
            source_usage_consumer_id,source_ref,representation_id,
            source_usage_id,purpose,item_id,0,1,'create',content_sha256,state,
            created_at,updated_at
        FROM journal_native_source_dependencies
        WHERE item_id IS NOT NULL;

        CREATE TABLE journal_prompt_input_source_dependencies (
            dependency_id TEXT PRIMARY KEY,
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL UNIQUE,
            source_usage_consumer_id TEXT NOT NULL UNIQUE,
            source_ref TEXT NOT NULL,
            representation_id TEXT NOT NULL,
            source_usage_id TEXT UNIQUE,
            purpose TEXT NOT NULL,
            interaction_id TEXT NOT NULL UNIQUE REFERENCES
                journal_prompt_interactions(interaction_id),
            input_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'bound','acknowledged','redaction_committed','released'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE journal_prompt_generation_requests (
            request_id TEXT PRIMARY KEY,
            interaction_id TEXT NOT NULL REFERENCES
                journal_prompt_interactions(interaction_id),
            interaction_revision INTEGER NOT NULL CHECK(interaction_revision >= 1),
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL UNIQUE,
            input_sha256 TEXT NOT NULL,
            prompt_definition_sha256 TEXT NOT NULL,
            context_manifest_json TEXT NOT NULL,
            context_manifest_sha256 TEXT NOT NULL,
            requested_by_actor_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'pending','leased','succeeded','failed','canceled'
            )),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            lease_owner TEXT,
            lease_token_sha256 TEXT,
            lease_expires_at TEXT,
            variant_id TEXT UNIQUE REFERENCES journal_prompt_result_variants(variant_id),
            producer_id TEXT,
            provider_id TEXT,
            model_id TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(interaction_id,interaction_revision),
            CHECK (
                (status='pending' AND lease_owner IS NULL
                    AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL
                    AND variant_id IS NULL AND completed_at IS NULL)
                OR
                (status='leased' AND lease_owner IS NOT NULL
                    AND lease_token_sha256 IS NOT NULL AND lease_expires_at IS NOT NULL
                    AND variant_id IS NULL AND completed_at IS NULL)
                OR
                (status='succeeded' AND variant_id IS NOT NULL
                    AND producer_id IS NOT NULL AND completed_at IS NOT NULL)
                OR
                (status IN ('failed','canceled') AND completed_at IS NOT NULL)
            )
        );
        CREATE INDEX journal_prompt_generation_requests_queue_idx
            ON journal_prompt_generation_requests(status,created_at,request_id);

        CREATE TABLE journal_prompt_result_source_dependencies (
            dependency_id TEXT PRIMARY KEY,
            client_mutation_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL UNIQUE,
            source_usage_consumer_id TEXT NOT NULL UNIQUE,
            source_ref TEXT NOT NULL,
            representation_id TEXT NOT NULL,
            source_usage_id TEXT UNIQUE,
            purpose TEXT NOT NULL,
            generation_request_id TEXT NOT NULL UNIQUE REFERENCES
                journal_prompt_generation_requests(request_id),
            interaction_id TEXT NOT NULL REFERENCES
                journal_prompt_interactions(interaction_id),
            variant_id TEXT UNIQUE REFERENCES journal_prompt_result_variants(variant_id),
            result_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'prepared','reserved','bound','acknowledged',
                'redaction_committed','released','aborted'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (state='prepared' AND source_usage_id IS NULL AND variant_id IS NULL)
                OR
                (state='reserved' AND source_usage_id IS NOT NULL AND variant_id IS NULL)
                OR
                (state IN ('bound','acknowledged')
                    AND source_usage_id IS NOT NULL AND variant_id IS NOT NULL)
                OR
                (state IN ('redaction_committed','released','aborted'))
            )
        );

        CREATE TABLE journal_prompt_source_redactions (
            redaction_event_id TEXT PRIMARY KEY,
            dependency_kind TEXT NOT NULL CHECK(dependency_kind IN ('input','result')),
            dependency_id TEXT NOT NULL,
            source_usage_id TEXT NOT NULL,
            source_usage_consumer_id TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            interaction_id TEXT NOT NULL REFERENCES
                journal_prompt_interactions(interaction_id),
            variant_id TEXT REFERENCES journal_prompt_result_variants(variant_id),
            redaction_epoch INTEGER NOT NULL CHECK(redaction_epoch >= 1),
            state TEXT NOT NULL CHECK(state IN ('scrubbing','committed')),
            result_sha256 TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(source_usage_id,redaction_epoch),
            CHECK (
                (dependency_kind='input' AND variant_id IS NULL)
                OR (dependency_kind='result' AND variant_id IS NOT NULL)
            ),
            CHECK (
                (state='scrubbing' AND result_sha256 IS NULL
                    AND completed_at IS NULL)
                OR
                (state='committed' AND result_sha256 IS NOT NULL
                    AND completed_at IS NOT NULL)
            )
        );
        """
    )
    conn.executescript(
        f"""
        DROP TRIGGER IF EXISTS journal_item_revisions_immutable_update;
        CREATE TRIGGER journal_item_revisions_immutable_update
        BEFORE UPDATE ON journal_item_revisions
        WHEN NOT (
            NEW.item_id = OLD.item_id
            AND NEW.revision = OLD.revision
            AND NEW.authority_kind = OLD.authority_kind
            AND NEW.plain_value = '[redacted]'
            AND NEW.content_sha256 = '{redacted_sha}'
            AND NEW.lifecycle = 'tombstoned'
            AND NEW.actor_json = OLD.actor_json
            AND COALESCE(NEW.source_ref,'') = COALESCE(OLD.source_ref,'')
            AND NEW.authorship = OLD.authorship
            AND NEW.review_state = OLD.review_state
            AND NEW.intent_id = OLD.intent_id
            AND NEW.created_at = OLD.created_at
            AND (
                EXISTS (
                    SELECT 1 FROM journal_native_redactions AS redaction
                    WHERE redaction.item_id=OLD.item_id
                      AND redaction.state='scrubbing'
                )
                OR EXISTS (
                    SELECT 1
                    FROM journal_import_source_redactions AS redaction
                    JOIN journal_import_spans AS span
                      ON span.cohort_id=redaction.cohort_id
                     AND span.file_id=redaction.file_id
                     AND span.item_id=OLD.item_id
                    WHERE redaction.state='scrubbing'
                )
                OR EXISTS (
                    SELECT 1 FROM journal_native_source_redactions AS redaction
                    WHERE redaction.item_id=OLD.item_id
                      AND redaction.state='scrubbing'
                )
                OR EXISTS (
                    SELECT 1
                    FROM journal_item_revision_source_redactions AS redaction
                    WHERE redaction.item_id=OLD.item_id
                      AND redaction.item_revision=OLD.revision
                      AND redaction.state='scrubbing'
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal_item_revisions_is_immutable');
        END;
        """
    )
    _set_legacy_version(conn, 21)


def _m022_postseal_cutover_maintenance(conn: sqlite3.Connection) -> None:
    """Retain the Journal write fence until post-seal evidence is released."""

    conn.executescript(
        """
        DROP TRIGGER journal_cutover_gate_transitions_immutable_update;
        DROP TRIGGER journal_cutover_gate_transitions_immutable_delete;
        ALTER TABLE journal_cutover_gate_transitions
          RENAME TO journal_cutover_gate_transitions_v20;
        CREATE TABLE journal_cutover_gate_transitions (
            gate_revision INTEGER PRIMARY KEY CHECK(gate_revision >= 1),
            transition_kind TEXT NOT NULL CHECK(transition_kind IN (
                'bootstrap','pause','resume','activate','release'
            )),
            from_state TEXT,
            to_state TEXT NOT NULL CHECK(to_state IN ('open','paused')),
            cohort_id TEXT,
            request_sha256 TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            capture_row_count INTEGER CHECK(capture_row_count >= 0),
            capture_row_high_water INTEGER CHECK(capture_row_high_water >= 0),
            entry_row_count INTEGER CHECK(entry_row_count >= 0),
            entry_row_high_water INTEGER CHECK(entry_row_high_water >= 0),
            created_at TEXT NOT NULL
        );
        INSERT INTO journal_cutover_gate_transitions
        SELECT * FROM journal_cutover_gate_transitions_v20;
        DROP TABLE journal_cutover_gate_transitions_v20;

        CREATE TABLE cutover_maintenance (
            singleton                    INTEGER PRIMARY KEY CHECK(singleton=1),
            domain                       TEXT NOT NULL CHECK(domain='journal'),
            state                        TEXT NOT NULL CHECK(state IN
                                           ('open','preseal_fenced','postseal_pending','recovery')),
            cohort_id                    TEXT,
            inventory_sha256             TEXT,
            fence_id                     TEXT,
            pause_request_sha256          TEXT,
            paused_at                    TEXT,
            postseal_evidence_sha256      TEXT,
            released_at                  TEXT,
            updated_at                   TEXT NOT NULL,
            CHECK(
              (state='open') OR
              (cohort_id IS NOT NULL AND inventory_sha256 IS NOT NULL
               AND fence_id IS NOT NULL AND pause_request_sha256 IS NOT NULL
               AND paused_at IS NOT NULL)
            )
        );

        CREATE TABLE cutover_maintenance_receipts (
            mutation_id          TEXT PRIMARY KEY,
            operation            TEXT NOT NULL,
            request_sha256       TEXT NOT NULL,
            result_json          TEXT NOT NULL,
            result_sha256        TEXT NOT NULL,
            created_at           TEXT NOT NULL
        );

        INSERT INTO cutover_maintenance(
            singleton,domain,state,cohort_id,inventory_sha256,fence_id,
            pause_request_sha256,paused_at,updated_at
        )
        SELECT
            1,'journal',
            CASE
              WHEN gate.state='open' THEN 'open'
              WHEN authority.activated_cohort_id IS NOT NULL THEN 'postseal_pending'
              ELSE 'preseal_fenced'
            END,
            CASE WHEN gate.state='paused' THEN gate.cohort_id END,
            CASE WHEN gate.state='paused' THEN cohort.inventory_sha256 END,
            CASE WHEN gate.state='paused'
                 THEN 'migrated-journal-gate-' || gate.gate_revision END,
            CASE WHEN gate.state='paused' THEN gate.request_sha256 END,
            CASE WHEN gate.state='paused' THEN gate.paused_at END,
            gate.updated_at
        FROM journal_cutover_gate AS gate
        CROSS JOIN journal_authority_control AS authority
        LEFT JOIN journal_import_cohorts AS cohort
          ON cohort.cohort_id=gate.cohort_id
        WHERE gate.singleton=1 AND authority.singleton=1;

        CREATE TABLE journal_cutover_release_receipts (
            mutation_id                    TEXT PRIMARY KEY,
            request_sha256                 TEXT NOT NULL,
            domain                         TEXT NOT NULL CHECK(domain='journal'),
            cohort_id                      TEXT NOT NULL,
            inventory_sha256               TEXT NOT NULL,
            actor_sha256                   TEXT NOT NULL,
            evidence_sha256                TEXT NOT NULL,
            database_checkpoint_sha256     TEXT NOT NULL,
            search_sha256                  TEXT NOT NULL,
            detachment_sha256              TEXT NOT NULL,
            authority_head_sha256          TEXT NOT NULL,
            high_water_sha256              TEXT NOT NULL,
            checkpoint_path_sha256         TEXT,
            search_path_sha256             TEXT,
            detachment_path_sha256         TEXT,
            released_at                    TEXT NOT NULL,
            result_json                    TEXT NOT NULL,
            result_sha256                  TEXT NOT NULL,
            created_at                     TEXT NOT NULL
        );
        """
    )
    _immutable_triggers(
        conn,
        (
            "journal_cutover_gate_transitions",
            "cutover_maintenance_receipts",
            "journal_cutover_release_receipts",
        ),
    )
    _set_legacy_version(conn, 22)


def _m023_controlled_postseal_source_drain(conn: sqlite3.Connection) -> None:
    """Bind and receipt the exact Source commands drained behind the fence."""

    # v22 was never a released schema.  Refuse to bless an already-open v22
    # rehearsal as if it had supplied the controlled Source-delta proof.
    if conn.execute(
        "SELECT 1 FROM journal_cutover_release_receipts LIMIT 1"
    ).fetchone() is not None:
        raise RuntimeError(
            "pre-v23 Journal release receipts require a fresh isolated rehearsal"
        )
    _add_column(
        conn,
        "journal_cutover_release_receipts",
        "source_drain_mutation_id",
        "TEXT",
    )
    _add_column(
        conn,
        "journal_cutover_release_receipts",
        "source_drain_result_sha256",
        "TEXT",
    )
    _add_column(
        conn,
        "journal_cutover_release_receipts",
        "source_effect_set_sha256",
        "TEXT",
    )
    _add_column(
        conn,
        "journal_cutover_release_receipts",
        "source_effect_max_rowid",
        "INTEGER",
    )
    conn.executescript(
        """
        CREATE TABLE journal_cutover_source_drain_batches (
            mutation_id                    TEXT PRIMARY KEY,
            request_sha256                 TEXT NOT NULL,
            cohort_id                      TEXT NOT NULL,
            actor_sha256                   TEXT NOT NULL,
            source_authority_id            TEXT NOT NULL,
            source_db_path_sha256          TEXT NOT NULL,
            source_effect_count            INTEGER NOT NULL CHECK(source_effect_count >= 0),
            source_effect_max_rowid        INTEGER NOT NULL CHECK(source_effect_max_rowid >= 0),
            source_baseline_set_sha256     TEXT NOT NULL,
            bound_effect_count             INTEGER NOT NULL CHECK(bound_effect_count >= 0),
            bound_effect_set_sha256        TEXT NOT NULL,
            baseline_capture_count         INTEGER NOT NULL CHECK(baseline_capture_count >= 0),
            baseline_capture_max_rowid     INTEGER NOT NULL CHECK(baseline_capture_max_rowid >= 0),
            previous_drain_result_sha256   TEXT,
            created_at                     TEXT NOT NULL
        );

        CREATE TABLE journal_cutover_source_drain_effects (
            batch_mutation_id      TEXT NOT NULL REFERENCES
                                      journal_cutover_source_drain_batches(mutation_id),
            ordinal                INTEGER NOT NULL CHECK(ordinal >= 0),
            source_rowid           INTEGER NOT NULL CHECK(source_rowid >= 1),
            effect_id              TEXT NOT NULL,
            payload_sha256         TEXT NOT NULL,
            PRIMARY KEY(batch_mutation_id,effect_id),
            UNIQUE(batch_mutation_id,ordinal),
            UNIQUE(batch_mutation_id,source_rowid),
            UNIQUE(effect_id)
        );

        CREATE TABLE journal_cutover_source_drain_captures (
            batch_mutation_id      TEXT NOT NULL,
            effect_id              TEXT NOT NULL,
            capture_id             TEXT NOT NULL UNIQUE,
            capture_rowid          INTEGER NOT NULL CHECK(capture_rowid >= 1),
            capture_request_sha256 TEXT NOT NULL,
            created_at             TEXT NOT NULL,
            PRIMARY KEY(batch_mutation_id,effect_id),
            FOREIGN KEY(batch_mutation_id,effect_id) REFERENCES
                journal_cutover_source_drain_effects(batch_mutation_id,effect_id)
        );

        CREATE TABLE journal_cutover_source_drain_receipts (
            batch_mutation_id          TEXT PRIMARY KEY REFERENCES
                                          journal_cutover_source_drain_batches(mutation_id),
            request_sha256             TEXT NOT NULL,
            source_ack_set_sha256      TEXT NOT NULL,
            capture_set_sha256         TEXT NOT NULL,
            post_capture_count         INTEGER NOT NULL CHECK(post_capture_count >= 0),
            post_capture_max_rowid     INTEGER NOT NULL CHECK(post_capture_max_rowid >= 0),
            post_entry_count           INTEGER NOT NULL CHECK(post_entry_count >= 0),
            post_entry_max_rowid       INTEGER NOT NULL CHECK(post_entry_max_rowid >= 0),
            result_json                TEXT NOT NULL,
            result_sha256              TEXT NOT NULL,
            completed_at               TEXT NOT NULL
        );

        CREATE INDEX journal_cutover_source_drain_batches_cohort_idx
            ON journal_cutover_source_drain_batches(cohort_id,source_effect_max_rowid);

        DROP TRIGGER journal_cutover_pause_capture_insert;
        CREATE TRIGGER journal_cutover_pause_capture_insert
        BEFORE INSERT ON journal_captures
        WHEN EXISTS(
            SELECT 1 FROM journal_cutover_gate
            WHERE singleton=1 AND state='paused'
        ) AND NOT EXISTS(
            SELECT 1
            FROM journal_cutover_source_drain_effects AS effect
            JOIN journal_cutover_source_drain_batches AS batch
              ON batch.mutation_id=effect.batch_mutation_id
            CROSS JOIN journal_authority_control AS authority
            CROSS JOIN cutover_maintenance AS maintenance
            LEFT JOIN journal_cutover_source_drain_receipts AS receipt
              ON receipt.batch_mutation_id=batch.mutation_id
            WHERE effect.effect_id=NEW.source_effect_id
              AND authority.singleton=1
              AND authority.mode='database_only'
              AND authority.activated_cohort_id=batch.cohort_id
              AND maintenance.singleton=1
              AND maintenance.state='postseal_pending'
              AND maintenance.cohort_id=batch.cohort_id
              AND receipt.batch_mutation_id IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal_cutover_ingress_paused');
        END;
        """
    )
    _immutable_triggers(
        conn,
        (
            "journal_cutover_source_drain_batches",
            "journal_cutover_source_drain_effects",
            "journal_cutover_source_drain_captures",
            "journal_cutover_source_drain_receipts",
        ),
    )
    _set_legacy_version(conn, 23)


def _m024_document_module_bindings(conn: sqlite3.Connection) -> None:
    """Mirror content-free Journal document-module navigation bindings."""

    conn.executescript(
        """
        CREATE TABLE journal_module_document_bindings (
            local_date TEXT NOT NULL,
            module_instance_id TEXT NOT NULL,
            module_instance_version INTEGER NOT NULL,
            domain_entity_id TEXT NOT NULL,
            binding_id TEXT NOT NULL UNIQUE,
            store_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            role TEXT NOT NULL,
            cowork_href TEXT NOT NULL,
            content_authority_epoch INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(local_date,module_instance_id,module_instance_version),
            FOREIGN KEY(module_instance_id,module_instance_version) REFERENCES
                journal_module_instance_versions(module_instance_id,instance_version),
            CHECK(length(local_date) = 10),
            CHECK(module_instance_version >= 1),
            CHECK(content_authority_epoch >= 1)
        );
        CREATE UNIQUE INDEX journal_module_document_target_uq
            ON journal_module_document_bindings(store_id,document_id);
        """
    )
    _set_legacy_version(conn, 24)


class JournalMigrationRunner(MigrationRunner):
    """Adopt the old meta-versioned Journal schema without guessing."""

    def _verify_history_hashes(
        self,
        conn: sqlite3.Connection,
        current_version: int,
    ) -> None:
        # Historical v1..v7 source did not exist as migration callables.  Once
        # baseline-stamped, those rows document adoption rather than a callable
        # whose hash can be audited.  Native migrations retain strict hashes.
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
                    "UPDATE _migration_history SET code_hash=?, hash_format=? WHERE version=?",
                    (current_hash, HASH_FORMAT_CURRENT, migration.version),
                )
            elif stored_hash != current_hash:
                raise MigrationHashMismatch(
                    f"{self.name}: native migration v{migration.version} was applied "
                    "from different source; add a new migration instead."
                )

    def _infer_baseline_version(self, conn: sqlite3.Connection) -> int:
        tables = _tables(conn)
        if not tables:
            return 0
        if "journal_meta" not in tables or "journal_captures" not in tables:
            raise RuntimeError("journal database contains an unrecognized pre-migration schema")
        row = conn.execute(
            "SELECT value FROM journal_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise RuntimeError("journal database is missing its historical schema version")
        try:
            version = int(row[0])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("journal database has an invalid historical schema version") from exc
        if version < 1 or version > LEGACY_SCHEMA_VERSION:
            raise RuntimeError(
                f"journal database has unsupported informal schema version {version}"
            )
        return version


JOURNAL_MIGRATIONS = JournalMigrationRunner(
    "journal_capture",
    migrations=[
        Migration(1, "bootstrap historical v7 schema", _m001_bootstrap_historical_v7),
        Migration(2, "historical source usage identity", _m002_source_usage),
        Migration(3, "historical v3 marker", _m003_historical_noop),
        Migration(4, "historical document dependency metadata", _m004_document_dependency_metadata),
        Migration(5, "historical migration ledgers", _m005_migration_ledgers),
        Migration(6, "historical structural parity", _m006_structural_parity),
        Migration(7, "historical effect receipts", _m007_effect_receipts),
        Migration(8, "immutable Journal configuration revisions", _m008_immutable_configuration),
        Migration(9, "logical days and immutable composition snapshots", _m009_day_compositions),
        Migration(
            10,
            "native items, typed observations, and prompt result lineage",
            _m010_native_content,
        ),
        Migration(
            11,
            "transactional search outbox and legacy entry bridge",
            _m011_search_outbox_and_legacy_bridge,
        ),
        Migration(12, "staged legacy Journal import cohorts", _m012_staged_history_import_cohorts),
        Migration(
            13,
            "fenced database-only Journal authority cutover",
            _m013_journal_authority_cutover,
        ),
        Migration(
            14,
            "authority-gated import publication visibility",
            _m014_import_publication_visibility,
        ),
        Migration(
            15,
            "legacy import Source dependencies and redaction",
            _m015_import_source_dependencies,
        ),
        Migration(
            16,
            "generic native item Source dependencies and redaction",
            _m016_generic_native_source_dependencies,
        ),
        Migration(
            17,
            "typed field-value Source dependencies and redaction",
            _m017_typed_field_source_dependencies,
        ),
        Migration(
            18,
            "typed field-value revision provenance",
            _m018_field_revision_provenance,
        ),
        Migration(
            19,
            "staged typed import profiles and observations",
            _m019_staged_typed_import_profiles,
        ),
        Migration(
            20,
            "durable pre-seal Journal ingress pause",
            _m020_cutover_ingress_pause,
        ),
        Migration(
            21,
            "public item actions and durable prompt generation",
            _m021_public_actions_and_prompt_generation,
        ),
        Migration(
            22,
            "durable post-seal Journal maintenance fence",
            _m022_postseal_cutover_maintenance,
        ),
        Migration(
            23,
            "controlled post-seal Source drain receipts",
            _m023_controlled_postseal_source_drain,
        ),
        Migration(
            24,
            "content-free Journal document module bindings",
            _m024_document_module_bindings,
        ),
    ],
)


def migrate(conn: sqlite3.Connection) -> None:
    JOURNAL_MIGRATIONS.run(conn)
