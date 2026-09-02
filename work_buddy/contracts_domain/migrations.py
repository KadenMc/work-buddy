"""Versioned schema for the Contracts SQLite authority."""

from __future__ import annotations

import sqlite3

from work_buddy.storage.migrations import Migration, MigrationRunner


def _m001_native_contract_authority(conn: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS contract_authority (
            singleton              INTEGER PRIMARY KEY CHECK(singleton = 1),
            state                  TEXT NOT NULL CHECK(state IN ('legacy', 'native')),
            authority_epoch        INTEGER NOT NULL CHECK(authority_epoch >= 0),
            sealed_cohort_id       TEXT,
            coordinator_decision_id TEXT,
            coordinator_decision_sha256 TEXT,
            sealed_at              TEXT,
            CHECK (
                (state = 'legacy' AND sealed_cohort_id IS NULL
                    AND coordinator_decision_id IS NULL
                    AND coordinator_decision_sha256 IS NULL
                    AND sealed_at IS NULL)
                OR
                (state = 'native' AND sealed_cohort_id IS NOT NULL
                    AND coordinator_decision_id IS NOT NULL
                    AND coordinator_decision_sha256 IS NOT NULL
                    AND sealed_at IS NOT NULL)
            )
        )
        """,
        """
        INSERT OR IGNORE INTO contract_authority (
            singleton, state, authority_epoch
        ) VALUES (1, 'legacy', 0)
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_wip_policies (
            policy_id       TEXT PRIMARY KEY,
            active_limit    INTEGER NOT NULL CHECK(active_limit > 0),
            revision        INTEGER NOT NULL CHECK(revision > 0),
            actor_ref       TEXT NOT NULL,
            intent_id       TEXT NOT NULL UNIQUE,
            updated_at      TEXT NOT NULL
        )
        """,
        """
        INSERT OR IGNORE INTO contract_wip_policies (
            policy_id, active_limit, revision, actor_ref, intent_id, updated_at
        ) VALUES ('default', 3, 1, 'system:contracts-schema',
                  'contracts-schema-v1:wip-default',
                  '1970-01-01T00:00:00.000+00:00')
        """,
        """
        CREATE TABLE IF NOT EXISTS contracts (
            contract_id        TEXT PRIMARY KEY CHECK(length(contract_id) = 32),
            title              TEXT NOT NULL CHECK(length(trim(title)) > 0),
            status             TEXT NOT NULL CHECK(status IN (
                'draft', 'active', 'paused', 'completed', 'abandoned'
            )),
            contract_type      TEXT NOT NULL CHECK(length(trim(contract_type)) > 0),
            lifecycle          TEXT NOT NULL CHECK(lifecycle IN (
                'current', 'archived', 'tombstoned'
            )),
            privacy_class      TEXT NOT NULL CHECK(privacy_class IN (
                'private', 'sensitive', 'shared'
            )),
            estimated_progress INTEGER NOT NULL DEFAULT 0
                CHECK(estimated_progress BETWEEN 0 AND 100),
            current_revision   INTEGER NOT NULL CHECK(current_revision > 0),
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL,
            tombstoned_at      TEXT,
            CHECK (
                (lifecycle = 'tombstoned' AND tombstoned_at IS NOT NULL)
                OR (lifecycle != 'tombstoned' AND tombstoned_at IS NULL)
            )
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_aliases (
            alias_key       TEXT PRIMARY KEY,
            alias_display   TEXT NOT NULL,
            alias_kind      TEXT NOT NULL CHECK(alias_kind IN (
                'logical_name', 'legacy_path', 'user_alias'
            )),
            contract_id     TEXT NOT NULL REFERENCES contracts(contract_id),
            created_at      TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_contract_alias_contract "
        "ON contract_aliases(contract_id, alias_kind, alias_key)",
        """
        CREATE TABLE IF NOT EXISTS contract_dates (
            contract_id     TEXT NOT NULL REFERENCES contracts(contract_id),
            date_kind       TEXT NOT NULL,
            date_value      TEXT NOT NULL,
            precision       TEXT NOT NULL DEFAULT 'day' CHECK(precision IN (
                'day', 'month', 'year', 'datetime'
            )),
            PRIMARY KEY(contract_id, date_kind)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_commitments (
            commitment_id   TEXT PRIMARY KEY CHECK(length(commitment_id) = 32),
            contract_id     TEXT NOT NULL REFERENCES contracts(contract_id),
            commitment_kind TEXT NOT NULL,
            text             TEXT NOT NULL CHECK(length(trim(text)) > 0),
            state            TEXT NOT NULL CHECK(state IN (
                'open', 'done', 'waived'
            )),
            due_date         TEXT,
            ordinal          INTEGER NOT NULL CHECK(ordinal >= 0),
            UNIQUE(contract_id, ordinal)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_constraints (
            constraint_id   TEXT PRIMARY KEY CHECK(length(constraint_id) = 32),
            contract_id     TEXT NOT NULL REFERENCES contracts(contract_id),
            constraint_kind TEXT NOT NULL,
            text             TEXT NOT NULL CHECK(length(trim(text)) > 0),
            state            TEXT NOT NULL CHECK(state IN (
                'current', 'resolved', 'superseded'
            )),
            ordinal          INTEGER NOT NULL CHECK(ordinal >= 0),
            UNIQUE(contract_id, ordinal)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_health_inputs (
            contract_id     TEXT NOT NULL REFERENCES contracts(contract_id),
            input_key       TEXT NOT NULL,
            value_json      TEXT NOT NULL,
            PRIMARY KEY(contract_id, input_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_participants (
            participant_id  TEXT PRIMARY KEY CHECK(length(participant_id) = 32),
            contract_id     TEXT NOT NULL REFERENCES contracts(contract_id),
            entity_ref      TEXT,
            display_name    TEXT,
            role            TEXT NOT NULL,
            ordinal         INTEGER NOT NULL CHECK(ordinal >= 0),
            CHECK(entity_ref IS NOT NULL OR display_name IS NOT NULL),
            UNIQUE(contract_id, ordinal)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_evidence_links (
            evidence_link_id TEXT PRIMARY KEY CHECK(length(evidence_link_id) = 32),
            contract_id      TEXT NOT NULL REFERENCES contracts(contract_id),
            evidence_ref     TEXT NOT NULL CHECK(length(trim(evidence_ref)) > 0),
            label            TEXT,
            requirement      TEXT NOT NULL CHECK(requirement IN (
                'must_have', 'optional'
            )),
            state            TEXT NOT NULL CHECK(state IN (
                'open', 'satisfied', 'waived'
            )),
            ordinal          INTEGER NOT NULL CHECK(ordinal >= 0),
            UNIQUE(contract_id, ordinal)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_document_bindings (
            binding_id                  TEXT PRIMARY KEY CHECK(length(binding_id) = 32),
            contract_id                 TEXT NOT NULL REFERENCES contracts(contract_id),
            body_role                   TEXT NOT NULL,
            store_id                    TEXT NOT NULL,
            document_id                 TEXT NOT NULL,
            interaction_contract_id     TEXT NOT NULL,
            interaction_contract_version INTEGER NOT NULL
                CHECK(interaction_contract_version > 0),
            lifecycle                   TEXT NOT NULL CHECK(lifecycle IN (
                'current', 'retired'
            )),
            authority_epoch             INTEGER NOT NULL CHECK(authority_epoch > 0),
            created_at                  TEXT NOT NULL,
            UNIQUE(binding_id, contract_id, body_role),
            UNIQUE(store_id, document_id)
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_contract_binding_current_role
        ON contract_document_bindings(contract_id, body_role)
        WHERE lifecycle = 'current'
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_body_roles (
            contract_id                  TEXT NOT NULL REFERENCES contracts(contract_id),
            body_role                    TEXT NOT NULL,
            body_mode                    TEXT NOT NULL CHECK(body_mode IN (
                'plain', 'document'
            )),
            plain_body                   TEXT,
            current_document_binding_id  TEXT,
            body_revision                INTEGER NOT NULL CHECK(body_revision > 0),
            interaction_contract_id      TEXT NOT NULL,
            interaction_contract_version INTEGER NOT NULL
                CHECK(interaction_contract_version > 0),
            privacy_class                TEXT NOT NULL CHECK(privacy_class IN (
                'private', 'sensitive', 'shared'
            )),
            PRIMARY KEY(contract_id, body_role),
            FOREIGN KEY(
                current_document_binding_id, contract_id, body_role
            ) REFERENCES contract_document_bindings(
                binding_id, contract_id, body_role
            ),
            CHECK (
                (body_mode = 'plain' AND plain_body IS NOT NULL
                    AND current_document_binding_id IS NULL)
                OR
                (body_mode = 'document' AND plain_body IS NULL
                    AND current_document_binding_id IS NOT NULL)
            )
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_revisions (
            revision_id       TEXT PRIMARY KEY CHECK(length(revision_id) = 32),
            contract_id       TEXT NOT NULL REFERENCES contracts(contract_id),
            revision          INTEGER NOT NULL CHECK(revision > 0),
            prior_revision    INTEGER,
            operation         TEXT NOT NULL CHECK(operation IN (
                'create', 'update', 'tombstone', 'legacy_import'
            )),
            snapshot_json     TEXT NOT NULL,
            snapshot_sha256   TEXT NOT NULL CHECK(length(snapshot_sha256) = 64),
            request_sha256    TEXT NOT NULL CHECK(length(request_sha256) = 64),
            actor_ref         TEXT NOT NULL,
            intent_id         TEXT NOT NULL,
            source_ref        TEXT,
            created_at        TEXT NOT NULL,
            UNIQUE(contract_id, revision),
            UNIQUE(contract_id, intent_id),
            CHECK (
                (revision = 1 AND prior_revision IS NULL)
                OR (revision > 1 AND prior_revision = revision - 1)
            )
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_contract_revisions_contract "
        "ON contract_revisions(contract_id, revision)",
        """
        CREATE TABLE IF NOT EXISTS contract_mutation_receipts (
            receipt_id       TEXT PRIMARY KEY CHECK(length(receipt_id) = 32),
            intent_id        TEXT NOT NULL UNIQUE,
            operation        TEXT NOT NULL,
            contract_id      TEXT NOT NULL,
            revision         INTEGER NOT NULL,
            request_sha256   TEXT NOT NULL CHECK(length(request_sha256) = 64),
            result_sha256    TEXT NOT NULL CHECK(length(result_sha256) = 64),
            created_at       TEXT NOT NULL,
            FOREIGN KEY(contract_id, revision)
                REFERENCES contract_revisions(contract_id, revision)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_search_outbox (
            event_id          TEXT PRIMARY KEY CHECK(length(event_id) = 32),
            contract_id       TEXT NOT NULL,
            revision          INTEGER NOT NULL,
            event_kind        TEXT NOT NULL CHECK(event_kind IN ('upsert', 'delete')),
            content_sha256    TEXT NOT NULL CHECK(length(content_sha256) = 64),
            privacy_class     TEXT NOT NULL CHECK(privacy_class IN (
                'private', 'sensitive', 'shared'
            )),
            payload_json      TEXT NOT NULL,
            committed_at      TEXT NOT NULL,
            delivered_at      TEXT,
            UNIQUE(contract_id, revision, event_kind),
            FOREIGN KEY(contract_id, revision)
                REFERENCES contract_revisions(contract_id, revision)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_contract_outbox_pending "
        "ON contract_search_outbox(delivered_at, committed_at, event_id)",
        """
        CREATE TABLE IF NOT EXISTS contract_import_cohorts (
            cohort_id          TEXT PRIMARY KEY CHECK(length(cohort_id) = 32),
            intent_id          TEXT NOT NULL UNIQUE,
            state              TEXT NOT NULL CHECK(state IN ('staged', 'sealed')),
            parser_version     INTEGER NOT NULL CHECK(parser_version > 0),
            inventory_sha256   TEXT NOT NULL CHECK(length(inventory_sha256) = 64),
            request_sha256     TEXT NOT NULL CHECK(length(request_sha256) = 64),
            source_label       TEXT NOT NULL,
            item_count         INTEGER NOT NULL CHECK(item_count >= 0),
            accepted_count     INTEGER NOT NULL CHECK(accepted_count >= 0),
            quarantined_count  INTEGER NOT NULL CHECK(quarantined_count >= 0),
            ignored_count      INTEGER NOT NULL CHECK(ignored_count >= 0),
            actor_ref          TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            sealed_at          TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_import_inventory (
            cohort_id          TEXT NOT NULL REFERENCES contract_import_cohorts(cohort_id),
            source_key         TEXT NOT NULL,
            legacy_alias       TEXT NOT NULL,
            source_sha256      TEXT NOT NULL CHECK(length(source_sha256) = 64),
            byte_length        INTEGER NOT NULL CHECK(byte_length >= 0),
            frozen_bytes       BLOB NOT NULL,
            disposition        TEXT NOT NULL CHECK(disposition IN (
                'accepted', 'quarantined', 'ignored'
            )),
            quarantine_code    TEXT,
            quarantine_detail  TEXT,
            entity_id          TEXT,
            PRIMARY KEY(cohort_id, source_key),
            CHECK (
                (disposition = 'accepted' AND entity_id IS NOT NULL
                    AND quarantine_code IS NULL)
                OR
                (disposition = 'quarantined' AND quarantine_code IS NOT NULL)
                OR
                (disposition = 'ignored' AND entity_id IS NULL)
            )
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_import_stage (
            cohort_id          TEXT NOT NULL,
            source_key         TEXT NOT NULL,
            contract_id        TEXT NOT NULL CHECK(length(contract_id) = 32),
            record_json        TEXT NOT NULL,
            record_sha256      TEXT NOT NULL CHECK(length(record_sha256) = 64),
            PRIMARY KEY(cohort_id, source_key),
            FOREIGN KEY(cohort_id, source_key)
                REFERENCES contract_import_inventory(cohort_id, source_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_import_receipts (
            receipt_id         TEXT PRIMARY KEY CHECK(length(receipt_id) = 32),
            intent_id          TEXT NOT NULL UNIQUE,
            cohort_id          TEXT NOT NULL,
            operation          TEXT NOT NULL CHECK(operation IN ('stage', 'seal')),
            request_sha256     TEXT NOT NULL CHECK(length(request_sha256) = 64),
            result_json        TEXT NOT NULL,
            result_sha256      TEXT NOT NULL CHECK(length(result_sha256) = 64),
            actor_ref          TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            FOREIGN KEY(cohort_id) REFERENCES contract_import_cohorts(cohort_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS contract_import_seals (
            seal_id                      TEXT PRIMARY KEY CHECK(length(seal_id) = 32),
            cohort_id                    TEXT NOT NULL UNIQUE,
            inventory_sha256             TEXT NOT NULL CHECK(length(inventory_sha256) = 64),
            coordinator_decision_id      TEXT NOT NULL,
            coordinator_decision_sha256  TEXT NOT NULL CHECK(
                length(coordinator_decision_sha256) = 64
            ),
            actor_ref                    TEXT NOT NULL,
            sealed_at                    TEXT NOT NULL,
            FOREIGN KEY(cohort_id) REFERENCES contract_import_cohorts(cohort_id)
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)

    for table in (
        "contract_revisions",
        "contract_mutation_receipts",
        "contract_import_inventory",
        "contract_import_stage",
        "contract_import_receipts",
        "contract_import_seals",
    ):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_append_only_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_append_only_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )


def _m002_exact_import_sources(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contract_import_source_dependencies (
            cohort_id                  TEXT NOT NULL,
            source_key                 TEXT NOT NULL,
            ingress_client_mutation_id TEXT NOT NULL,
            source_usage_consumer_id   TEXT NOT NULL,
            source_ref                 TEXT,
            representation_id          TEXT,
            submission_id              TEXT,
            source_usage_id            TEXT,
            source_usage_state         TEXT NOT NULL DEFAULT 'unreserved'
                CHECK(source_usage_state IN
                    ('unreserved','reserved','acknowledged','released')),
            retained_at                TEXT,
            acknowledged_at            TEXT,
            released_at                TEXT,
            PRIMARY KEY(cohort_id, source_key),
            UNIQUE(source_usage_consumer_id),
            FOREIGN KEY(cohort_id, source_key)
                REFERENCES contract_import_inventory(cohort_id, source_key),
            CHECK(
                (source_usage_state = 'unreserved'
                    AND source_ref IS NULL AND representation_id IS NULL
                    AND submission_id IS NULL AND source_usage_id IS NULL)
                OR
                (source_usage_state IN ('reserved','acknowledged','released')
                    AND source_ref IS NOT NULL AND representation_id IS NOT NULL
                    AND source_usage_id IS NOT NULL)
            )
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS contract_import_source_state_forward_only
        BEFORE UPDATE ON contract_import_source_dependencies
        WHEN NOT (
          (OLD.source_usage_state='unreserved'
            AND NEW.source_usage_state IN ('unreserved','reserved'))
          OR (OLD.source_usage_state='reserved'
            AND NEW.source_usage_state IN ('reserved','acknowledged','released'))
          OR (OLD.source_usage_state='acknowledged'
            AND NEW.source_usage_state IN ('acknowledged','released'))
          OR (OLD.source_usage_state='released'
            AND NEW.source_usage_state='released')
        )
        BEGIN
            SELECT RAISE(ABORT, 'contract_import_source_state_is_forward_only');
        END
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contract_import_source_state "
        "ON contract_import_source_dependencies("
        "cohort_id, source_usage_state, source_key)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS contract_import_source_identity_immutable
        BEFORE UPDATE ON contract_import_source_dependencies
        WHEN NEW.cohort_id != OLD.cohort_id
          OR NEW.source_key != OLD.source_key
          OR NEW.ingress_client_mutation_id != OLD.ingress_client_mutation_id
          OR NEW.source_usage_consumer_id != OLD.source_usage_consumer_id
          OR (
            OLD.source_ref IS NOT NULL AND (
              COALESCE(NEW.source_ref,'') != OLD.source_ref
              OR COALESCE(NEW.representation_id,'') != OLD.representation_id
              OR COALESCE(NEW.submission_id,'') != COALESCE(OLD.submission_id,'')
              OR COALESCE(NEW.source_usage_id,'') != OLD.source_usage_id
            )
          )
        BEGIN
            SELECT RAISE(ABORT, 'contract_import_source_identity_is_immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS contract_import_source_no_delete
        BEFORE DELETE ON contract_import_source_dependencies
        BEGIN
            SELECT RAISE(ABORT, 'contract_import_source_is_append_only');
        END
        """
    )


def _m003_cutover_maintenance(conn: sqlite3.Connection) -> None:
    """Keep Contracts fenced until post-seal search certification is durable."""

    statements = (
        """
        CREATE TABLE IF NOT EXISTS cutover_maintenance (
            singleton                    INTEGER PRIMARY KEY CHECK(singleton=1),
            domain                       TEXT NOT NULL CHECK(domain='contracts'),
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
        )
        """,
        """
        INSERT OR IGNORE INTO cutover_maintenance
          (singleton,domain,state,updated_at)
        VALUES (1,'contracts','open','1970-01-01T00:00:00.000+00:00')
        """,
        """
        CREATE TABLE IF NOT EXISTS cutover_maintenance_receipts (
            mutation_id          TEXT PRIMARY KEY,
            operation            TEXT NOT NULL,
            request_sha256       TEXT NOT NULL,
            result_json          TEXT NOT NULL,
            result_sha256        TEXT NOT NULL,
            created_at           TEXT NOT NULL
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)


CONTRACT_MIGRATIONS = MigrationRunner(
    "contracts",
    [
        Migration(1, "native contract authority and import ledger", _m001_native_contract_authority),
        Migration(2, "exact Source-backed contract imports", _m002_exact_import_sources),
        Migration(3, "durable cutover maintenance fence", _m003_cutover_maintenance),
    ],
)


__all__ = ["CONTRACT_MIGRATIONS"]
