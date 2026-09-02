"""Versioned schema for the personal-knowledge SQLite authority."""

from __future__ import annotations

import sqlite3

from work_buddy.storage.migrations import Migration, MigrationRunner


def _m001_personal_knowledge_authority(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS personal_knowledge_authority (
            singleton          INTEGER PRIMARY KEY CHECK(singleton = 1),
            authority          TEXT NOT NULL CHECK(authority IN
                                  ('legacy_markdown','sqlite')),
            authority_epoch    INTEGER NOT NULL CHECK(authority_epoch >= 1),
            sealed_cohort_id   TEXT,
            sealed_at          TEXT,
            updated_at         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS personal_units (
            unit_id                    TEXT PRIMARY KEY,
            current_path               TEXT NOT NULL UNIQUE
                                          CHECK(current_path LIKE 'personal/%'),
            name                       TEXT NOT NULL,
            description                TEXT NOT NULL DEFAULT '',
            summary                    TEXT NOT NULL DEFAULT '',
            body                       TEXT,
            body_mode                  TEXT NOT NULL DEFAULT 'plain'
                                          CHECK(body_mode IN ('plain','document')),
            document_binding_id        TEXT,
            document_store_id          TEXT,
            document_id                TEXT,
            interaction_contract_id    TEXT NOT NULL DEFAULT 'personal_note/v1',
            interaction_contract_version INTEGER NOT NULL DEFAULT 1
                                          CHECK(interaction_contract_version >= 1),
            severity                   TEXT NOT NULL DEFAULT '',
            privacy_class              TEXT NOT NULL DEFAULT 'private'
                                          CHECK(privacy_class IN
                                            ('private','restricted','public')),
            disclosure_class           TEXT NOT NULL DEFAULT 'local_only'
                                          CHECK(disclosure_class IN
                                            ('local_only','consent_required','shareable')),
            lifecycle                  TEXT NOT NULL DEFAULT 'active'
                                          CHECK(lifecycle IN
                                            ('active','archived','tombstoned')),
            current_revision           INTEGER NOT NULL CHECK(current_revision >= 1),
            observation_count          INTEGER NOT NULL DEFAULT 0
                                          CHECK(observation_count >= 0),
            last_observed              TEXT NOT NULL DEFAULT '',
            source_file                TEXT NOT NULL DEFAULT '',
            source_ref                 TEXT,
            created_at                 TEXT NOT NULL,
            updated_at                 TEXT NOT NULL,
            tombstoned_at              TEXT,
            CHECK(
              (body_mode = 'plain' AND body IS NOT NULL
                AND document_binding_id IS NULL AND document_store_id IS NULL
                AND document_id IS NULL)
              OR
              (body_mode = 'document' AND body IS NULL
                AND document_binding_id IS NOT NULL
                AND document_store_id IS NOT NULL AND document_id IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_personal_units_lifecycle
            ON personal_units(lifecycle, updated_at);
        CREATE INDEX IF NOT EXISTS idx_personal_units_privacy
            ON personal_units(privacy_class, disclosure_class);

        CREATE TABLE IF NOT EXISTS personal_unit_paths (
            logical_path       TEXT PRIMARY KEY CHECK(logical_path LIKE 'personal/%'),
            unit_id            TEXT NOT NULL REFERENCES personal_units(unit_id),
            is_current         INTEGER NOT NULL CHECK(is_current IN (0,1)),
            created_at         TEXT NOT NULL,
            retired_at         TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_personal_unit_current_path
            ON personal_unit_paths(unit_id) WHERE is_current = 1;

        CREATE TABLE IF NOT EXISTS personal_unit_categories (
            unit_id            TEXT NOT NULL REFERENCES personal_units(unit_id),
            category           TEXT NOT NULL,
            ordinal            INTEGER NOT NULL CHECK(ordinal >= 0),
            PRIMARY KEY(unit_id, category),
            UNIQUE(unit_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_personal_categories_category
            ON personal_unit_categories(category, unit_id);

        CREATE TABLE IF NOT EXISTS personal_unit_aliases (
            unit_id            TEXT NOT NULL REFERENCES personal_units(unit_id),
            alias              TEXT NOT NULL,
            alias_norm         TEXT NOT NULL,
            ordinal            INTEGER NOT NULL CHECK(ordinal >= 0),
            PRIMARY KEY(unit_id, alias_norm),
            UNIQUE(unit_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_personal_alias_norm
            ON personal_unit_aliases(alias_norm);

        CREATE TABLE IF NOT EXISTS personal_unit_tags (
            unit_id            TEXT NOT NULL REFERENCES personal_units(unit_id),
            tag                TEXT NOT NULL,
            tag_norm           TEXT NOT NULL,
            ordinal            INTEGER NOT NULL CHECK(ordinal >= 0),
            PRIMARY KEY(unit_id, tag_norm),
            UNIQUE(unit_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_personal_tag_norm
            ON personal_unit_tags(tag_norm);

        CREATE TABLE IF NOT EXISTS personal_unit_requirements (
            unit_id            TEXT NOT NULL REFERENCES personal_units(unit_id),
            requirement        TEXT NOT NULL,
            ordinal            INTEGER NOT NULL CHECK(ordinal >= 0),
            PRIMARY KEY(unit_id, requirement),
            UNIQUE(unit_id, ordinal)
        );

        CREATE TABLE IF NOT EXISTS personal_unit_edges (
            edge_id            TEXT PRIMARY KEY,
            source_unit_id     TEXT NOT NULL REFERENCES personal_units(unit_id),
            edge_kind          TEXT NOT NULL CHECK(edge_kind IN ('parent','reference')),
            target_unit_id     TEXT REFERENCES personal_units(unit_id),
            target_path        TEXT NOT NULL CHECK(target_path LIKE 'personal/%'),
            ordinal            INTEGER NOT NULL CHECK(ordinal >= 0),
            created_at         TEXT NOT NULL,
            UNIQUE(source_unit_id, edge_kind, target_path)
        );
        CREATE INDEX IF NOT EXISTS idx_personal_edges_target
            ON personal_unit_edges(target_unit_id, edge_kind);

        CREATE TABLE IF NOT EXISTS personal_observations (
            observation_id     TEXT PRIMARY KEY,
            unit_id            TEXT NOT NULL REFERENCES personal_units(unit_id),
            observed_at        TEXT NOT NULL,
            evidence           TEXT NOT NULL,
            source_ref         TEXT,
            actor              TEXT NOT NULL,
            unit_revision      INTEGER NOT NULL,
            created_at         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_personal_observations_unit
            ON personal_observations(unit_id, observed_at, observation_id);

        CREATE TABLE IF NOT EXISTS personal_unit_revisions (
            unit_id            TEXT NOT NULL REFERENCES personal_units(unit_id),
            revision           INTEGER NOT NULL CHECK(revision >= 1),
            mutation_kind      TEXT NOT NULL,
            actor              TEXT NOT NULL,
            source_ref         TEXT,
            intent_id          TEXT,
            snapshot_sha256    TEXT NOT NULL,
            snapshot_json      TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            PRIMARY KEY(unit_id, revision)
        );

        CREATE TABLE IF NOT EXISTS personal_mutation_receipts (
            idempotency_key    TEXT PRIMARY KEY,
            request_sha256     TEXT NOT NULL,
            operation          TEXT NOT NULL,
            unit_id            TEXT,
            revision           INTEGER,
            result_json        TEXT NOT NULL,
            created_at         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS personal_search_outbox (
            event_id           TEXT PRIMARY KEY,
            unit_id            TEXT NOT NULL REFERENCES personal_units(unit_id),
            revision           INTEGER NOT NULL,
            event_kind         TEXT NOT NULL CHECK(event_kind IN ('upsert','delete')),
            logical_path       TEXT NOT NULL,
            content_sha256     TEXT NOT NULL,
            privacy_class      TEXT NOT NULL,
            disclosure_class   TEXT NOT NULL,
            committed_at       TEXT NOT NULL,
            delivered_at       TEXT,
            UNIQUE(unit_id, revision)
        );
        CREATE INDEX IF NOT EXISTS idx_personal_outbox_pending
            ON personal_search_outbox(delivered_at, committed_at, event_id);

        CREATE TABLE IF NOT EXISTS personal_import_cohorts (
            cohort_id          TEXT PRIMARY KEY,
            parser_version     TEXT NOT NULL,
            request_sha256     TEXT NOT NULL,
            inventory_sha256   TEXT NOT NULL,
            source_root        TEXT NOT NULL,
            state              TEXT NOT NULL CHECK(state IN
                                  ('prepared','verified','sealed','aborted')),
            file_count         INTEGER NOT NULL,
            staged_count       INTEGER NOT NULL,
            quarantined_count  INTEGER NOT NULL,
            prepared_at        TEXT NOT NULL,
            verified_at        TEXT,
            sealed_at          TEXT,
            aborted_at         TEXT
        );

        CREATE TABLE IF NOT EXISTS personal_import_items (
            cohort_id          TEXT NOT NULL
                                  REFERENCES personal_import_cohorts(cohort_id),
            relative_path      TEXT NOT NULL,
            source_sha256      TEXT NOT NULL,
            byte_length        INTEGER NOT NULL,
            mtime_ns           INTEGER NOT NULL,
            logical_path       TEXT,
            unit_id            TEXT,
            payload_json       TEXT,
            disposition        TEXT NOT NULL CHECK(disposition IN
                                  ('staged','quarantined','sealed')),
            reason_code        TEXT,
            source_ref         TEXT,
            parity_status      TEXT,
            PRIMARY KEY(cohort_id, relative_path)
        );
        CREATE INDEX IF NOT EXISTS idx_personal_import_disposition
            ON personal_import_items(cohort_id, disposition, relative_path);

        CREATE TABLE IF NOT EXISTS personal_import_map (
            cohort_id          TEXT NOT NULL,
            relative_path      TEXT NOT NULL,
            source_sha256      TEXT NOT NULL,
            unit_id            TEXT NOT NULL REFERENCES personal_units(unit_id),
            revision           INTEGER NOT NULL,
            logical_path       TEXT NOT NULL,
            source_ref         TEXT,
            parity_status      TEXT NOT NULL CHECK(parity_status IN ('exact','normalized')),
            sealed_at          TEXT NOT NULL,
            PRIMARY KEY(cohort_id, relative_path)
        );

        CREATE TABLE IF NOT EXISTS personal_import_receipts (
            cohort_id          TEXT PRIMARY KEY
                                  REFERENCES personal_import_cohorts(cohort_id),
            request_sha256     TEXT NOT NULL,
            result_sha256      TEXT NOT NULL,
            result_json        TEXT NOT NULL,
            created_at         TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO personal_knowledge_authority "
        "(singleton,authority,authority_epoch,updated_at) "
        "VALUES (1,'legacy_markdown',1,strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
    )


def _m002_exact_import_sources(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS personal_import_source_dependencies (
            cohort_id                  TEXT NOT NULL,
            relative_path              TEXT NOT NULL,
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
            PRIMARY KEY(cohort_id, relative_path),
            UNIQUE(source_usage_consumer_id),
            FOREIGN KEY(cohort_id, relative_path)
                REFERENCES personal_import_items(cohort_id, relative_path),
            CHECK(
                (source_usage_state = 'unreserved'
                    AND source_ref IS NULL AND representation_id IS NULL
                    AND submission_id IS NULL AND source_usage_id IS NULL)
                OR
                (source_usage_state IN ('reserved','acknowledged','released')
                    AND source_ref IS NOT NULL AND representation_id IS NOT NULL
                    AND source_usage_id IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_personal_import_source_state
            ON personal_import_source_dependencies(
                cohort_id, source_usage_state, relative_path
            );
        CREATE TRIGGER IF NOT EXISTS personal_import_source_identity_immutable
        BEFORE UPDATE ON personal_import_source_dependencies
        WHEN NEW.cohort_id != OLD.cohort_id
          OR NEW.relative_path != OLD.relative_path
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
            SELECT RAISE(ABORT, 'personal_import_source_identity_is_immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS personal_import_source_no_delete
        BEFORE DELETE ON personal_import_source_dependencies
        BEGIN
            SELECT RAISE(ABORT, 'personal_import_source_is_append_only');
        END;
        CREATE TRIGGER IF NOT EXISTS personal_import_source_state_forward_only
        BEFORE UPDATE ON personal_import_source_dependencies
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
            SELECT RAISE(ABORT, 'personal_import_source_state_is_forward_only');
        END;
        """
    )


def _m003_cutover_maintenance(conn: sqlite3.Connection) -> None:
    """Keep native writes fenced until post-seal search certification."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cutover_maintenance (
            singleton                    INTEGER PRIMARY KEY CHECK(singleton=1),
            domain                       TEXT NOT NULL
                                           CHECK(domain='personal_knowledge'),
            state                        TEXT NOT NULL CHECK(state IN
                                           ('open','preseal_fenced',
                                            'postseal_pending','recovery')),
            cohort_id                    TEXT,
            inventory_sha256             TEXT,
            fence_id                     TEXT,
            pause_request_sha256          TEXT,
            paused_at                    TEXT,
            postseal_evidence_sha256      TEXT,
            released_at                  TEXT,
            updated_at                   TEXT NOT NULL,
            CHECK(
              state='open' OR
              (cohort_id IS NOT NULL AND inventory_sha256 IS NOT NULL
               AND fence_id IS NOT NULL AND pause_request_sha256 IS NOT NULL
               AND paused_at IS NOT NULL)
            )
        );
        INSERT OR IGNORE INTO cutover_maintenance
          (singleton,domain,state,updated_at)
        VALUES (1,'personal_knowledge','open','1970-01-01T00:00:00.000+00:00');

        CREATE TABLE IF NOT EXISTS cutover_maintenance_receipts (
            mutation_id          TEXT PRIMARY KEY,
            operation            TEXT NOT NULL,
            request_sha256       TEXT NOT NULL,
            result_json          TEXT NOT NULL,
            result_sha256        TEXT NOT NULL,
            created_at           TEXT NOT NULL
        );
        """
    )


PERSONAL_KNOWLEDGE_MIGRATIONS = MigrationRunner(
    "personal_knowledge",
    migrations=[
        Migration(1, "personal knowledge SQLite authority", _m001_personal_knowledge_authority),
        Migration(2, "exact Source-backed personal imports", _m002_exact_import_sources),
        Migration(3, "durable cutover maintenance fence", _m003_cutover_maintenance),
    ],
)

SCHEMA_VERSION = PERSONAL_KNOWLEDGE_MIGRATIONS.target_version
