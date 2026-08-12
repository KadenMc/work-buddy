"""Truth-owned schema extension for durable Hindsight projection delivery.

``install_truth_hindsight_projection_schema`` is deliberately callable from a
Truth migration.  It never commits and therefore can share the migration's
transaction.  Runtime workers only open the resulting tables; they do not
silently migrate an authoritative Truth database.
"""

from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 1


def install_truth_hindsight_projection_schema(conn: sqlite3.Connection) -> None:
    script = """
        CREATE TABLE IF NOT EXISTS truth_hindsight_projection_outbox (
            effect_id                   TEXT PRIMARY KEY,
            claim_id                    TEXT NOT NULL,
            claim_generation            TEXT NOT NULL,
            policy_id                   TEXT NOT NULL,
            desired_state               TEXT NOT NULL CHECK (
                desired_state IN ('upsert', 'remove')
            ),
            reason_code                 TEXT NOT NULL,
            eligibility_sha256          TEXT NOT NULL,
            authorization_ref           TEXT NOT NULL,
            purge_projection_source     INTEGER NOT NULL DEFAULT 0 CHECK (
                purge_projection_source IN (0, 1)
            ),
            request_sha256              TEXT NOT NULL,
            state                       TEXT NOT NULL CHECK (
                state IN (
                    'pending', 'delivering', 'reconciling',
                    'failed_retryable', 'failed_terminal',
                    'delivered', 'superseded'
                )
            ),
            attempt_count               INTEGER NOT NULL DEFAULT 0 CHECK (
                attempt_count >= 0
            ),
            lease_owner                 TEXT,
            lease_expires_at            TEXT,
            next_attempt_at             TEXT,
            last_error_code             TEXT,
            created_at                  TEXT NOT NULL,
            updated_at                  TEXT NOT NULL,
            UNIQUE (claim_id, claim_generation, policy_id)
        );

        CREATE INDEX IF NOT EXISTS idx_truth_hindsight_projection_ready
            ON truth_hindsight_projection_outbox(
                state, next_attempt_at, created_at, effect_id
            );

        CREATE TABLE IF NOT EXISTS truth_hindsight_projection_heads (
            claim_id                    TEXT NOT NULL,
            policy_id                   TEXT NOT NULL,
            claim_generation            TEXT NOT NULL,
            desired_state               TEXT NOT NULL CHECK (
                desired_state IN ('upsert', 'remove')
            ),
            effect_id                   TEXT NOT NULL UNIQUE REFERENCES
                truth_hindsight_projection_outbox(effect_id),
            request_sha256              TEXT NOT NULL,
            updated_at                  TEXT NOT NULL,
            PRIMARY KEY (claim_id, policy_id)
        );

        CREATE TABLE IF NOT EXISTS truth_hindsight_projection_attempts (
            effect_id                   TEXT NOT NULL REFERENCES
                truth_hindsight_projection_outbox(effect_id),
            attempt_no                  INTEGER NOT NULL CHECK (attempt_no > 0),
            worker_id                   TEXT NOT NULL,
            state                       TEXT NOT NULL,
            dependency_usages_json      TEXT NOT NULL DEFAULT '[]',
            destination_document_id     TEXT,
            captured_source_ref         TEXT,
            captured_representation_id  TEXT,
            content_sha256              TEXT,
            disclosure_run_id           TEXT,
            disclosure_entry_id         TEXT,
            disclosure_manifest_sha256  TEXT,
            error_code                   TEXT,
            started_at                  TEXT NOT NULL,
            completed_at                TEXT,
            PRIMARY KEY (effect_id, attempt_no)
        );

        CREATE TABLE IF NOT EXISTS truth_hindsight_projection_receipts (
            claim_id                    TEXT NOT NULL,
            policy_id                   TEXT NOT NULL,
            claim_generation            TEXT NOT NULL,
            receipt_state               TEXT NOT NULL CHECK (
                receipt_state IN ('present', 'absent')
            ),
            destination_document_id     TEXT NOT NULL,
            projection_method           TEXT NOT NULL,
            lifecycle_status            TEXT NOT NULL,
            applicability_scope_json    TEXT NOT NULL,
            valid_from                   TEXT,
            valid_to                     TEXT,
            captured_source_ref         TEXT,
            captured_representation_id  TEXT,
            content_sha256              TEXT,
            disclosure_run_id           TEXT,
            disclosure_entry_id         TEXT,
            disclosure_manifest_sha256  TEXT,
            dependency_usages_json      TEXT NOT NULL DEFAULT '[]',
            last_effect_id               TEXT NOT NULL REFERENCES
                truth_hindsight_projection_outbox(effect_id),
            observed_at                  TEXT NOT NULL,
            PRIMARY KEY (claim_id, policy_id)
        );

        CREATE TABLE IF NOT EXISTS truth_hindsight_projection_dependencies (
            claim_id                    TEXT NOT NULL,
            policy_id                   TEXT NOT NULL,
            claim_generation            TEXT NOT NULL,
            usage_id                    TEXT NOT NULL,
            source_ref                  TEXT NOT NULL,
            representation_id           TEXT NOT NULL,
            redaction_epoch             INTEGER NOT NULL CHECK (
                redaction_epoch >= 0
            ),
            active                      INTEGER NOT NULL CHECK (active IN (0, 1)),
            created_at                  TEXT NOT NULL,
            acknowledged_at             TEXT,
            released_at                 TEXT,
            PRIMARY KEY (claim_id, policy_id, claim_generation, usage_id)
        );

        CREATE INDEX IF NOT EXISTS idx_truth_hindsight_projection_usage
            ON truth_hindsight_projection_dependencies(usage_id, active);

        CREATE TABLE IF NOT EXISTS truth_hindsight_projection_source_cleanup (
            cleanup_id                  TEXT PRIMARY KEY,
            effect_id                   TEXT NOT NULL REFERENCES
                truth_hindsight_projection_outbox(effect_id),
            source_ref                  TEXT NOT NULL,
            authorization_ref           TEXT NOT NULL,
            reason_code                 TEXT NOT NULL,
            state                       TEXT NOT NULL CHECK (
                state IN ('pending', 'completed')
            ),
            created_at                  TEXT NOT NULL,
            completed_at                TEXT,
            UNIQUE (effect_id, source_ref)
        );

        CREATE TABLE IF NOT EXISTS truth_hindsight_projection_authorizations (
            authorization_ref           TEXT PRIMARY KEY,
            store_id                    TEXT NOT NULL,
            purpose                     TEXT NOT NULL CHECK (
                purpose = 'truth_hindsight_projection'
            ),
            policy_id                   TEXT NOT NULL,
            recipient                   TEXT NOT NULL,
            provider_id                 TEXT NOT NULL,
            model_id                    TEXT NOT NULL,
            eligible_claim_kinds_json   TEXT NOT NULL,
            projection_method           TEXT NOT NULL,
            granted_by_ref              TEXT NOT NULL,
            basis                       TEXT NOT NULL,
            canonical_sha256            TEXT NOT NULL UNIQUE,
            granted_at                  TEXT NOT NULL,
            expires_at                  TEXT NOT NULL,
            revoked_at                  TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_truth_hindsight_authorization_active
            ON truth_hindsight_projection_authorizations(
                store_id, policy_id, expires_at, revoked_at
            );

        CREATE TRIGGER IF NOT EXISTS trg_truth_hindsight_authorization_immutable
        BEFORE UPDATE ON truth_hindsight_projection_authorizations
        WHEN NEW.authorization_ref IS NOT OLD.authorization_ref
          OR NEW.store_id IS NOT OLD.store_id
          OR NEW.purpose IS NOT OLD.purpose
          OR NEW.policy_id IS NOT OLD.policy_id
          OR NEW.recipient IS NOT OLD.recipient
          OR NEW.provider_id IS NOT OLD.provider_id
          OR NEW.model_id IS NOT OLD.model_id
          OR NEW.eligible_claim_kinds_json IS NOT OLD.eligible_claim_kinds_json
          OR NEW.projection_method IS NOT OLD.projection_method
          OR NEW.granted_by_ref IS NOT OLD.granted_by_ref
          OR NEW.basis IS NOT OLD.basis
          OR NEW.canonical_sha256 IS NOT OLD.canonical_sha256
          OR NEW.granted_at IS NOT OLD.granted_at
          OR NEW.expires_at IS NOT OLD.expires_at
        BEGIN
            SELECT RAISE(ABORT, 'truth Hindsight authorization is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_truth_hindsight_outbox_identity_immutable
        BEFORE UPDATE ON truth_hindsight_projection_outbox
        WHEN NEW.effect_id IS NOT OLD.effect_id
          OR NEW.claim_id IS NOT OLD.claim_id
          OR NEW.claim_generation IS NOT OLD.claim_generation
          OR NEW.policy_id IS NOT OLD.policy_id
          OR NEW.desired_state IS NOT OLD.desired_state
          OR NEW.reason_code IS NOT OLD.reason_code
          OR NEW.eligibility_sha256 IS NOT OLD.eligibility_sha256
          OR NEW.authorization_ref IS NOT OLD.authorization_ref
          OR NEW.purge_projection_source IS NOT OLD.purge_projection_source
          OR NEW.request_sha256 IS NOT OLD.request_sha256
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'truth Hindsight projection intent is immutable');
        END;
        """
    # ``Connection.executescript`` may issue an implicit COMMIT before running
    # its script.  Parse complete SQLite statements and execute them one by one
    # so a Truth migration can keep this DDL in its own atomic transaction.
    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        statement = "\n".join(pending).strip()
        if statement and sqlite3.complete_statement(statement):
            conn.execute(statement)
            pending.clear()
    if any(line.strip() for line in pending):
        raise sqlite3.OperationalError("incomplete Truth Hindsight projection DDL")


def projection_schema_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'truth_hindsight_projection_outbox'"
    ).fetchone()
    return row is not None
