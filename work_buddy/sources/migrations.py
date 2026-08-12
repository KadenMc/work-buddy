"""Versioned SQLite schema for the machine-level Sources store."""

from __future__ import annotations

import sqlite3

from work_buddy.storage.migrations import Migration, MigrationRunner


SCHEMA_VERSION = 3


def _m001_sources_schema(conn: sqlite3.Connection) -> None:
    """Create the retained-source, authorization, delivery, and audit schema."""

    statements = (
        """
        CREATE TABLE IF NOT EXISTS source_store_info (
            singleton        INTEGER PRIMARY KEY CHECK(singleton = 1),
            authority_id     TEXT NOT NULL UNIQUE,
            schema_version   INTEGER NOT NULL CHECK(schema_version >= 1),
            created_at       TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_authorities (
            authority_id       TEXT PRIMARY KEY,
            custody_kind       TEXT NOT NULL CHECK(custody_kind IN ('local','foreign')),
            imported_at        TEXT,
            import_id          TEXT,
            created_at         TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_blobs (
            content_sha256   TEXT PRIMARY KEY,
            relative_path   TEXT NOT NULL UNIQUE,
            byte_length     INTEGER NOT NULL CHECK(byte_length >= 0),
            ref_count       INTEGER NOT NULL CHECK(ref_count >= 0),
            created_at      TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_items (
            authority_id               TEXT NOT NULL REFERENCES source_authorities(authority_id),
            source_item_id              TEXT NOT NULL,
            custodian_authority_id      TEXT NOT NULL,
            ref_schema                  TEXT NOT NULL,
            primary_representation_id   TEXT NOT NULL,
            origin_ref_json             TEXT,
            native_revision             TEXT,
            source_role                 TEXT NOT NULL,
            fidelity                    TEXT NOT NULL,
            tenant_scope_id             TEXT NOT NULL,
            originating_surface         TEXT NOT NULL,
            namespace                   TEXT,
            sensitivity_class           TEXT NOT NULL,
            retention_class             TEXT NOT NULL,
            occurred_at                 TEXT,
            provider_observed_at         TEXT,
            received_at                 TEXT NOT NULL,
            committed_at                TEXT NOT NULL,
            lifecycle_state             TEXT NOT NULL DEFAULT 'active'
                CHECK(lifecycle_state IN ('active','tombstoned','redacted')),
            redaction_epoch             INTEGER NOT NULL DEFAULT 0 CHECK(redaction_epoch >= 0),
            redaction_event_id           TEXT,
            PRIMARY KEY(authority_id, source_item_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_representations (
            representation_id             TEXT PRIMARY KEY,
            authority_id                  TEXT NOT NULL,
            source_item_id                 TEXT NOT NULL,
            representation_kind            TEXT NOT NULL,
            media_type                     TEXT NOT NULL,
            schema_type                    TEXT,
            character_encoding             TEXT,
            content_sha256                 TEXT NOT NULL,
            byte_length                    INTEGER NOT NULL CHECK(byte_length >= 0),
            character_length               INTEGER CHECK(character_length IS NULL OR character_length >= 0),
            inline_content                 BLOB,
            blob_sha256                    TEXT REFERENCES source_blobs(content_sha256),
            is_primary                     INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
            derived_from_representation_id TEXT REFERENCES source_representations(representation_id),
            derivation_relation            TEXT,
            producer_ref_json              TEXT,
            created_at                     TEXT NOT NULL,
            redacted_at                    TEXT,
            FOREIGN KEY(authority_id, source_item_id)
                REFERENCES source_items(authority_id, source_item_id),
            CHECK(
                (redacted_at IS NOT NULL AND inline_content IS NULL AND blob_sha256 IS NULL)
                OR (redacted_at IS NULL AND (
                    (inline_content IS NOT NULL AND blob_sha256 IS NULL)
                    OR (inline_content IS NULL AND blob_sha256 IS NOT NULL)
                ))
            )
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_source_primary_representation
        ON source_representations(authority_id, source_item_id)
        WHERE is_primary = 1
        """,
        """
        CREATE TABLE IF NOT EXISTS source_attributions (
            attribution_id       TEXT PRIMARY KEY,
            authority_id        TEXT NOT NULL,
            source_item_id       TEXT NOT NULL,
            representation_id   TEXT REFERENCES source_representations(representation_id),
            role                TEXT NOT NULL,
            actor_ref_json      TEXT,
            attribution_state   TEXT NOT NULL CHECK(attribution_state IN ('identified','unknown','mixed')),
            basis               TEXT NOT NULL,
            assurance           TEXT NOT NULL,
            selector_json       TEXT,
            asserted_by_json    TEXT,
            observed_at         TEXT NOT NULL,
            supersedes_id       TEXT REFERENCES source_attributions(attribution_id),
            created_at          TEXT NOT NULL,
            FOREIGN KEY(authority_id, source_item_id)
                REFERENCES source_items(authority_id, source_item_id),
            CHECK(
                (attribution_state = 'identified' AND actor_ref_json IS NOT NULL)
                OR (attribution_state != 'identified' AND actor_ref_json IS NULL)
            )
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_observations (
            observation_id        TEXT PRIMARY KEY,
            authority_id          TEXT NOT NULL,
            source_item_id         TEXT NOT NULL,
            observation_kind      TEXT NOT NULL,
            resolver_id           TEXT NOT NULL,
            resolver_version      TEXT NOT NULL,
            observed_at           TEXT NOT NULL,
            native_revision       TEXT,
            native_content_sha256 TEXT,
            retained_sha256       TEXT,
            status                TEXT NOT NULL,
            error_code            TEXT,
            metadata_json         TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(authority_id, source_item_id)
                REFERENCES source_items(authority_id, source_item_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_derivations (
            derivation_id          TEXT PRIMARY KEY,
            derived_authority_id   TEXT NOT NULL,
            derived_item_id        TEXT NOT NULL,
            input_authority_id     TEXT NOT NULL,
            input_item_id          TEXT NOT NULL,
            relation               TEXT NOT NULL,
            producer_ref_json      TEXT NOT NULL,
            activity_id            TEXT NOT NULL,
            selector_json          TEXT,
            method_json            TEXT NOT NULL,
            fidelity               TEXT NOT NULL,
            created_at             TEXT NOT NULL,
            FOREIGN KEY(derived_authority_id, derived_item_id)
                REFERENCES source_items(authority_id, source_item_id),
            FOREIGN KEY(input_authority_id, input_item_id)
                REFERENCES source_items(authority_id, source_item_id),
            UNIQUE(derived_authority_id, derived_item_id, input_authority_id,
                   input_item_id, relation, activity_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_idempotency (
            authority_id             TEXT NOT NULL,
            tenant_scope_id          TEXT NOT NULL,
            issuer_ref_json          TEXT NOT NULL,
            principal_ref_json       TEXT NOT NULL,
            client_mutation_id       TEXT NOT NULL,
            request_sha256           TEXT NOT NULL,
            result_json              TEXT NOT NULL,
            created_at               TEXT NOT NULL,
            PRIMARY KEY(authority_id, tenant_scope_id, issuer_ref_json,
                        principal_ref_json, client_mutation_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_access_bindings (
            binding_id                   TEXT PRIMARY KEY,
            authority_id                 TEXT NOT NULL,
            source_item_id                TEXT NOT NULL,
            principal_ref_json           TEXT NOT NULL,
            trusted_service_id           TEXT,
            purpose                      TEXT NOT NULL,
            access_mode                  TEXT NOT NULL CHECK(access_mode IN ('metadata','content')),
            scope_json                   TEXT NOT NULL,
            external_recipient           TEXT,
            model_id                     TEXT,
            egress_class                 TEXT,
            content_boundary_json        TEXT,
            authorization_fingerprint    TEXT NOT NULL,
            gesture_receipt_id            TEXT,
            expires_at                   TEXT,
            revoked_at                   TEXT,
            created_at                   TEXT NOT NULL,
            FOREIGN KEY(authority_id, source_item_id)
                REFERENCES source_items(authority_id, source_item_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_source_access_lookup
        ON source_access_bindings(authority_id, source_item_id, purpose, principal_ref_json)
        """,
        """
        CREATE TABLE IF NOT EXISTS source_access_audit (
            audit_id                  TEXT PRIMARY KEY,
            binding_id                TEXT NOT NULL REFERENCES source_access_bindings(binding_id),
            authority_id              TEXT NOT NULL,
            source_item_id             TEXT NOT NULL,
            representation_id         TEXT,
            access_mode               TEXT NOT NULL,
            purpose                   TEXT NOT NULL,
            authorization_context_sha256 TEXT NOT NULL,
            accessed_at               TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_usage_intents (
            usage_id                 TEXT PRIMARY KEY,
            authority_id             TEXT NOT NULL,
            source_item_id            TEXT NOT NULL,
            representation_id        TEXT NOT NULL REFERENCES source_representations(representation_id),
            selector_json            TEXT,
            principal_ref_json       TEXT NOT NULL,
            purpose                  TEXT NOT NULL,
            consumer_domain          TEXT NOT NULL,
            consumer_id              TEXT NOT NULL,
            use_kind                 TEXT NOT NULL,
            disclosure_kind          TEXT NOT NULL,
            redaction_policy         TEXT NOT NULL,
            access_binding_id        TEXT NOT NULL REFERENCES source_access_bindings(binding_id),
            bound_redaction_epoch    INTEGER NOT NULL,
            request_sha256           TEXT NOT NULL,
            status                   TEXT NOT NULL CHECK(status IN ('reserved','acknowledged','released')),
            maintenance_state        TEXT NOT NULL DEFAULT 'clean'
                CHECK(maintenance_state IN ('clean','pending_redaction','completed')),
            created_at               TEXT NOT NULL,
            acknowledged_at          TEXT,
            released_at              TEXT,
            UNIQUE(consumer_domain, consumer_id, use_kind)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_usage_ack_idempotency (
            acknowledgement_key  TEXT PRIMARY KEY,
            usage_id             TEXT NOT NULL REFERENCES source_usage_intents(usage_id),
            request_sha256       TEXT NOT NULL,
            acknowledged_at      TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ingress_submissions (
            submission_id            TEXT PRIMARY KEY,
            authority_id             TEXT NOT NULL,
            source_item_id            TEXT NOT NULL,
            representation_id        TEXT NOT NULL REFERENCES source_representations(representation_id),
            issuer_ref_json          TEXT NOT NULL,
            inputter_ref_json        TEXT NOT NULL,
            input_mode               TEXT NOT NULL,
            gesture_receipt_id        TEXT,
            authorization_fingerprint TEXT NOT NULL,
            occurred_at              TEXT,
            received_at              TEXT NOT NULL,
            committed_at             TEXT NOT NULL,
            FOREIGN KEY(authority_id, source_item_id)
                REFERENCES source_items(authority_id, source_item_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_commands (
            command_id                 TEXT PRIMARY KEY,
            submission_id              TEXT NOT NULL REFERENCES ingress_submissions(submission_id),
            command_schema             TEXT NOT NULL,
            target_domain              TEXT NOT NULL,
            command_type               TEXT NOT NULL,
            parameters_json            TEXT NOT NULL,
            parameters_sha256          TEXT NOT NULL,
            authorization_fingerprint  TEXT NOT NULL,
            authorization_expires_at   TEXT,
            created_at                 TEXT NOT NULL,
            UNIQUE(submission_id, target_domain, command_type)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_outbox (
            effect_id                    TEXT PRIMARY KEY,
            command_id                   TEXT REFERENCES source_commands(command_id),
            target_domain                TEXT NOT NULL,
            effect_type                  TEXT NOT NULL,
            payload_json                 TEXT NOT NULL,
            payload_sha256               TEXT NOT NULL,
            authorization_fingerprint    TEXT NOT NULL,
            authorization_expires_at     TEXT,
            status                       TEXT NOT NULL
                CHECK(status IN ('pending','leased','retryable','succeeded','failed_terminal','paused')),
            lease_owner                  TEXT,
            lease_until                  TEXT,
            attempts                     INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            result_ref                   TEXT,
            result_sha256                TEXT,
            error_code                   TEXT,
            created_at                   TEXT NOT NULL,
            updated_at                   TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_source_outbox_ready
        ON source_outbox(status, lease_until, created_at)
        """,
        """
        CREATE TABLE IF NOT EXISTS source_effect_receipts (
            receipt_id       TEXT PRIMARY KEY,
            effect_id        TEXT NOT NULL UNIQUE REFERENCES source_outbox(effect_id),
            target_domain    TEXT NOT NULL,
            result_ref       TEXT NOT NULL,
            result_sha256    TEXT NOT NULL,
            received_at      TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_redaction_events (
            redaction_event_id       TEXT PRIMARY KEY,
            authority_id             TEXT NOT NULL,
            source_item_id            TEXT NOT NULL,
            prior_redaction_epoch    INTEGER NOT NULL,
            redaction_epoch          INTEGER NOT NULL,
            actor_ref_json           TEXT NOT NULL,
            authorization_fingerprint TEXT NOT NULL,
            reason_code              TEXT NOT NULL,
            managed_copy_state       TEXT NOT NULL,
            issued_copy_state        TEXT NOT NULL,
            created_at               TEXT NOT NULL,
            FOREIGN KEY(authority_id, source_item_id)
                REFERENCES source_items(authority_id, source_item_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_origin_identities (
            provider_id          TEXT NOT NULL,
            occurrence_key       TEXT NOT NULL,
            native_part          TEXT NOT NULL DEFAULT '',
            native_revision      TEXT NOT NULL DEFAULT '',
            authority_id         TEXT NOT NULL,
            source_item_id        TEXT NOT NULL,
            content_sha256       TEXT NOT NULL,
            created_at           TEXT NOT NULL,
            PRIMARY KEY(provider_id, occurrence_key, native_part, native_revision),
            FOREIGN KEY(authority_id, source_item_id)
                REFERENCES source_items(authority_id, source_item_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_imports (
            import_id                    TEXT PRIMARY KEY,
            export_authority_id          TEXT NOT NULL,
            custodian_authority_id       TEXT NOT NULL,
            manifest_sha256              TEXT NOT NULL,
            authorization_fingerprint    TEXT NOT NULL,
            imported_at                  TEXT NOT NULL,
            item_count                   INTEGER NOT NULL,
            reused_count                 INTEGER NOT NULL,
            remapped_count               INTEGER NOT NULL,
            quarantined_count            INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_import_mappings (
            import_id             TEXT NOT NULL REFERENCES source_imports(import_id),
            original_authority_id TEXT NOT NULL,
            original_item_id      TEXT NOT NULL,
            local_authority_id    TEXT NOT NULL,
            local_item_id         TEXT NOT NULL,
            mapping_kind          TEXT NOT NULL CHECK(mapping_kind IN ('preserved','reused','remapped')),
            PRIMARY KEY(import_id, original_authority_id, original_item_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_import_quarantine (
            quarantine_id          TEXT PRIMARY KEY,
            import_id              TEXT NOT NULL,
            original_authority_id  TEXT NOT NULL,
            original_item_id       TEXT NOT NULL,
            reason_code            TEXT NOT NULL,
            record_sha256          TEXT NOT NULL,
            quarantined_at         TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_imported_usage_audit (
            import_id           TEXT NOT NULL REFERENCES source_imports(import_id),
            original_usage_id   TEXT NOT NULL,
            authority_id        TEXT NOT NULL,
            source_item_id       TEXT NOT NULL,
            record_json         TEXT NOT NULL,
            imported_at         TEXT NOT NULL,
            PRIMARY KEY(import_id, original_usage_id)
        )
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_items_no_delete
        BEFORE DELETE ON source_items
        BEGIN
            SELECT RAISE(ABORT, 'source items cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_items_immutable
        BEFORE UPDATE ON source_items
        WHEN NEW.authority_id IS NOT OLD.authority_id
          OR NEW.source_item_id IS NOT OLD.source_item_id
          OR NEW.custodian_authority_id IS NOT OLD.custodian_authority_id
          OR NEW.ref_schema IS NOT OLD.ref_schema
          OR NEW.primary_representation_id IS NOT OLD.primary_representation_id
          OR NEW.origin_ref_json IS NOT OLD.origin_ref_json
          OR NEW.native_revision IS NOT OLD.native_revision
          OR NEW.source_role IS NOT OLD.source_role
          OR NEW.fidelity IS NOT OLD.fidelity
          OR NEW.tenant_scope_id IS NOT OLD.tenant_scope_id
          OR NEW.originating_surface IS NOT OLD.originating_surface
          OR NEW.namespace IS NOT OLD.namespace
          OR NEW.sensitivity_class IS NOT OLD.sensitivity_class
          OR NEW.retention_class IS NOT OLD.retention_class
          OR NEW.occurred_at IS NOT OLD.occurred_at
          OR NEW.provider_observed_at IS NOT OLD.provider_observed_at
          OR NEW.received_at IS NOT OLD.received_at
          OR NEW.committed_at IS NOT OLD.committed_at
        BEGIN
            SELECT RAISE(ABORT, 'source item identity and capture facts are immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_representation_no_delete
        BEFORE DELETE ON source_representations
        BEGIN
            SELECT RAISE(ABORT, 'source representations cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_representation_redaction_only
        BEFORE UPDATE ON source_representations
        WHEN NEW.representation_id IS NOT OLD.representation_id
          OR NEW.authority_id IS NOT OLD.authority_id
          OR NEW.source_item_id IS NOT OLD.source_item_id
          OR NEW.representation_kind IS NOT OLD.representation_kind
          OR NEW.media_type IS NOT OLD.media_type
          OR NEW.schema_type IS NOT OLD.schema_type
          OR NEW.character_encoding IS NOT OLD.character_encoding
          OR NEW.content_sha256 IS NOT OLD.content_sha256
          OR NEW.byte_length IS NOT OLD.byte_length
          OR NEW.character_length IS NOT OLD.character_length
          OR NEW.is_primary IS NOT OLD.is_primary
          OR NEW.derived_from_representation_id IS NOT OLD.derived_from_representation_id
          OR NEW.derivation_relation IS NOT OLD.derivation_relation
          OR NEW.producer_ref_json IS NOT OLD.producer_ref_json
          OR NEW.created_at IS NOT OLD.created_at
          OR (OLD.inline_content IS NULL AND NEW.inline_content IS NOT NULL)
          OR (OLD.blob_sha256 IS NULL AND NEW.blob_sha256 IS NOT NULL)
          OR (OLD.redacted_at IS NOT NULL AND NEW.redacted_at IS NULL)
        BEGIN
            SELECT RAISE(ABORT, 'source representation is immutable except for readable-content removal');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_attribution_append_only_update
        BEFORE UPDATE ON source_attributions
        BEGIN SELECT RAISE(ABORT, 'source attributions are append-only'); END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_attribution_append_only_delete
        BEFORE DELETE ON source_attributions
        BEGIN SELECT RAISE(ABORT, 'source attributions are append-only'); END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_observation_append_only_update
        BEFORE UPDATE ON source_observations
        BEGIN SELECT RAISE(ABORT, 'source observations are append-only'); END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_observation_append_only_delete
        BEFORE DELETE ON source_observations
        BEGIN SELECT RAISE(ABORT, 'source observations are append-only'); END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_derivation_append_only_update
        BEFORE UPDATE ON source_derivations
        BEGIN SELECT RAISE(ABORT, 'source derivations are append-only'); END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_derivation_append_only_delete
        BEFORE DELETE ON source_derivations
        BEGIN SELECT RAISE(ABORT, 'source derivations are append-only'); END
        """,
    )
    for statement in statements:
        conn.execute(statement)


def _m002_recoverable_exports(conn: sqlite3.Connection) -> None:
    """Persist issued-copy export intent before any source is resolved."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_export_operations (
            export_id                   TEXT PRIMARY KEY,
            idempotency_key             TEXT NOT NULL UNIQUE,
            request_sha256              TEXT NOT NULL,
            destination                 TEXT NOT NULL,
            include_content             INTEGER NOT NULL CHECK(include_content IN (0,1)),
            source_refs_json            TEXT NOT NULL,
            principal_ref_json          TEXT NOT NULL,
            authorization_fingerprint   TEXT NOT NULL,
            state                       TEXT NOT NULL CHECK(state IN (
                'prepared','written','completed','failed'
            )),
            payload_sha256              TEXT,
            item_count                  INTEGER,
            usage_ids_json              TEXT NOT NULL DEFAULT '[]',
            exported_at                 TEXT NOT NULL,
            written_at                  TEXT,
            completed_at                TEXT,
            error_code                  TEXT,
            created_at                  TEXT NOT NULL,
            updated_at                  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_exports_state "
        "ON source_export_operations(state, updated_at)"
    )
    conn.execute(
        "UPDATE source_store_info SET schema_version = ? WHERE singleton = 1",
        (2,),
    )


def _m003_export_operator_authorizations(conn: sqlite3.Connection) -> None:
    """Bind fresh maintenance approval to an immutable export operation."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_export_operator_authorizations (
            export_id                    TEXT NOT NULL
                REFERENCES source_export_operations(export_id),
            action                       TEXT NOT NULL CHECK(action IN (
                'recover_export','abort_export'
            )),
            authorization_fingerprint    TEXT NOT NULL,
            authorization_request_id     TEXT NOT NULL,
            approved_scope_sha256         TEXT NOT NULL,
            created_at                    TEXT NOT NULL,
            PRIMARY KEY(export_id, action, authorization_fingerprint)
        )
        """
    )
    conn.execute(
        "UPDATE source_store_info SET schema_version = ? WHERE singleton = 1",
        (SCHEMA_VERSION,),
    )


SOURCES_MIGRATIONS = MigrationRunner(
    "sources",
    [
        Migration(1, "retained source foundation", _m001_sources_schema),
        Migration(2, "recoverable issued-copy exports", _m002_recoverable_exports),
        Migration(
            3,
            "fresh operator authorization for export recovery",
            _m003_export_operator_authorizations,
        ),
    ],
)
