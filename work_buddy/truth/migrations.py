"""Versioned SQLite schema for targeted truth stores."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from work_buddy.logging_config import get_logger
from work_buddy.hindsight_projection.schema import (
    install_truth_hindsight_projection_schema,
)
from work_buddy.storage.migrations import (
    HASH_FORMAT_CURRENT,
    Migration,
    MigrationError,
    MigrationRunner,
    SchemaVersionTooNew,
)


logger = get_logger(__name__)

SCHEMA_VERSION = 11

# Redacted spans retain their immutable identity/hash but not their quote or
# quote context.  Keep the selector valid JSON (and valid for the existing
# selector parser) so recovery exports and integrity scans can still process
# the row without preserving any source text or source-text length.
REDACTED_SELECTOR_JSON = (
    '[{"exact":"[redacted]","prefix":"","suffix":"","type":"TextQuoteSelector"}]'
)
REDACTED_ACTION_CONTEXT_JSON = '{"kind":"redacted"}'


def _m001_initial_schema(conn: sqlite3.Connection) -> None:
    """Create the first truth ledger schema and its database guards."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS store_info (
            store_id       TEXT PRIMARY KEY,
            profile        TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            title          TEXT,
            created_at     TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ledger_records (
            seq          INTEGER PRIMARY KEY AUTOINCREMENT,
            record_type  TEXT NOT NULL,
            record_key   TEXT NOT NULL,
            UNIQUE (record_type, record_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS evidence (
            id                  TEXT PRIMARY KEY,
            kind                TEXT NOT NULL,
            source_locator      TEXT NOT NULL,
            content_sha256      TEXT NOT NULL,
            content             TEXT,
            content_path        TEXT,
            media_type          TEXT,
            acquired_at         TEXT NOT NULL,
            acquired_by_kind    TEXT NOT NULL,
            acquired_by_ref     TEXT,
            acquisition_method  TEXT NOT NULL,
            trust_class         TEXT NOT NULL,
            derived_from_store  TEXT,
            meta_json           TEXT,
            redacted_at         TEXT,
            created_at          TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS evidence_spans (
            id               TEXT PRIMARY KEY,
            evidence_id      TEXT NOT NULL REFERENCES evidence(id),
            selector_json    TEXT NOT NULL,
            quote_exact      TEXT,
            span_sha256      TEXT NOT NULL,
            author_kind      TEXT,
            author_ref       TEXT,
            redacted_at      TEXT,
            created_at       TEXT NOT NULL,
            created_by_kind  TEXT NOT NULL,
            created_by_ref   TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS claims (
            id                     TEXT PRIMARY KEY,
            proposition            TEXT NOT NULL,
            canonical_sha256       TEXT NOT NULL,
            claim_kind             TEXT NOT NULL,
            structured_json        TEXT,
            scope                  TEXT NOT NULL DEFAULT 'store',
            valid_from             TEXT,
            valid_to               TEXT,
            confidence_extraction  REAL,
            meta_json              TEXT,
            redacted_at            TEXT,
            created_at             TEXT NOT NULL,
            created_by_kind        TEXT NOT NULL,
            created_by_ref         TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS derivations (
            id             TEXT PRIMARY KEY,
            claim_id       TEXT NOT NULL REFERENCES claims(id),
            method         TEXT NOT NULL,
            producer_kind  TEXT NOT NULL,
            producer_ref   TEXT,
            confidence     REAL,
            rationale      TEXT,
            created_at     TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS derivation_premises (
            derivation_id  TEXT NOT NULL REFERENCES derivations(id),
            premise_kind   TEXT NOT NULL,
            premise_ref    TEXT NOT NULL,
            PRIMARY KEY (derivation_id, premise_ref)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS claim_links (
            id                       TEXT PRIMARY KEY,
            from_claim_id            TEXT NOT NULL REFERENCES claims(id),
            link_type                TEXT NOT NULL,
            to_kind                  TEXT NOT NULL,
            to_ref                   TEXT NOT NULL,
            role_json                TEXT,
            target_fingerprint       TEXT,
            fingerprint_reviewed_at  TEXT,
            created_at               TEXT NOT NULL,
            created_by_kind          TEXT NOT NULL,
            created_by_ref           TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS link_retractions (
            link_id     TEXT PRIMARY KEY REFERENCES claim_links(id),
            at          TEXT NOT NULL,
            actor_kind  TEXT NOT NULL,
            actor_ref   TEXT,
            reason      TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS claim_status_events (
            seq         INTEGER PRIMARY KEY AUTOINCREMENT,
            id          TEXT NOT NULL UNIQUE,
            claim_id    TEXT NOT NULL REFERENCES claims(id),
            status      TEXT NOT NULL,
            at          TEXT NOT NULL,
            actor_kind  TEXT NOT NULL,
            actor_ref   TEXT,
            basis_kind  TEXT NOT NULL,
            basis_ref   TEXT,
            note        TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS gestures (
            id              TEXT PRIMARY KEY,
            at              TEXT NOT NULL,
            surface         TEXT NOT NULL,
            actor_ref       TEXT NOT NULL,
            kind            TEXT NOT NULL,
            subject_ref     TEXT NOT NULL,
            payload_sha256  TEXT NOT NULL,
            payload_excerpt TEXT NOT NULL,
            context_sha256  TEXT,
            expires_at      TEXT,
            consumed_at     TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS redaction_events (
            id            TEXT PRIMARY KEY,
            subject_kind  TEXT NOT NULL,
            subject_ref   TEXT NOT NULL,
            at            TEXT NOT NULL,
            actor_ref     TEXT NOT NULL,
            basis_kind    TEXT NOT NULL,
            basis_ref     TEXT NOT NULL,
            reason        TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS projections (
            id              TEXT PRIMARY KEY,
            path            TEXT NOT NULL,
            rendered_at     TEXT NOT NULL,
            content_sha256  TEXT NOT NULL,
            manifest_json   TEXT NOT NULL,
            health          TEXT NOT NULL DEFAULT 'clean',
            health_reason   TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sweeps (
            id           TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            at           TEXT NOT NULL,
            params_json  TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sweep_findings (
            id                TEXT PRIMARY KEY,
            sweep_id          TEXT NOT NULL REFERENCES sweeps(id),
            subject_kind      TEXT NOT NULL,
            subject_ref       TEXT NOT NULL,
            finding           TEXT NOT NULL,
            resolved_at       TEXT,
            resolved_by_ref   TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS claims_current (
            claim_id             TEXT PRIMARY KEY REFERENCES claims(id),
            status               TEXT NOT NULL,
            status_seq           INTEGER NOT NULL,
            effective_valid_from TEXT,
            effective_valid_to   TEXT,
            health               TEXT NOT NULL DEFAULT 'clean',
            health_reason        TEXT,
            rebuilt_at           TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_claim_status_claim_at "
        "ON claim_status_events(claim_id, at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_claim_status_claim_seq "
        "ON claim_status_events(claim_id, seq DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_status_confirm_gesture "
        "ON claim_status_events(basis_ref) "
        "WHERE status = 'confirmed' AND basis_kind = 'gesture' "
        "AND basis_ref IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_claim_links_from ON claim_links(from_claim_id)",
        "CREATE INDEX IF NOT EXISTS idx_claim_links_target "
        "ON claim_links(to_kind, to_ref)",
        "CREATE INDEX IF NOT EXISTS idx_claims_scope_kind ON claims(scope, claim_kind)",
        "CREATE INDEX IF NOT EXISTS idx_claims_scope_valid_from "
        "ON claims(scope, valid_from DESC)",
        "CREATE INDEX IF NOT EXISTS idx_claims_canonical_sha256 "
        "ON claims(canonical_sha256)",
        "CREATE INDEX IF NOT EXISTS idx_evidence_content_sha256 "
        "ON evidence(content_sha256)",
        "CREATE INDEX IF NOT EXISTS idx_evidence_spans_evidence "
        "ON evidence_spans(evidence_id)",
        "CREATE INDEX IF NOT EXISTS idx_sweep_findings_sweep "
        "ON sweep_findings(sweep_id)",
        """
        CREATE TRIGGER IF NOT EXISTS store_info_single_row_insert
        BEFORE INSERT ON store_info
        WHEN EXISTS (SELECT 1 FROM store_info)
        BEGIN
            SELECT RAISE(ABORT, 'store-info-single-row');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS store_info_append_only_update
        BEFORE UPDATE ON store_info
        WHEN NOT (
            NEW.schema_version > OLD.schema_version
            AND NEW.store_id IS OLD.store_id
            AND NEW.profile IS OLD.profile
            AND NEW.title IS OLD.title
            AND NEW.created_at IS OLD.created_at
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS evidence_append_only_update
        BEFORE UPDATE ON evidence
        WHEN NOT (
            OLD.redacted_at IS NULL
            AND NEW.redacted_at IS NOT NULL
            AND NEW.content IS NULL
            AND NEW.content_path IS NULL
            AND NEW.id IS OLD.id
            AND NEW.kind IS OLD.kind
            AND NEW.source_locator IS OLD.source_locator
            AND NEW.content_sha256 IS OLD.content_sha256
            AND NEW.media_type IS OLD.media_type
            AND NEW.acquired_at IS OLD.acquired_at
            AND NEW.acquired_by_kind IS OLD.acquired_by_kind
            AND NEW.acquired_by_ref IS OLD.acquired_by_ref
            AND NEW.acquisition_method IS OLD.acquisition_method
            AND NEW.trust_class IS OLD.trust_class
            AND NEW.derived_from_store IS OLD.derived_from_store
            AND NEW.meta_json IS OLD.meta_json
            AND NEW.created_at IS OLD.created_at
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS evidence_spans_append_only_update
        BEFORE UPDATE ON evidence_spans
        WHEN NOT (
            OLD.redacted_at IS NULL
            AND NEW.redacted_at IS NOT NULL
            AND NEW.quote_exact IS NULL
            AND NEW.selector_json = '{REDACTED_SELECTOR_JSON}'
            AND NEW.id IS OLD.id
            AND NEW.evidence_id IS OLD.evidence_id
            AND NEW.span_sha256 IS OLD.span_sha256
            AND NEW.author_kind IS OLD.author_kind
            AND NEW.author_ref IS OLD.author_ref
            AND NEW.created_at IS OLD.created_at
            AND NEW.created_by_kind IS OLD.created_by_kind
            AND NEW.created_by_ref IS OLD.created_by_ref
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS claims_append_only_update
        BEFORE UPDATE ON claims
        WHEN NOT (
            OLD.redacted_at IS NULL
            AND NEW.redacted_at IS NOT NULL
            AND NEW.proposition = '[redacted]'
            AND NEW.structured_json IS NULL
            AND NEW.id IS OLD.id
            AND NEW.canonical_sha256 IS OLD.canonical_sha256
            AND NEW.claim_kind IS OLD.claim_kind
            AND NEW.scope IS OLD.scope
            AND NEW.valid_from IS OLD.valid_from
            AND NEW.valid_to IS OLD.valid_to
            AND NEW.confidence_extraction IS OLD.confidence_extraction
            AND NEW.meta_json IS OLD.meta_json
            AND NEW.created_at IS OLD.created_at
            AND NEW.created_by_kind IS OLD.created_by_kind
            AND NEW.created_by_ref IS OLD.created_by_ref
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS gestures_append_only_update
        BEFORE UPDATE ON gestures
        WHEN NOT (
            NEW.id IS OLD.id
            AND NEW.at IS OLD.at
            AND NEW.surface IS OLD.surface
            AND NEW.actor_ref IS OLD.actor_ref
            AND NEW.kind IS OLD.kind
            AND NEW.subject_ref IS OLD.subject_ref
            AND NEW.payload_sha256 IS OLD.payload_sha256
            AND NEW.context_sha256 IS OLD.context_sha256
            AND NEW.expires_at IS OLD.expires_at
            AND (
                (
                    OLD.consumed_at IS NULL
                    AND NEW.consumed_at IS NOT NULL
                    AND NEW.payload_excerpt IS OLD.payload_excerpt
                )
                OR (
                    NEW.consumed_at IS OLD.consumed_at
                    AND OLD.payload_excerpt <> '[redacted]'
                    AND NEW.payload_excerpt = '[redacted]'
                    AND (
                        EXISTS (
                            SELECT 1 FROM claims
                            WHERE id = OLD.subject_ref
                            AND redacted_at IS NOT NULL
                        )
                        OR EXISTS (
                            SELECT 1 FROM evidence
                            WHERE id = OLD.subject_ref
                            AND redacted_at IS NOT NULL
                        )
                        OR EXISTS (
                            SELECT 1 FROM evidence_spans
                            WHERE id = OLD.subject_ref
                            AND redacted_at IS NOT NULL
                        )
                    )
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sweep_findings_append_only_update
        BEFORE UPDATE ON sweep_findings
        WHEN NOT (
            OLD.resolved_at IS NULL
            AND NEW.resolved_at IS NOT NULL
            AND NEW.id IS OLD.id
            AND NEW.sweep_id IS OLD.sweep_id
            AND NEW.subject_kind IS OLD.subject_kind
            AND NEW.subject_ref IS OLD.subject_ref
            AND NEW.finding IS OLD.finding
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
    )
    for statement in statements:
        conn.execute(statement)

    immutable_update_tables = (
        "ledger_records",
        "derivations",
        "derivation_premises",
        "claim_links",
        "link_retractions",
        "claim_status_events",
        "redaction_events",
        "sweeps",
    )
    for table in immutable_update_tables:
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_append_only_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )

    protected_delete_tables = (
        "store_info",
        "ledger_records",
        "evidence",
        "evidence_spans",
        "claims",
        "derivations",
        "derivation_premises",
        "claim_links",
        "link_retractions",
        "claim_status_events",
        "gestures",
        "redaction_events",
        "sweeps",
        "sweep_findings",
    )
    for table in protected_delete_tables:
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_append_only_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )


def _m002_document_surface(conn: sqlite3.Connection) -> None:
    """Create the co-work document surface schema (PRD sections 5 and 6).

    Additive over v1: six new base tables, their per-table append-only guards,
    the documents latest-pointer carve-out, the proposals redaction carve-out,
    and one recreation of the gestures update trigger so a proposal subject can
    carry a redacted excerpt exactly like a claim or evidence subject already
    can.
    """
    statements = (
        """
        CREATE TABLE IF NOT EXISTS documents (
            id                    TEXT PRIMARY KEY,
            path                  TEXT NOT NULL,
            title                 TEXT,
            document_class        TEXT NOT NULL,
            content_sha256        TEXT NOT NULL,
            ydoc_snapshot_sha256  TEXT,
            created_at            TEXT NOT NULL,
            created_by_kind       TEXT NOT NULL,
            created_by_ref        TEXT,
            meta_json             TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS document_spans (
            id               TEXT PRIMARY KEY,
            document_id      TEXT NOT NULL REFERENCES documents(id),
            selector_json    TEXT NOT NULL,
            quote_exact      TEXT,
            span_sha256      TEXT NOT NULL,
            author_kind      TEXT,
            author_ref       TEXT,
            created_at       TEXT NOT NULL,
            created_by_kind  TEXT NOT NULL,
            created_by_ref   TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS expressions (
            id                    TEXT PRIMARY KEY,
            document_span_id      TEXT NOT NULL REFERENCES document_spans(id),
            claim_ref_kind        TEXT NOT NULL,
            claim_ref             TEXT NOT NULL,
            role                  TEXT NOT NULL,
            claim_canonical_sha256 TEXT NOT NULL,
            span_sha256           TEXT NOT NULL,
            created_at            TEXT NOT NULL,
            created_by_kind       TEXT NOT NULL,
            created_by_ref        TEXT,
            meta_json             TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS proposals (
            id                  TEXT PRIMARY KEY,
            document_id         TEXT NOT NULL REFERENCES documents(id),
            base_content_sha256 TEXT NOT NULL,
            selector_json       TEXT NOT NULL,
            -- Nullable so the frozen redaction carve-out (which requires
            -- NEW.quote_exact IS NULL) is reachable, mirroring the shipped
            -- evidence_spans.quote_exact precedent. Live-proposal quote
            -- presence is enforced at the engine and export layers, not by a
            -- NOT NULL column. The frozen DDL annotated this NOT NULL, which
            -- contradicts its own redaction trigger and prose ("content fields
            -- null out"). Deviation flagged to the orchestrator.
            quote_exact         TEXT,
            span_sha256         TEXT NOT NULL,
            replacement         TEXT,
            rationale           TEXT,
            tldr                TEXT,
            claim_refs_json     TEXT,
            canonical_sha256    TEXT NOT NULL,
            dedup_key           TEXT NOT NULL,
            expires_at          TEXT,
            created_at          TEXT NOT NULL,
            created_by_kind     TEXT NOT NULL,
            created_by_ref      TEXT,
            meta_json           TEXT,
            redacted_at         TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS proposal_status_events (
            seq          INTEGER PRIMARY KEY AUTOINCREMENT,
            id           TEXT NOT NULL UNIQUE,
            proposal_id  TEXT NOT NULL REFERENCES proposals(id),
            status       TEXT NOT NULL,
            decision     TEXT,
            at           TEXT NOT NULL,
            actor_kind   TEXT NOT NULL,
            actor_ref    TEXT,
            basis_kind   TEXT NOT NULL,
            basis_ref    TEXT,
            note         TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS doc_events (
            id                    TEXT PRIMARY KEY,
            document_id           TEXT NOT NULL REFERENCES documents(id),
            kind                  TEXT NOT NULL,
            at                    TEXT NOT NULL,
            actor_kind            TEXT NOT NULL,
            actor_ref             TEXT,
            content_sha256        TEXT,
            ydoc_snapshot_sha256  TEXT,
            detail                TEXT
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_path ON documents(path)",
        "CREATE INDEX IF NOT EXISTS idx_documents_ydoc_snapshot "
        "ON documents(ydoc_snapshot_sha256)",
        "CREATE INDEX IF NOT EXISTS idx_document_spans_document "
        "ON document_spans(document_id)",
        "CREATE INDEX IF NOT EXISTS idx_expressions_document_span "
        "ON expressions(document_span_id)",
        "CREATE INDEX IF NOT EXISTS idx_expressions_claim_ref "
        "ON expressions(claim_ref)",
        "CREATE INDEX IF NOT EXISTS idx_proposals_document ON proposals(document_id)",
        "CREATE INDEX IF NOT EXISTS idx_proposals_dedup "
        "ON proposals(document_id, dedup_key)",
        "CREATE INDEX IF NOT EXISTS idx_proposals_canonical "
        "ON proposals(canonical_sha256)",
        "CREATE INDEX IF NOT EXISTS idx_proposal_status_proposal_seq "
        "ON proposal_status_events(proposal_id, seq DESC)",
        "CREATE INDEX IF NOT EXISTS idx_doc_events_document "
        "ON doc_events(document_id)",
        """
        CREATE TRIGGER IF NOT EXISTS documents_append_only_update
        BEFORE UPDATE ON documents
        WHEN NOT (
            NEW.id IS OLD.id
            AND NEW.path IS OLD.path
            AND NEW.title IS OLD.title
            AND NEW.document_class IS OLD.document_class
            AND NEW.created_at IS OLD.created_at
            AND NEW.created_by_kind IS OLD.created_by_kind
            AND NEW.created_by_ref IS OLD.created_by_ref
            AND NEW.meta_json IS OLD.meta_json
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS proposals_append_only_update
        BEFORE UPDATE ON proposals
        WHEN NOT (
            OLD.redacted_at IS NULL
            AND NEW.redacted_at IS NOT NULL
            AND NEW.quote_exact IS NULL
            AND NEW.replacement IS NULL
            AND NEW.rationale IS NULL
            AND NEW.tldr IS NULL
            AND NEW.claim_refs_json IS NULL
            AND NEW.selector_json = '{REDACTED_SELECTOR_JSON}'
            AND NEW.id IS OLD.id
            AND NEW.document_id IS OLD.document_id
            AND NEW.base_content_sha256 IS OLD.base_content_sha256
            AND NEW.span_sha256 IS OLD.span_sha256
            AND NEW.canonical_sha256 IS OLD.canonical_sha256
            AND NEW.dedup_key IS OLD.dedup_key
            AND NEW.expires_at IS OLD.expires_at
            AND NEW.created_at IS OLD.created_at
            AND NEW.created_by_kind IS OLD.created_by_kind
            AND NEW.created_by_ref IS OLD.created_by_ref
            AND NEW.meta_json IS OLD.meta_json
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
    )
    for statement in statements:
        conn.execute(statement)

    append_only_update_tables = (
        "document_spans",
        "expressions",
        "proposal_status_events",
        "doc_events",
    )
    for table in append_only_update_tables:
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_append_only_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )

    protected_delete_tables = (
        "documents",
        "document_spans",
        "expressions",
        "proposals",
        "proposal_status_events",
        "doc_events",
    )
    for table in protected_delete_tables:
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_append_only_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )

    # Proposals are now gesture subjects and are redactable, so the shipped
    # gestures update trigger must admit a redacted proposal-subject excerpt.
    # Drop with IF EXISTS so a missing prior trigger is not a migration error
    # and CREATE cannot silently keep the old definition, then recreate the
    # trigger verbatim with a fourth EXISTS branch over proposals. The only
    # delta from the v1 trigger is that final proposals branch.
    conn.execute("DROP TRIGGER IF EXISTS gestures_append_only_update")
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS gestures_append_only_update
        BEFORE UPDATE ON gestures
        WHEN NOT (
            NEW.id IS OLD.id
            AND NEW.at IS OLD.at
            AND NEW.surface IS OLD.surface
            AND NEW.actor_ref IS OLD.actor_ref
            AND NEW.kind IS OLD.kind
            AND NEW.subject_ref IS OLD.subject_ref
            AND NEW.payload_sha256 IS OLD.payload_sha256
            AND NEW.context_sha256 IS OLD.context_sha256
            AND NEW.expires_at IS OLD.expires_at
            AND (
                (
                    OLD.consumed_at IS NULL
                    AND NEW.consumed_at IS NOT NULL
                    AND NEW.payload_excerpt IS OLD.payload_excerpt
                )
                OR (
                    NEW.consumed_at IS OLD.consumed_at
                    AND OLD.payload_excerpt <> '[redacted]'
                    AND NEW.payload_excerpt = '[redacted]'
                    AND (
                        EXISTS (
                            SELECT 1 FROM claims
                            WHERE id = OLD.subject_ref
                            AND redacted_at IS NOT NULL
                        )
                        OR EXISTS (
                            SELECT 1 FROM evidence
                            WHERE id = OLD.subject_ref
                            AND redacted_at IS NOT NULL
                        )
                        OR EXISTS (
                            SELECT 1 FROM evidence_spans
                            WHERE id = OLD.subject_ref
                            AND redacted_at IS NOT NULL
                        )
                        OR EXISTS (
                            SELECT 1 FROM proposals
                            WHERE id = OLD.subject_ref
                            AND redacted_at IS NOT NULL
                        )
                    )
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """
    )


def _document_path_key(path: str) -> str:
    """Return the host-local key used to reject path aliases.

    Document paths are portable POSIX-style relative paths.  The key is local
    machine state because case sensitivity is a property of the host on which
    the folder is opened, not a portable ledger fact.
    """

    return path.casefold() if os.name == "nt" else path


def _m003_cowork_document_foundation(conn: sqlite3.Connection) -> None:
    """Add recoverable Co-work initialization and document-version history."""

    conn.execute(
        "ALTER TABLE proposals ADD COLUMN base_structured_head_sha256 TEXT"
    )

    # Recreate the append-only guard so the new nullable base is pinned too.
    # Without this replacement SQLite would allow an UPDATE that changed only
    # the newly-added column because the v2 trigger cannot mention it.
    conn.execute("DROP TRIGGER IF EXISTS proposals_append_only_update")
    conn.execute(
        f"""
        CREATE TRIGGER proposals_append_only_update
        BEFORE UPDATE ON proposals
        WHEN NOT (
            OLD.redacted_at IS NULL
            AND NEW.redacted_at IS NOT NULL
            AND NEW.quote_exact IS NULL
            AND NEW.replacement IS NULL
            AND NEW.rationale IS NULL
            AND NEW.tldr IS NULL
            AND NEW.claim_refs_json IS NULL
            AND NEW.selector_json = '{REDACTED_SELECTOR_JSON}'
            AND NEW.id IS OLD.id
            AND NEW.document_id IS OLD.document_id
            AND NEW.base_content_sha256 IS OLD.base_content_sha256
            AND NEW.base_structured_head_sha256 IS OLD.base_structured_head_sha256
            AND NEW.span_sha256 IS OLD.span_sha256
            AND NEW.canonical_sha256 IS OLD.canonical_sha256
            AND NEW.dedup_key IS OLD.dedup_key
            AND NEW.expires_at IS OLD.expires_at
            AND NEW.created_at IS OLD.created_at
            AND NEW.created_by_kind IS OLD.created_by_kind
            AND NEW.created_by_ref IS OLD.created_by_ref
            AND NEW.meta_json IS OLD.meta_json
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """
    )

    statements = (
        """
        CREATE TABLE document_path_keys (
            document_id TEXT PRIMARY KEY REFERENCES documents(id),
            path_key    TEXT NOT NULL UNIQUE
        )
        """,
        """
        CREATE TABLE document_versions (
            id                     TEXT PRIMARY KEY,
            document_id            TEXT NOT NULL REFERENCES documents(id),
            kind                   TEXT NOT NULL,
            projection_sha256      TEXT NOT NULL,
            ydoc_snapshot_sha256   TEXT NOT NULL,
            structured_head_sha256 TEXT NOT NULL,
            created_at             TEXT NOT NULL,
            actor_kind             TEXT NOT NULL,
            actor_ref              TEXT,
            detail                 TEXT
        )
        """,
        "CREATE INDEX idx_document_versions_document "
        "ON document_versions(document_id, created_at, id)",
        "CREATE INDEX idx_document_versions_projection "
        "ON document_versions(projection_sha256)",
        "CREATE INDEX idx_document_versions_snapshot "
        "ON document_versions(ydoc_snapshot_sha256)",
        """
        CREATE TRIGGER document_versions_append_only_update
        BEFORE UPDATE ON document_versions
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
        """
        CREATE TRIGGER document_versions_append_only_delete
        BEFORE DELETE ON document_versions
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
        """
        CREATE TABLE cowork_bootstrap_intents (
            id                     TEXT PRIMARY KEY,
            idempotency_key        TEXT NOT NULL,
            actor_ref              TEXT NOT NULL,
            request_sha256         TEXT NOT NULL,
            mode                   TEXT NOT NULL,
            state                  TEXT NOT NULL,
            document_id            TEXT NOT NULL,
            normalized_path        TEXT NOT NULL,
            path_key               TEXT NOT NULL,
            title                  TEXT,
            document_class         TEXT NOT NULL,
            source_sha256          TEXT NOT NULL,
            source_byte_length     INTEGER NOT NULL,
            expected_file_sha256   TEXT,
            snapshot_sha256        TEXT,
            structured_head_sha256 TEXT,
            staged_path            TEXT,
            created_at             TEXT NOT NULL,
            updated_at             TEXT NOT NULL,
            expires_at             TEXT NOT NULL,
            committed_at           TEXT,
            receipt_json           TEXT,
            recovery_detail        TEXT,
            UNIQUE(actor_ref, idempotency_key)
        )
        """,
        "CREATE INDEX idx_cowork_bootstrap_state_expiry "
        "ON cowork_bootstrap_intents(state, expires_at)",
        "CREATE UNIQUE INDEX uq_cowork_bootstrap_live_path "
        "ON cowork_bootstrap_intents(path_key) "
        "WHERE state IN ('prepared', 'publishing')",
        """
        CREATE TABLE cowork_materialization_intents (
            id                              TEXT PRIMARY KEY,
            idempotency_key                 TEXT,
            actor_ref                       TEXT NOT NULL,
            document_id                     TEXT NOT NULL REFERENCES documents(id),
            state                           TEXT NOT NULL,
            expected_file_sha256            TEXT NOT NULL,
            expected_structured_head_sha256 TEXT NOT NULL,
            snapshot_sha256                 TEXT NOT NULL,
            rendered_sha256                 TEXT NOT NULL,
            staged_path                     TEXT,
            quarantine_path                 TEXT,
            document_version_id             TEXT NOT NULL,
            created_at                      TEXT NOT NULL,
            updated_at                      TEXT NOT NULL,
            committed_at                    TEXT,
            receipt_json                    TEXT,
            recovery_detail                 TEXT,
            UNIQUE(actor_ref, idempotency_key)
        )
        """,
        "CREATE INDEX idx_cowork_materialization_state "
        "ON cowork_materialization_intents(state, document_id)",
    )
    for statement in statements:
        conn.execute(statement)

    for row in conn.execute("SELECT id, path FROM documents ORDER BY id"):
        try:
            conn.execute(
                "INSERT INTO document_path_keys (document_id, path_key) VALUES (?, ?)",
                (row[0], _document_path_key(str(row[1]))),
            )
        except sqlite3.IntegrityError as exc:
            raise sqlite3.IntegrityError(
                "existing document paths collide under the host filesystem rules"
            ) from exc


def _m004_cowork_lifecycle_intents(conn: sqlite3.Connection) -> None:
    """Add recoverable human lifecycle intents outside the portable ledger."""

    statements = (
        """
        CREATE TABLE cowork_sitting_intents (
            id                              TEXT PRIMARY KEY,
            idempotency_key                 TEXT NOT NULL,
            actor_ref                       TEXT NOT NULL,
            document_id                     TEXT NOT NULL REFERENCES documents(id),
            request_sha256                  TEXT NOT NULL,
            state                           TEXT NOT NULL,
            expected_file_sha256            TEXT NOT NULL,
            expected_structured_head_sha256 TEXT NOT NULL,
            expected_snapshot_sha256        TEXT NOT NULL,
            admitted_items_json             TEXT NOT NULL,
            failed_items_json               TEXT NOT NULL,
            has_apply                       INTEGER NOT NULL,
            new_snapshot_sha256             TEXT,
            new_structured_head_sha256      TEXT,
            rendered_sha256                 TEXT,
            materialization_intent_id       TEXT,
            created_at                      TEXT NOT NULL,
            updated_at                      TEXT NOT NULL,
            expires_at                      TEXT NOT NULL,
            committed_at                    TEXT,
            receipt_json                    TEXT,
            recovery_detail                 TEXT,
            UNIQUE(actor_ref, idempotency_key)
        )
        """,
        "CREATE INDEX idx_cowork_sitting_state_expiry "
        "ON cowork_sitting_intents(state, expires_at)",
        "CREATE INDEX idx_cowork_sitting_document "
        "ON cowork_sitting_intents(document_id, state)",
        """
        CREATE TABLE cowork_reimport_intents (
            id                              TEXT PRIMARY KEY,
            idempotency_key                 TEXT NOT NULL,
            actor_ref                       TEXT NOT NULL,
            document_id                     TEXT NOT NULL REFERENCES documents(id),
            state                           TEXT NOT NULL,
            expected_file_sha256            TEXT NOT NULL,
            prior_projection_sha256         TEXT NOT NULL,
            prior_snapshot_sha256           TEXT NOT NULL,
            prior_structured_head_sha256    TEXT NOT NULL,
            source_byte_length              INTEGER NOT NULL,
            staged_path                     TEXT NOT NULL,
            replacement_snapshot_sha256     TEXT,
            replacement_structured_head_sha256 TEXT,
            document_version_id             TEXT NOT NULL,
            created_at                      TEXT NOT NULL,
            updated_at                      TEXT NOT NULL,
            expires_at                      TEXT NOT NULL,
            committed_at                    TEXT,
            receipt_json                    TEXT,
            recovery_detail                 TEXT,
            UNIQUE(actor_ref, idempotency_key)
        )
        """,
        "CREATE INDEX idx_cowork_reimport_state_expiry "
        "ON cowork_reimport_intents(state, expires_at)",
        "CREATE INDEX idx_cowork_reimport_document "
        "ON cowork_reimport_intents(document_id, state)",
        """
        CREATE TABLE cowork_retirement_intents (
            id                              TEXT PRIMARY KEY,
            idempotency_key                 TEXT NOT NULL,
            actor_ref                       TEXT NOT NULL,
            document_id                     TEXT NOT NULL REFERENCES documents(id),
            state                           TEXT NOT NULL,
            expected_file_sha256            TEXT NOT NULL,
            expected_projection_sha256      TEXT NOT NULL,
            expected_snapshot_sha256        TEXT NOT NULL,
            expected_structured_head_sha256 TEXT NOT NULL,
            consequence_sha256              TEXT NOT NULL,
            created_at                      TEXT NOT NULL,
            updated_at                      TEXT NOT NULL,
            expires_at                      TEXT NOT NULL,
            committed_at                    TEXT,
            receipt_json                    TEXT,
            recovery_detail                 TEXT,
            UNIQUE(actor_ref, idempotency_key)
        )
        """,
        "CREATE INDEX idx_cowork_retirement_state_expiry "
        "ON cowork_retirement_intents(state, expires_at)",
        "CREATE INDEX idx_cowork_retirement_document "
        "ON cowork_retirement_intents(document_id, state)",
    )
    for statement in statements:
        conn.execute(statement)


def _m005_cowork_verify_cothink(conn: sqlite3.Connection) -> None:
    """Add the portable, append-only Co-work Verify and Co-think ledger.

    The records in this migration describe durable product facts: immutable
    definitions, exact action inputs, resolved plans, completed work, routing
    decisions, and advisory Co-think contributions. Queue leases and other
    restart machinery remain runtime state and deliberately do not live here.
    """

    statements = (
        """
        CREATE TABLE criterion_definition_versions (
            id                        TEXT PRIMARY KEY,
            stable_key                TEXT NOT NULL,
            version                   INTEGER NOT NULL CHECK(version > 0),
            title                     TEXT NOT NULL,
            description               TEXT NOT NULL,
            criterion_kind            TEXT NOT NULL,
            origin                    TEXT NOT NULL,
            configuration_schema_json TEXT NOT NULL,
            canonical_sha256          TEXT NOT NULL UNIQUE,
            created_at                TEXT NOT NULL,
            created_by_kind           TEXT NOT NULL,
            created_by_ref            TEXT,
            created_by_meta_json       TEXT,
            UNIQUE(stable_key, version)
        )
        """,
        """
        CREATE TABLE check_definition_versions (
            id                             TEXT PRIMARY KEY,
            stable_key                     TEXT NOT NULL,
            version                        INTEGER NOT NULL CHECK(version > 0),
            title                          TEXT NOT NULL,
            mechanism                      TEXT NOT NULL,
            executor_ref                   TEXT NOT NULL,
            supported_criterion_kinds_json TEXT NOT NULL,
            input_schema_json              TEXT NOT NULL,
            output_schema_json             TEXT NOT NULL,
            limitations_json               TEXT NOT NULL,
            origin                         TEXT NOT NULL,
            canonical_sha256               TEXT NOT NULL UNIQUE,
            created_at                     TEXT NOT NULL,
            created_by_kind                TEXT NOT NULL,
            created_by_ref                 TEXT,
            created_by_meta_json            TEXT,
            UNIQUE(stable_key, version)
        )
        """,
        """
        CREATE TABLE criterion_check_bindings (
            id                              TEXT PRIMARY KEY,
            criterion_definition_version_id TEXT NOT NULL
                REFERENCES criterion_definition_versions(id),
            check_definition_version_id     TEXT NOT NULL
                REFERENCES check_definition_versions(id),
            configuration_json              TEXT NOT NULL,
            canonical_sha256                TEXT NOT NULL UNIQUE,
            created_at                      TEXT NOT NULL,
            created_by_kind                 TEXT NOT NULL,
            created_by_ref                  TEXT,
            created_by_meta_json             TEXT
        )
        """,
        """
        CREATE TABLE criterion_activations (
            id                              TEXT PRIMARY KEY,
            criterion_definition_version_id TEXT NOT NULL
                REFERENCES criterion_definition_versions(id),
            criterion_check_binding_id      TEXT NOT NULL
                REFERENCES criterion_check_bindings(id),
            scope_json                      TEXT NOT NULL,
            is_enabled                      INTEGER NOT NULL
                CHECK(is_enabled IN (0, 1)),
            is_required                     INTEGER NOT NULL
                CHECK(is_required IN (0, 1)),
            origin                          TEXT NOT NULL,
            canonical_sha256                TEXT NOT NULL,
            created_at                      TEXT NOT NULL,
            created_by_kind                 TEXT NOT NULL,
            created_by_ref                  TEXT,
            created_by_meta_json             TEXT
        )
        """,
        """
        CREATE TABLE action_snapshots (
            id                         TEXT PRIMARY KEY,
            document_id                TEXT NOT NULL REFERENCES documents(id),
            document_version_id        TEXT REFERENCES document_versions(id),
            ydoc_snapshot_sha256       TEXT NOT NULL,
            structured_head_sha256     TEXT NOT NULL,
            ydoc_generation_sha256     TEXT NOT NULL,
            baseline_projection_sha256 TEXT NOT NULL,
            projection_sha256          TEXT NOT NULL,
            projection_blob_sha256     TEXT NOT NULL,
            target_kind                TEXT NOT NULL,
            target_selector_json       TEXT NOT NULL,
            target_text_sha256         TEXT NOT NULL,
            target_blob_sha256         TEXT NOT NULL,
            context_boundary_json      TEXT NOT NULL,
            allowed_change_ranges_json TEXT NOT NULL,
            egress_boundary_json       TEXT NOT NULL,
            canonical_sha256           TEXT NOT NULL UNIQUE,
            created_at                 TEXT NOT NULL,
            created_by_kind            TEXT NOT NULL,
            created_by_ref             TEXT,
            created_by_meta_json        TEXT
        )
        """,
        """
        CREATE TABLE evaluation_plan_snapshots (
            id                   TEXT PRIMARY KEY,
            action_snapshot_id   TEXT NOT NULL REFERENCES action_snapshots(id),
            plan_json            TEXT NOT NULL,
            canonical_sha256     TEXT NOT NULL UNIQUE,
            created_at           TEXT NOT NULL,
            created_by_kind      TEXT NOT NULL,
            created_by_ref       TEXT,
            created_by_meta_json TEXT
        )
        """,
        """
        CREATE TABLE evaluation_runs (
            id                   TEXT PRIMARY KEY,
            action_snapshot_id   TEXT NOT NULL REFERENCES action_snapshots(id),
            plan_snapshot_id     TEXT NOT NULL
                REFERENCES evaluation_plan_snapshots(id),
            run_kind             TEXT NOT NULL,
            status               TEXT NOT NULL,
            canonical_sha256     TEXT NOT NULL UNIQUE,
            started_at           TEXT NOT NULL,
            completed_at         TEXT,
            created_by_kind      TEXT NOT NULL,
            created_by_ref       TEXT,
            created_by_meta_json TEXT
        )
        """,
        """
        CREATE TABLE check_executions (
            id                          TEXT PRIMARY KEY,
            evaluation_run_id           TEXT NOT NULL REFERENCES evaluation_runs(id),
            check_definition_version_id TEXT NOT NULL
                REFERENCES check_definition_versions(id),
            criterion_check_binding_id  TEXT NOT NULL
                REFERENCES criterion_check_bindings(id),
            mechanism                   TEXT NOT NULL,
            status                      TEXT NOT NULL,
            input_sha256                TEXT NOT NULL,
            output_sha256               TEXT,
            diagnostics_json            TEXT NOT NULL,
            producer_json               TEXT NOT NULL,
            canonical_sha256            TEXT NOT NULL UNIQUE,
            started_at                  TEXT NOT NULL,
            completed_at                TEXT,
            created_by_kind             TEXT NOT NULL,
            created_by_ref              TEXT,
            created_by_meta_json         TEXT
        )
        """,
        """
        CREATE TABLE evaluation_results (
            id                              TEXT PRIMARY KEY,
            evaluation_run_id               TEXT NOT NULL
                REFERENCES evaluation_runs(id),
            check_execution_id              TEXT NOT NULL
                REFERENCES check_executions(id),
            criterion_definition_version_id TEXT NOT NULL
                REFERENCES criterion_definition_versions(id),
            result_kind                     TEXT NOT NULL,
            severity                        TEXT NOT NULL,
            message                         TEXT NOT NULL,
            evidence_selector_json          TEXT,
            payload_json                    TEXT NOT NULL,
            canonical_sha256                TEXT NOT NULL UNIQUE,
            created_at                      TEXT NOT NULL,
            created_by_kind                 TEXT NOT NULL,
            created_by_ref                  TEXT,
            created_by_meta_json             TEXT
        )
        """,
        """
        CREATE TABLE routing_dispositions (
            id                     TEXT PRIMARY KEY,
            evaluation_result_id   TEXT NOT NULL REFERENCES evaluation_results(id),
            decision               TEXT NOT NULL,
            rationale              TEXT NOT NULL,
            policy_snapshot_sha256 TEXT,
            canonical_sha256       TEXT NOT NULL,
            created_at             TEXT NOT NULL,
            created_by_kind        TEXT NOT NULL,
            created_by_ref         TEXT,
            created_by_meta_json    TEXT
        )
        """,
        """
        CREATE TABLE result_relations (
            id                   TEXT PRIMARY KEY,
            evaluation_result_id TEXT NOT NULL REFERENCES evaluation_results(id),
            relation_kind        TEXT NOT NULL,
            target_kind          TEXT NOT NULL,
            target_ref           TEXT NOT NULL,
            canonical_sha256     TEXT NOT NULL UNIQUE,
            created_at           TEXT NOT NULL,
            created_by_kind      TEXT NOT NULL,
            created_by_ref       TEXT,
            created_by_meta_json TEXT
        )
        """,
        """
        CREATE TABLE model_call_authorization_receipts (
            id                    TEXT PRIMARY KEY,
            action_snapshot_id    TEXT NOT NULL REFERENCES action_snapshots(id),
            plan_snapshot_id      TEXT REFERENCES evaluation_plan_snapshots(id),
            provider              TEXT NOT NULL,
            model                 TEXT NOT NULL,
            context_sha256        TEXT NOT NULL,
            content_boundary_json TEXT NOT NULL,
            egress_class          TEXT NOT NULL,
            cost_ceiling_usd      REAL NOT NULL CHECK(cost_ceiling_usd >= 0),
            retry_limit           INTEGER NOT NULL CHECK(retry_limit >= 0),
            expires_at            TEXT NOT NULL,
            canonical_sha256      TEXT NOT NULL UNIQUE,
            created_at            TEXT NOT NULL,
            created_by_kind       TEXT NOT NULL,
            created_by_ref        TEXT,
            created_by_meta_json  TEXT
        )
        """,
        """
        CREATE TABLE cothink_items (
            id                   TEXT PRIMARY KEY,
            action_snapshot_id   TEXT NOT NULL REFERENCES action_snapshots(id),
            subtype              TEXT NOT NULL,
            purpose              TEXT NOT NULL,
            payload_json         TEXT NOT NULL,
            rationale            TEXT NOT NULL,
            delivery_state       TEXT NOT NULL,
            provenance_json      TEXT NOT NULL,
            canonical_sha256     TEXT NOT NULL UNIQUE,
            created_at           TEXT NOT NULL,
            created_by_kind      TEXT NOT NULL,
            created_by_ref       TEXT,
            created_by_meta_json TEXT
        )
        """,
        "CREATE INDEX idx_criterion_definitions_key "
        "ON criterion_definition_versions(stable_key, version)",
        "CREATE INDEX idx_check_definitions_key "
        "ON check_definition_versions(stable_key, version)",
        "CREATE INDEX idx_criterion_bindings_criterion "
        "ON criterion_check_bindings(criterion_definition_version_id)",
        "CREATE INDEX idx_criterion_activations_criterion "
        "ON criterion_activations(criterion_definition_version_id, created_at, id)",
        "CREATE INDEX idx_action_snapshots_document "
        "ON action_snapshots(document_id, created_at, id)",
        "CREATE INDEX idx_evaluation_plans_action "
        "ON evaluation_plan_snapshots(action_snapshot_id)",
        "CREATE INDEX idx_evaluation_runs_action "
        "ON evaluation_runs(action_snapshot_id, started_at, id)",
        "CREATE INDEX idx_check_executions_run "
        "ON check_executions(evaluation_run_id, started_at, id)",
        "CREATE INDEX idx_evaluation_results_run "
        "ON evaluation_results(evaluation_run_id, created_at, id)",
        "CREATE INDEX idx_routing_dispositions_result "
        "ON routing_dispositions(evaluation_result_id, created_at, id)",
        "CREATE INDEX idx_result_relations_result "
        "ON result_relations(evaluation_result_id, created_at, id)",
        "CREATE INDEX idx_model_authorizations_action "
        "ON model_call_authorization_receipts(action_snapshot_id, created_at, id)",
        "CREATE INDEX idx_cothink_items_action "
        "ON cothink_items(action_snapshot_id, created_at, id)",
    )
    for statement in statements:
        conn.execute(statement)

    immutable_tables = (
        "criterion_definition_versions",
        "check_definition_versions",
        "criterion_check_bindings",
        "criterion_activations",
        "action_snapshots",
        "evaluation_plan_snapshots",
        "evaluation_runs",
        "check_executions",
        "evaluation_results",
        "routing_dispositions",
        "result_relations",
        "model_call_authorization_receipts",
        "cothink_items",
    )
    for table in immutable_tables:
        conn.execute(
            f"""
            CREATE TRIGGER {table}_append_only_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {table}_append_only_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )


def _m006_cothink_item_lifecycle(conn: sqlite3.Connection) -> None:
    """Add the durable Co-think item lifecycle event stream.

    Every pre-v6 item receives one deterministic ``open`` event. The backfill
    preserves the immutable item and appends migration facts after the
    pre-existing ledger rather than rewriting history.
    """

    import hashlib
    import json

    conn.execute(
        """
        CREATE TABLE cothink_item_status_events (
            id                   TEXT PRIMARY KEY,
            cothink_item_id      TEXT NOT NULL REFERENCES cothink_items(id),
            status               TEXT NOT NULL
                CHECK(status IN ('open', 'parked', 'dismissed')),
            reason               TEXT,
            canonical_sha256     TEXT NOT NULL UNIQUE,
            created_at           TEXT NOT NULL,
            created_by_kind      TEXT NOT NULL,
            created_by_ref       TEXT,
            created_by_meta_json TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_cothink_item_status_item "
        "ON cothink_item_status_events(cothink_item_id, created_at, id)"
    )
    conn.execute(
        """
        CREATE TRIGGER cothink_item_status_events_append_only_update
        BEFORE UPDATE ON cothink_item_status_events
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER cothink_item_status_events_append_only_delete
        BEFORE DELETE ON cothink_item_status_events
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """
    )

    seed = b"work-buddy:cothink-item-status:v1\0"
    rows = conn.execute(
        "SELECT id, created_at FROM cothink_items ORDER BY rowid"
    ).fetchall()
    for row in rows:
        item_id = str(row[0])
        event_id = hashlib.sha256(seed + item_id.encode("utf-8")).hexdigest()[:32]
        payload = {
            "cothink_item_id": item_id,
            "reason": None,
            "status": "open",
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO cothink_item_status_events "
            "(id, cothink_item_id, status, reason, canonical_sha256, "
            "created_at, created_by_kind, created_by_ref, created_by_meta_json) "
            "VALUES (?, ?, 'open', NULL, ?, ?, ?, ?, ?)",
            (
                event_id,
                item_id,
                digest,
                row[1],
                "system",
                "truth-schema-v6",
                '{"basis":"pre_lifecycle_item_existence"}',
            ),
        )
        conn.execute(
            "INSERT INTO ledger_records (record_type, record_key) VALUES (?, ?)",
            ("cothink_item_status_event", event_id),
        )


def _m007_portable_cowork_coordination(conn: sqlite3.Connection) -> None:
    """Add sanitized, append-only Verify/Co-think coordination history."""

    statements = (
        """
        CREATE TABLE cowork_coordination_jobs (
            id                       TEXT PRIMARY KEY,
            document_id              TEXT NOT NULL REFERENCES documents(id),
            evaluation_run_id        TEXT REFERENCES evaluation_runs(id),
            action_snapshot_id       TEXT NOT NULL REFERENCES action_snapshots(id),
            plan_snapshot_id         TEXT REFERENCES evaluation_plan_snapshots(id),
            role                     TEXT NOT NULL
                CHECK(role IN ('specialist', 'reviser', 'coordinator', 'cothink')),
            parent_job_id            TEXT REFERENCES cowork_coordination_jobs(id),
            authorization_receipt_id TEXT NOT NULL
                REFERENCES model_call_authorization_receipts(id),
            context_sha256           TEXT NOT NULL,
            selection_json           TEXT NOT NULL,
            request_summary_json     TEXT NOT NULL,
            canonical_sha256         TEXT NOT NULL UNIQUE,
            created_at               TEXT NOT NULL,
            created_by_kind          TEXT NOT NULL,
            created_by_ref           TEXT,
            created_by_meta_json     TEXT
        )
        """,
        """
        CREATE TABLE cowork_coordination_status_events (
            id                    TEXT PRIMARY KEY,
            coordination_job_id   TEXT NOT NULL REFERENCES cowork_coordination_jobs(id),
            status                TEXT NOT NULL CHECK(status IN (
                'prepared', 'launching', 'running', 'submitted',
                'completed', 'unavailable', 'failed'
            )),
            outcome_kind          TEXT CHECK(outcome_kind IS NULL OR outcome_kind IN (
                'typed_submission_received', 'routing_completed',
                'revision_requested', 'revision_candidate_prepared',
                'correction_routing_completed', 'completed_with_item',
                'completed_no_useful_item', 'unavailable'
            )),
            output_sha256         TEXT,
            error_code            TEXT,
            message               TEXT,
            consequence_refs_json TEXT NOT NULL,
            canonical_sha256      TEXT NOT NULL UNIQUE,
            created_at            TEXT NOT NULL,
            created_by_kind       TEXT NOT NULL,
            created_by_ref        TEXT,
            created_by_meta_json  TEXT
        )
        """,
        """
        CREATE TABLE cowork_review_applications (
            id                        TEXT PRIMARY KEY,
            document_id               TEXT NOT NULL REFERENCES documents(id),
            applied_proposal_ids_json TEXT NOT NULL,
            canonical_sha256          TEXT NOT NULL UNIQUE,
            committed_at              TEXT NOT NULL,
            created_by_kind           TEXT NOT NULL,
            created_by_ref            TEXT,
            created_by_meta_json      TEXT
        )
        """,
        "CREATE INDEX idx_cowork_coordination_document "
        "ON cowork_coordination_jobs(document_id, created_at, id)",
        "CREATE INDEX idx_cowork_coordination_run "
        "ON cowork_coordination_jobs(evaluation_run_id, created_at, id)",
        "CREATE INDEX idx_cowork_coordination_parent "
        "ON cowork_coordination_jobs(parent_job_id)",
        "CREATE INDEX idx_cowork_coordination_status_job "
        "ON cowork_coordination_status_events("
        "coordination_job_id, created_at, id)",
        "CREATE INDEX idx_cowork_review_applications_document "
        "ON cowork_review_applications(document_id, committed_at, id)",
    )
    for statement in statements:
        conn.execute(statement)

    for table in (
        "cowork_coordination_jobs",
        "cowork_coordination_status_events",
        "cowork_review_applications",
    ):
        conn.execute(
            f"""
            CREATE TRIGGER {table}_append_only_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {table}_append_only_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )


def _m008_document_provenance_attestations(
    conn: sqlite3.Connection,
) -> None:
    """Add durable, target-bound authorship and human-review attestations.

    Existing imports predate the distinction between an acquisition source
    and a Save target.  Backfill those documents while the narrow document
    update trigger is temporarily replaced inside this migration transaction.
    """

    import json

    # v3 created bootstrap intents before From file carried a provenance
    # determination.  Persist the importer contract and the digest of the
    # staged determination so a commit cannot silently substitute or lose
    # either one.  Rows prepared by older code remain NULL and are the only
    # imports eligible for the legacy Unknown fallback.
    bootstrap_columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(cowork_bootstrap_intents)"
        ).fetchall()
    }
    for name in (
        "importer_id",
        "source_media_type",
        "import_attestation_sha256",
    ):
        if name not in bootstrap_columns:
            conn.execute(
                f"ALTER TABLE cowork_bootstrap_intents ADD COLUMN {name} TEXT"
            )

    statements = (
        """
        CREATE TABLE document_provenance_attestations (
            id                            TEXT PRIMARY KEY,
            document_id                   TEXT NOT NULL REFERENCES documents(id),
            target_kind                   TEXT NOT NULL CHECK(
                target_kind IN ('document_version', 'document_span')
            ),
            document_version_id           TEXT REFERENCES document_versions(id),
            document_span_id              TEXT REFERENCES document_spans(id),
            target_structured_head_sha256 TEXT NOT NULL,
            authorship_kind               TEXT NOT NULL CHECK(
                authorship_kind IN ('human', 'ai', 'mixed', 'unknown')
            ),
            human_contributors_json       TEXT NOT NULL,
            review_status                 TEXT NOT NULL CHECK(
                review_status IN (
                    'reviewed', 'not_reviewed', 'not_applicable', 'unknown'
                )
            ),
            human_reviewers_json          TEXT NOT NULL,
            source_kind                   TEXT NOT NULL CHECK(
                source_kind IN (
                    'file_import', 'paste', 'direct_entry',
                    'proposal_acceptance', 'legacy'
                )
            ),
            source_json                   TEXT NOT NULL,
            basis_kind                    TEXT NOT NULL CHECK(
                basis_kind IN (
                    'user_attestation', 'automatic_short_text_attribution',
                    'proposal_acceptance', 'migration_backfill', 'legacy'
                )
            ),
            basis_ref                     TEXT,
            supersedes_id                 TEXT
                REFERENCES document_provenance_attestations(id),
            idempotency_key               TEXT NOT NULL,
            canonical_sha256              TEXT NOT NULL UNIQUE,
            created_at                    TEXT NOT NULL,
            attested_by_kind              TEXT NOT NULL,
            attested_by_ref               TEXT,
            attested_by_meta_json         TEXT,
            CHECK (
                (
                    target_kind = 'document_version'
                    AND document_version_id IS NOT NULL
                    AND document_span_id IS NULL
                )
                OR
                (
                    target_kind = 'document_span'
                    AND document_span_id IS NOT NULL
                    AND document_version_id IS NULL
                )
            )
        )
        """,
        "CREATE INDEX idx_document_provenance_document "
        "ON document_provenance_attestations(document_id, created_at, id)",
        "CREATE INDEX idx_document_provenance_version "
        "ON document_provenance_attestations(document_version_id)",
        "CREATE INDEX idx_document_provenance_span "
        "ON document_provenance_attestations(document_span_id)",
        "CREATE INDEX idx_document_provenance_supersedes "
        "ON document_provenance_attestations(supersedes_id)",
        "CREATE UNIQUE INDEX uq_document_provenance_idempotency "
        "ON document_provenance_attestations("
        "document_id, attested_by_kind, ifnull(attested_by_ref, ''), "
        "idempotency_key)",
        """
        CREATE TRIGGER document_provenance_attestations_append_only_update
        BEFORE UPDATE ON document_provenance_attestations
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
        """
        CREATE TRIGGER document_provenance_attestations_append_only_delete
        BEFORE DELETE ON document_provenance_attestations
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
    )
    for statement in statements:
        conn.execute(statement)

    imported_rows = conn.execute(
        """
        SELECT d.id, d.path, d.meta_json, v.projection_sha256
        FROM documents AS d
        JOIN document_versions AS v ON v.document_id = d.id
        WHERE v.kind = 'initial_import' AND v.detail = 'import'
        ORDER BY d.id, v.created_at, v.rowid
        """
    ).fetchall()
    first_import_by_document: dict[str, tuple[str, str, str | None, str]] = {}
    for row in imported_rows:
        document_id = str(row[0])
        first_import_by_document.setdefault(
            document_id,
            (
                document_id,
                str(row[1]),
                None if row[2] is None else str(row[2]),
                str(row[3]),
            ),
        )

    conn.execute("DROP TRIGGER IF EXISTS documents_append_only_update")
    for document_id, path, raw_meta, source_sha256 in (
        first_import_by_document.values()
    ):
        try:
            meta = json.loads(raw_meta) if raw_meta else {}
        except (TypeError, json.JSONDecodeError) as exc:
            raise sqlite3.IntegrityError(
                f"imported document {document_id} has invalid meta_json"
            ) from exc
        if not isinstance(meta, dict):
            raise sqlite3.IntegrityError(
                f"imported document {document_id} meta_json is not an object"
            )
        existing_source = meta.get("source")
        if existing_source is not None and not isinstance(existing_source, dict):
            raise sqlite3.IntegrityError(
                f"imported document {document_id} source metadata is not an object"
            )
        source = dict(existing_source or {})
        source.update(
            {
                "kind": "file_import",
                "path": path,
                "sha256": source_sha256,
                "writeback_policy": "never",
            }
        )
        meta["source"] = source
        conn.execute(
            "UPDATE documents SET meta_json = ? WHERE id = ?",
            (
                json.dumps(
                    meta,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                document_id,
            ),
        )

    conn.execute(
        """
        CREATE TRIGGER documents_append_only_update
        BEFORE UPDATE ON documents
        WHEN NOT (
            NEW.id IS OLD.id
            AND NEW.path IS OLD.path
            AND NEW.title IS OLD.title
            AND NEW.document_class IS OLD.document_class
            AND NEW.created_at IS OLD.created_at
            AND NEW.created_by_kind IS OLD.created_by_kind
            AND NEW.created_by_ref IS OLD.created_by_ref
            AND NEW.meta_json IS OLD.meta_json
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """
    )


def _v8_legacy_import_provenance_rows(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    if not _table_exists(conn, "document_provenance_attestations"):
        return []
    return list(
        conn.execute(
            """
            SELECT d.id AS document_id, d.path, d.meta_json,
                   v.id AS version_id, v.structured_head_sha256, v.created_at
            FROM documents AS d
            JOIN document_versions AS v ON v.document_id = d.id
            WHERE v.kind = 'initial_import'
              AND v.detail = 'import'
              AND NOT EXISTS (
                  SELECT 1
                  FROM document_provenance_attestations AS p
                  WHERE p.target_kind = 'document_version'
                    AND p.document_version_id = v.id
              )
            ORDER BY d.id, v.created_at, v.rowid
            """
        )
    )


def _v8_legacy_import_source(row: sqlite3.Row) -> dict[str, str] | None:
    import json

    try:
        meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
    except (TypeError, json.JSONDecodeError):
        return None
    source_meta = meta.get("source") if isinstance(meta, dict) else None
    if not isinstance(source_meta, dict):
        return None
    if (
        source_meta.get("kind") not in {"file_import", "imported_markdown"}
        or source_meta.get("writeback_policy") != "never"
    ):
        return None
    source_sha256 = source_meta.get("sha256")
    if (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in source_sha256)
    ):
        return None
    return {
        "kind": "file_import",
        "path": str(row["path"]),
        "sha256": source_sha256,
    }


def needs_v8_legacy_import_provenance_backfill(
    conn: sqlite3.Connection,
) -> bool:
    """Read-only compatibility probe used to avoid a write lock per open."""

    return any(
        _v8_legacy_import_source(row) is not None
        for row in _v8_legacy_import_provenance_rows(conn)
    )


def backfill_v8_legacy_import_provenance(
    conn: sqlite3.Connection,
) -> int:
    """Append deterministic Unknown attestations omitted by early v8 builds.

    This compatibility backfill intentionally lives outside the hashed v8
    migration function. Some local stores already ran that migration while
    the feature was under development, so changing its recorded code hash
    would make those otherwise valid stores impossible to open.

    The caller owns the transaction. Repeated calls append nothing.
    """

    from work_buddy.truth.identity import canonical_json, sha256_text
    from work_buddy.truth.provenance import (
        ATTESTATION_SCHEMA,
        attestation_canonical_sha256,
    )

    inserted = 0
    for row in _v8_legacy_import_provenance_rows(conn):
        source = _v8_legacy_import_source(row)
        if source is None:
            continue

        document_id = str(row["document_id"])
        version_id = str(row["version_id"])
        target_head = str(row["structured_head_sha256"])
        basis_ref = "truth-schema-v8:legacy-file-import"
        canonical = attestation_canonical_sha256(
            document_id=document_id,
            target_kind="document_version",
            document_version_id=version_id,
            document_span_id=None,
            target_structured_head_sha256=target_head,
            authorship_kind="unknown",
            human_contributors=[],
            review_status="unknown",
            human_reviewers=[],
            source_kind="file_import",
            source=source,
            basis_kind="migration_backfill",
            basis_ref=basis_ref,
            supersedes_id=None,
            attested_by_kind="system",
            attested_by_ref=None,
            attested_by_meta=None,
        )
        identifier = sha256_text(
            canonical_json(
                {
                    "schema": ATTESTATION_SCHEMA,
                    "document_id": document_id,
                    "document_version_id": version_id,
                    "basis_kind": "migration_backfill",
                }
            )
        )[:32]
        conn.execute(
            """
            INSERT INTO document_provenance_attestations (
                id, document_id, target_kind, document_version_id,
                document_span_id, target_structured_head_sha256,
                authorship_kind, human_contributors_json, review_status,
                human_reviewers_json, source_kind, source_json, basis_kind,
                basis_ref, supersedes_id, idempotency_key, canonical_sha256,
                created_at, attested_by_kind, attested_by_ref,
                attested_by_meta_json
            ) VALUES (
                ?, ?, 'document_version', ?, NULL, ?,
                'unknown', '[]', 'unknown', '[]', 'file_import', ?,
                'migration_backfill', ?, NULL, ?, ?, ?, 'system', NULL, NULL
            )
            """,
            (
                identifier,
                document_id,
                version_id,
                target_head,
                canonical_json(source),
                basis_ref,
                f"migration:v8:file-import:{version_id}",
                canonical,
                str(row["created_at"]),
            ),
        )
        conn.execute(
            "INSERT INTO ledger_records (record_type, record_key) VALUES (?, ?)",
            ("document_provenance_attestation", identifier),
        )
        inserted += 1
    return inserted


def _m009_source_backed_truth_provenance(conn: sqlite3.Connection) -> None:
    """Add portable source receipts and outcome-aware Truth provenance.

    Historical creator fields are intentionally not rewritten or promoted to
    issuer-qualified authorship. Read models classify subjects with no v9
    attribution events as ``legacy_unspecified``.
    """

    # Exact managed-document source redaction retains immutable identities and
    # digests while destroying every readable quotation/frozen input.  These
    # nullable tombstone timestamps are deliberately introduced only in v9;
    # historical rows remain live until an explicit, source-bound receipt is
    # committed by the document-content redaction service.
    document_span_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(document_spans)")
    }
    if "redacted_at" not in document_span_columns:
        conn.execute("ALTER TABLE document_spans ADD COLUMN redacted_at TEXT")
    action_snapshot_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(action_snapshots)")
    }
    if "redacted_at" not in action_snapshot_columns:
        conn.execute("ALTER TABLE action_snapshots ADD COLUMN redacted_at TEXT")

    statements = (
        """
        CREATE TABLE evidence_source_resolution_records (
            id                           TEXT PRIMARY KEY,
            evidence_id                  TEXT NOT NULL REFERENCES evidence(id),
            source_ref_json              TEXT NOT NULL,
            representation_id            TEXT NOT NULL,
            content_sha256               TEXT NOT NULL,
            media_type                   TEXT NOT NULL,
            byte_length                  INTEGER NOT NULL CHECK(byte_length >= 0),
            selector_json                TEXT,
            resolver_id                  TEXT NOT NULL,
            resolver_version             TEXT NOT NULL,
            observation_id               TEXT NOT NULL,
            redaction_epoch              INTEGER NOT NULL CHECK(redaction_epoch >= 0),
            resolved_at                  TEXT NOT NULL,
            usage_id                     TEXT NOT NULL UNIQUE,
            authorization_context_sha256 TEXT NOT NULL,
            canonical_sha256             TEXT NOT NULL UNIQUE,
            created_at                   TEXT NOT NULL,
            created_by_kind              TEXT NOT NULL,
            created_by_ref               TEXT,
            created_by_meta_json          TEXT
        )
        """,
        "CREATE INDEX idx_evidence_source_resolution_evidence "
        "ON evidence_source_resolution_records(evidence_id, created_at, id)",
        "CREATE INDEX idx_evidence_source_resolution_source "
        "ON evidence_source_resolution_records(source_ref_json)",
        """
        CREATE TABLE truth_source_usage_events (
            id                     TEXT PRIMARY KEY,
            resolution_record_id   TEXT NOT NULL
                REFERENCES evidence_source_resolution_records(id),
            usage_id               TEXT NOT NULL,
            status                 TEXT NOT NULL CHECK(
                status IN (
                    'reserved', 'acknowledgement_pending', 'acknowledged',
                    'release_pending', 'released', 'redaction_pending'
                )
            ),
            purpose                TEXT NOT NULL,
            consumer_ref           TEXT NOT NULL,
            redaction_epoch        INTEGER NOT NULL CHECK(redaction_epoch >= 0),
            error_code             TEXT,
            canonical_sha256       TEXT NOT NULL UNIQUE,
            created_at             TEXT NOT NULL,
            created_by_kind        TEXT NOT NULL,
            created_by_ref         TEXT,
            created_by_meta_json   TEXT
        )
        """,
        "CREATE INDEX idx_truth_source_usage_resolution "
        "ON truth_source_usage_events(resolution_record_id, created_at, id)",
        "CREATE INDEX idx_truth_source_usage_id "
        "ON truth_source_usage_events(usage_id, created_at, id)",
        """
        CREATE TABLE provenance_attribution_events (
            id                TEXT PRIMARY KEY,
            subject_kind      TEXT NOT NULL CHECK(
                subject_kind IN ('claim', 'expression', 'evidence', 'evidence_span')
            ),
            subject_ref       TEXT NOT NULL,
            actor_ref_json    TEXT NOT NULL,
            role              TEXT NOT NULL CHECK(
                role IN (
                    'semantic_producer', 'selector', 'candidate_preparer',
                    'matcher', 'semantic_reviser', 'evidence_selector',
                    'expression_relation_assessor', 'applier',
                    'execution_authorizer', 'substantive_reviewer',
                    'candidate_decision_actor', 'lifecycle_decision_actor'
                )
            ),
            basis             TEXT NOT NULL,
            assurance         TEXT NOT NULL,
            run_ref           TEXT,
            source_ref_json   TEXT,
            asserted_at       TEXT NOT NULL,
            supersedes_id     TEXT REFERENCES provenance_attribution_events(id),
            canonical_sha256  TEXT NOT NULL UNIQUE,
            created_at        TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_provenance_attribution_subject "
        "ON provenance_attribution_events(subject_kind, subject_ref, asserted_at, id)",
        "CREATE INDEX idx_provenance_attribution_actor "
        "ON provenance_attribution_events(actor_ref_json, asserted_at, id)",
        "CREATE INDEX idx_provenance_attribution_supersedes "
        "ON provenance_attribution_events(supersedes_id)",
        """
        CREATE TABLE candidate_decision_events (
            id                           TEXT PRIMARY KEY,
            candidate_id                 TEXT NOT NULL,
            candidate_sha256             TEXT NOT NULL,
            decision                     TEXT NOT NULL CHECK(
                decision IN ('add', 'connect', 'dismiss')
            ),
            claim_id                     TEXT REFERENCES claims(id),
            actor_ref_json               TEXT NOT NULL,
            basis                        TEXT NOT NULL,
            assurance                    TEXT NOT NULL,
            authorization_ref            TEXT NOT NULL,
            authorization_context_sha256 TEXT NOT NULL,
            run_ref                      TEXT,
            source_refs_json             TEXT NOT NULL,
            decided_at                   TEXT NOT NULL,
            canonical_sha256             TEXT NOT NULL UNIQUE,
            created_at                   TEXT NOT NULL,
            CHECK (
                (decision = 'dismiss' AND claim_id IS NULL)
                OR (decision IN ('add', 'connect') AND claim_id IS NOT NULL)
            )
        )
        """,
        "CREATE INDEX idx_candidate_decision_candidate "
        "ON candidate_decision_events(candidate_id, decided_at, id)",
        "CREATE INDEX idx_candidate_decision_claim "
        "ON candidate_decision_events(claim_id, decided_at, id)",
        """
        CREATE TABLE truth_operation_results (
            id                TEXT PRIMARY KEY,
            operation_name    TEXT NOT NULL,
            idempotency_key   TEXT NOT NULL,
            request_sha256    TEXT NOT NULL,
            result_json       TEXT NOT NULL,
            result_sha256     TEXT NOT NULL,
            actor_ref_json    TEXT NOT NULL,
            canonical_sha256  TEXT NOT NULL UNIQUE,
            created_at        TEXT NOT NULL,
            UNIQUE(operation_name, idempotency_key)
        )
        """,
        "CREATE INDEX idx_truth_operation_created "
        "ON truth_operation_results(operation_name, created_at, id)",
        """
        CREATE TABLE document_content_redactions (
            id                              TEXT PRIMARY KEY,
            document_id                     TEXT NOT NULL REFERENCES documents(id),
            replacement_document_version_id TEXT NOT NULL
                REFERENCES document_versions(id),
            source_usage_id                 TEXT NOT NULL,
            source_ref_json                 TEXT NOT NULL,
            source_redaction_event_id        TEXT NOT NULL,
            content_class                   TEXT NOT NULL
                CHECK(content_class = 'exact_copy'),
            redaction_policy                TEXT NOT NULL
                CHECK(redaction_policy = 'scrub'),
            actor_ref_json                  TEXT NOT NULL,
            coverage_sha256                 TEXT NOT NULL,
            canonical_sha256                TEXT NOT NULL UNIQUE,
            created_at                      TEXT NOT NULL,
            UNIQUE(document_id, source_usage_id, source_redaction_event_id)
        )
        """,
        "CREATE INDEX idx_document_content_redaction_document "
        "ON document_content_redactions(document_id, created_at, id)",
        """
        CREATE TABLE document_content_redaction_targets (
            id                 TEXT PRIMARY KEY,
            redaction_id       TEXT NOT NULL
                REFERENCES document_content_redactions(id),
            target_kind        TEXT NOT NULL CHECK(target_kind IN (
                'document_version_blob', 'document_source_blob',
                'action_snapshot_blob', 'action_snapshot_metadata',
                'document_span', 'proposal', 'semantic_derivative'
            )),
            target_ref         TEXT NOT NULL,
            field_name         TEXT NOT NULL,
            content_sha256     TEXT,
            disposition        TEXT NOT NULL CHECK(disposition IN (
                'blob_cleanup', 'sql_tombstone', 'review_required'
            )),
            canonical_sha256   TEXT NOT NULL UNIQUE,
            created_at         TEXT NOT NULL,
            UNIQUE(redaction_id, target_kind, target_ref, field_name)
        )
        """,
        "CREATE INDEX idx_document_content_redaction_target_receipt "
        "ON document_content_redaction_targets(redaction_id, target_kind, target_ref)",
        "CREATE INDEX idx_document_content_redaction_target_blob "
        "ON document_content_redaction_targets(content_sha256) "
        "WHERE content_sha256 IS NOT NULL",
        """
        CREATE TABLE document_content_redaction_status_events (
            id                 TEXT PRIMARY KEY,
            redaction_id       TEXT NOT NULL
                REFERENCES document_content_redactions(id),
            status             TEXT NOT NULL CHECK(status IN (
                'content_tombstoned', 'cleanup_complete', 'cleanup_incomplete'
            )),
            detail_json        TEXT NOT NULL,
            canonical_sha256   TEXT NOT NULL UNIQUE,
            created_at         TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_document_content_redaction_status "
        "ON document_content_redaction_status_events(redaction_id, created_at, id)",
    )
    for statement in statements:
        conn.execute(statement)

    for table in (
        "evidence_source_resolution_records",
        "truth_source_usage_events",
        "provenance_attribution_events",
        "candidate_decision_events",
        "truth_operation_results",
        "document_content_redactions",
        "document_content_redaction_targets",
        "document_content_redaction_status_events",
    ):
        conn.execute(
            f"""
            CREATE TRIGGER {table}_append_only_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {table}_append_only_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )

    # Recreate the two append-only guards with a single, one-way readable-
    # content tombstone carve-out.  IDs, hashes, authorship, and timestamps
    # remain immutable; only the exact quote/selector context can disappear.
    conn.execute("DROP TRIGGER IF EXISTS document_spans_append_only_update")
    conn.execute(
        f"""
        CREATE TRIGGER document_spans_append_only_update
        BEFORE UPDATE ON document_spans
        WHEN NOT (
            OLD.redacted_at IS NULL
            AND NEW.redacted_at IS NOT NULL
            AND NEW.selector_json = '{REDACTED_SELECTOR_JSON}'
            AND NEW.quote_exact IS NULL
            AND NEW.id IS OLD.id
            AND NEW.document_id IS OLD.document_id
            AND NEW.span_sha256 IS OLD.span_sha256
            AND NEW.author_kind IS OLD.author_kind
            AND NEW.author_ref IS OLD.author_ref
            AND NEW.created_at IS OLD.created_at
            AND NEW.created_by_kind IS OLD.created_by_kind
            AND NEW.created_by_ref IS OLD.created_by_ref
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS action_snapshots_append_only_update")
    conn.execute(
        f"""
        CREATE TRIGGER action_snapshots_append_only_update
        BEFORE UPDATE ON action_snapshots
        WHEN NOT (
            OLD.redacted_at IS NULL
            AND NEW.redacted_at IS NOT NULL
            AND NEW.target_selector_json = '{REDACTED_ACTION_CONTEXT_JSON}'
            AND NEW.context_boundary_json = '{REDACTED_ACTION_CONTEXT_JSON}'
            AND NEW.allowed_change_ranges_json = '[]'
            AND NEW.id IS OLD.id
            AND NEW.document_id IS OLD.document_id
            AND NEW.document_version_id IS OLD.document_version_id
            AND NEW.ydoc_snapshot_sha256 IS OLD.ydoc_snapshot_sha256
            AND NEW.structured_head_sha256 IS OLD.structured_head_sha256
            AND NEW.ydoc_generation_sha256 IS OLD.ydoc_generation_sha256
            AND NEW.baseline_projection_sha256 IS OLD.baseline_projection_sha256
            AND NEW.projection_sha256 IS OLD.projection_sha256
            AND NEW.projection_blob_sha256 IS OLD.projection_blob_sha256
            AND NEW.target_kind IS OLD.target_kind
            AND NEW.target_text_sha256 IS OLD.target_text_sha256
            AND NEW.target_blob_sha256 IS OLD.target_blob_sha256
            AND NEW.egress_boundary_json IS OLD.egress_boundary_json
            AND NEW.canonical_sha256 IS OLD.canonical_sha256
            AND NEW.created_at IS OLD.created_at
            AND NEW.created_by_kind IS OLD.created_by_kind
            AND NEW.created_by_ref IS OLD.created_by_ref
            AND NEW.created_by_meta_json IS OLD.created_by_meta_json
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """
    )

    # Hindsight delivery is a Truth-owned, content-minimized outbox. Install
    # its tables in this same migration transaction so lifecycle writes can
    # enqueue a projection effect atomically with the authoritative decision.
    install_truth_hindsight_projection_schema(conn)


def _m010_direct_entry_provenance_basis(conn: sqlite3.Connection) -> None:
    """Admit honest automatic attribution for text typed in Co-work.

    SQLite cannot widen a CHECK constraint in place. Rebuild only the
    append-only provenance table, preserving every row and its insertion
    order; indexes and immutability triggers are then restored verbatim.
    """

    conn.execute(
        """
        CREATE TABLE document_provenance_attestations_v10 (
            id                            TEXT PRIMARY KEY,
            document_id                   TEXT NOT NULL REFERENCES documents(id),
            target_kind                   TEXT NOT NULL CHECK(
                target_kind IN ('document_version', 'document_span')
            ),
            document_version_id           TEXT REFERENCES document_versions(id),
            document_span_id              TEXT REFERENCES document_spans(id),
            target_structured_head_sha256 TEXT NOT NULL,
            authorship_kind               TEXT NOT NULL CHECK(
                authorship_kind IN ('human', 'ai', 'mixed', 'unknown')
            ),
            human_contributors_json       TEXT NOT NULL,
            review_status                 TEXT NOT NULL CHECK(
                review_status IN (
                    'reviewed', 'not_reviewed', 'not_applicable', 'unknown'
                )
            ),
            human_reviewers_json          TEXT NOT NULL,
            source_kind                   TEXT NOT NULL CHECK(
                source_kind IN (
                    'file_import', 'paste', 'direct_entry',
                    'proposal_acceptance', 'legacy'
                )
            ),
            source_json                   TEXT NOT NULL,
            basis_kind                    TEXT NOT NULL CHECK(
                basis_kind IN (
                    'user_attestation', 'automatic_short_text_attribution',
                    'automatic_direct_entry_attribution',
                    'proposal_acceptance', 'migration_backfill', 'legacy'
                )
            ),
            basis_ref                     TEXT,
            supersedes_id                 TEXT
                REFERENCES document_provenance_attestations_v10(id),
            idempotency_key               TEXT NOT NULL,
            canonical_sha256              TEXT NOT NULL UNIQUE,
            created_at                    TEXT NOT NULL,
            attested_by_kind              TEXT NOT NULL,
            attested_by_ref               TEXT,
            attested_by_meta_json         TEXT,
            CHECK (
                (
                    target_kind = 'document_version'
                    AND document_version_id IS NOT NULL
                    AND document_span_id IS NULL
                )
                OR
                (
                    target_kind = 'document_span'
                    AND document_span_id IS NOT NULL
                    AND document_version_id IS NULL
                )
            )
        )
        """
    )
    conn.execute(
        """
        INSERT INTO document_provenance_attestations_v10 (
            id, document_id, target_kind, document_version_id,
            document_span_id, target_structured_head_sha256, authorship_kind,
            human_contributors_json, review_status, human_reviewers_json,
            source_kind, source_json, basis_kind, basis_ref, supersedes_id,
            idempotency_key, canonical_sha256, created_at, attested_by_kind,
            attested_by_ref, attested_by_meta_json
        )
        SELECT
            id, document_id, target_kind, document_version_id,
            document_span_id, target_structured_head_sha256, authorship_kind,
            human_contributors_json, review_status, human_reviewers_json,
            source_kind, source_json, basis_kind, basis_ref, supersedes_id,
            idempotency_key, canonical_sha256, created_at, attested_by_kind,
            attested_by_ref, attested_by_meta_json
        FROM document_provenance_attestations
        ORDER BY rowid
        """
    )
    conn.execute("DROP TABLE document_provenance_attestations")
    conn.execute(
        "ALTER TABLE document_provenance_attestations_v10 "
        "RENAME TO document_provenance_attestations"
    )
    for statement in (
        "CREATE INDEX idx_document_provenance_document "
        "ON document_provenance_attestations(document_id, created_at, id)",
        "CREATE INDEX idx_document_provenance_version "
        "ON document_provenance_attestations(document_version_id)",
        "CREATE INDEX idx_document_provenance_span "
        "ON document_provenance_attestations(document_span_id)",
        "CREATE INDEX idx_document_provenance_supersedes "
        "ON document_provenance_attestations(supersedes_id)",
        "CREATE UNIQUE INDEX uq_document_provenance_idempotency "
        "ON document_provenance_attestations("
        "document_id, attested_by_kind, ifnull(attested_by_ref, ''), "
        "idempotency_key)",
        """
        CREATE TRIGGER document_provenance_attestations_append_only_update
        BEFORE UPDATE ON document_provenance_attestations
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
        """
        CREATE TRIGGER document_provenance_attestations_append_only_delete
        BEFORE DELETE ON document_provenance_attestations
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END
        """,
    ):
        conn.execute(statement)


def _m011_document_truth_activation(conn: sqlite3.Connection) -> None:
    """Add immutable document interaction contracts and Truth admission history."""

    statements = (
        """
        CREATE TABLE interaction_contract_definitions (
            id                  TEXT PRIMARY KEY,
            contract_id         TEXT NOT NULL,
            definition_version  INTEGER NOT NULL CHECK(definition_version > 0),
            definition_json     TEXT NOT NULL,
            definition_sha256   TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            UNIQUE(contract_id, definition_version)
        )
        """,
        """
        CREATE TABLE document_interaction_contract_assignments (
            id                           TEXT PRIMARY KEY,
            document_id                  TEXT NOT NULL UNIQUE REFERENCES documents(id),
            binding_id                   TEXT,
            interaction_contract_id      TEXT NOT NULL,
            interaction_contract_version INTEGER NOT NULL,
            interaction_contract_sha256  TEXT NOT NULL,
            cowork_document_class        TEXT NOT NULL CHECK(
                cowork_document_class IN ('co_authored', 'generated')
            ),
            actor_ref                    TEXT NOT NULL,
            intent_id                    TEXT NOT NULL,
            assigned_at                  TEXT NOT NULL,
            FOREIGN KEY(
                interaction_contract_id, interaction_contract_version
            ) REFERENCES interaction_contract_definitions(
                contract_id, definition_version
            )
        )
        """,
        """
        CREATE TABLE document_truth_activation_transitions (
            id                    TEXT PRIMARY KEY,
            document_id           TEXT NOT NULL REFERENCES documents(id),
            activation_revision   INTEGER NOT NULL CHECK(activation_revision > 0),
            previous_state        TEXT CHECK(
                previous_state IS NULL OR
                previous_state IN ('disabled', 'enabled', 'paused')
            ),
            next_state            TEXT NOT NULL CHECK(
                next_state IN ('disabled', 'enabled', 'paused')
            ),
            observed_head_sha256  TEXT,
            ledger_high_water_seq INTEGER NOT NULL CHECK(ledger_high_water_seq >= 0),
            ledger_digest         TEXT NOT NULL,
            actor_ref             TEXT NOT NULL,
            intent_id             TEXT NOT NULL,
            reason                TEXT,
            request_sha256        TEXT NOT NULL,
            created_at            TEXT NOT NULL,
            UNIQUE(document_id, activation_revision),
            UNIQUE(document_id, intent_id)
        )
        """,
        """
        CREATE TABLE document_truth_activation_current (
            document_id         TEXT PRIMARY KEY REFERENCES documents(id),
            activation_revision INTEGER NOT NULL CHECK(activation_revision > 0),
            state               TEXT NOT NULL CHECK(
                state IN ('disabled', 'enabled', 'paused')
            ),
            transition_id       TEXT NOT NULL UNIQUE
                REFERENCES document_truth_activation_transitions(id),
            updated_at          TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE document_truth_policy_receipts (
            id                           TEXT PRIMARY KEY,
            document_id                  TEXT NOT NULL REFERENCES documents(id),
            binding_id                   TEXT,
            interaction_contract_id      TEXT NOT NULL,
            interaction_contract_version INTEGER NOT NULL,
            interaction_contract_sha256  TEXT NOT NULL,
            outcome                      TEXT NOT NULL CHECK(
                outcome IN ('not_applicable', 'not_applicable_recovery')
            ),
            intent_id                    TEXT NOT NULL,
            actor_ref                    TEXT NOT NULL,
            request_sha256               TEXT NOT NULL,
            created_at                   TEXT NOT NULL,
            UNIQUE(document_id, intent_id),
            FOREIGN KEY(
                interaction_contract_id, interaction_contract_version
            ) REFERENCES interaction_contract_definitions(
                contract_id, definition_version
            )
        )
        """,
        """
        CREATE TABLE document_truth_admission_seal_events (
            id                          TEXT PRIMARY KEY,
            document_id                 TEXT NOT NULL REFERENCES documents(id),
            intent_id                   TEXT NOT NULL,
            activation_revision         INTEGER NOT NULL CHECK(activation_revision > 0),
            state                       TEXT NOT NULL CHECK(
                state IN ('pending', 'committed', 'aborted')
            ),
            seal_revision               INTEGER NOT NULL CHECK(seal_revision > 0),
            coordinator_decision_id      TEXT NOT NULL,
            coordinator_decision_sha256  TEXT NOT NULL,
            actor_ref                    TEXT NOT NULL,
            canonical_sha256             TEXT NOT NULL UNIQUE,
            created_at                   TEXT NOT NULL,
            UNIQUE(document_id, seal_revision)
        )
        """,
        """
        CREATE TABLE document_truth_admission_seals_current (
            document_id                 TEXT PRIMARY KEY REFERENCES documents(id),
            intent_id                   TEXT NOT NULL,
            activation_revision         INTEGER NOT NULL CHECK(activation_revision > 0),
            state                       TEXT NOT NULL CHECK(
                state IN ('pending', 'committed', 'aborted')
            ),
            seal_revision               INTEGER NOT NULL CHECK(seal_revision > 0),
            coordinator_decision_id      TEXT NOT NULL,
            coordinator_decision_sha256  TEXT NOT NULL,
            event_id                     TEXT NOT NULL UNIQUE
                REFERENCES document_truth_admission_seal_events(id),
            updated_at                   TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX idx_document_truth_activation_document
        ON document_truth_activation_transitions(document_id, activation_revision)
        """,
        """
        CREATE INDEX idx_document_truth_receipts_document
        ON document_truth_policy_receipts(document_id, created_at, id)
        """,
        """
        CREATE INDEX idx_document_truth_seals_document
        ON document_truth_admission_seal_events(document_id, seal_revision)
        """,
    )
    for statement in statements:
        conn.execute(statement)

    append_only = (
        "interaction_contract_definitions",
        "document_interaction_contract_assignments",
        "document_truth_activation_transitions",
        "document_truth_policy_receipts",
        "document_truth_admission_seal_events",
    )
    for table in append_only:
        conn.execute(
            f"""
            CREATE TRIGGER {table}_append_only_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {table}_append_only_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END
            """
        )

    # Existing Co-work documents had full Truth behavior. Freeze that behavior
    # explicitly so v11 does not silently remove capabilities after upgrade.
    from work_buddy.cowork.truth_activation import backfill_legacy_document_policies

    backfill_legacy_document_policies(conn)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


class _TruthMigrationRunner(MigrationRunner):
    """Migration runner with strict v0 handling and dual version updates."""

    def _infer_baseline_version(self, conn: sqlite3.Connection) -> int:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' "
                "AND name != '_migration_history'"
            )
        }
        if tables:
            names = ", ".join(sorted(tables))
            raise MigrationError(
                f"truth: refusing unversioned partial schema with tables: {names}"
            )
        return 0

    def _apply_one(
        self,
        conn: sqlite3.Connection,
        migration: Migration,
    ) -> None:
        logger.info(
            "%s: applying v%d (%s)",
            self.name,
            migration.version,
            migration.description,
        )
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._apply_one_locked(conn, migration)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            logger.exception(
                "%s: v%d (%s) failed and rolled back",
                self.name,
                migration.version,
                migration.description,
            )
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def _apply_one_locked(
        self,
        conn: sqlite3.Connection,
        migration: Migration,
    ) -> None:
        """Apply one migration inside a caller-owned write transaction."""
        migration.fn(conn)
        if _table_exists(conn, "store_info"):
            conn.execute(
                "UPDATE store_info SET schema_version = ? WHERE schema_version < ?",
                (migration.version, migration.version),
            )
        conn.execute(
            "INSERT INTO _migration_history "
            "(version, description, applied_at, code_hash, hash_format) "
            "VALUES (?, ?, datetime('now'), ?, ?)",
            (
                migration.version,
                migration.description,
                self._hash_callable(migration.fn),
                HASH_FORMAT_CURRENT,
            ),
        )
        self._set_user_version(conn, migration.version)


TRUTH_MIGRATIONS = _TruthMigrationRunner(
    "truth",
    migrations=[
        Migration(1, "initial truth ledger schema", _m001_initial_schema),
        Migration(2, "co-work document surface schema", _m002_document_surface),
        Migration(
            3,
            "co-work document versions and recoverable persistence",
            _m003_cowork_document_foundation,
        ),
        Migration(
            4,
            "recoverable co-work sitting and lifecycle intents",
            _m004_cowork_lifecycle_intents,
        ),
        Migration(
            5,
            "portable co-work verify and co-think ledger",
            _m005_cowork_verify_cothink,
        ),
        Migration(
            6,
            "co-think item lifecycle events",
            _m006_cothink_item_lifecycle,
        ),
        Migration(
            7,
            "portable co-work coordination history",
            _m007_portable_cowork_coordination,
        ),
        Migration(
            8,
            "document provenance attestations and import-source safety",
            _m008_document_provenance_attestations,
        ),
        Migration(
            9,
            "source-backed Truth receipts and outcome-aware provenance",
            _m009_source_backed_truth_provenance,
        ),
        Migration(
            10,
            "automatic direct-entry provenance attribution",
            _m010_direct_entry_provenance_basis,
        ),
        Migration(
            11,
            "per-document interaction contracts and Truth admission",
            _m011_document_truth_activation,
        ),
    ],
)


def current_version(conn: sqlite3.Connection) -> int:
    """Return the SQLite schema version for one open truth store."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def snapshot_store(
    conn: sqlite3.Connection,
    db_path: str | Path,
    version: int,
) -> Path:
    """Back up a store before migration.

    The ``pre-vN`` suffix names the schema version captured in the file.
    A backup named ``store.pre-v1.db`` is therefore the v1 state saved
    immediately before the engine applies v2.
    """
    path = Path(db_path)
    if version < 1:
        raise ValueError("snapshot version must be positive")
    if not path.exists():
        raise FileNotFoundError(path)

    snapshot = path.with_name(f"{path.stem}.pre-v{version}{path.suffix}")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{snapshot.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)

    source = conn
    owns_source = False
    if conn.in_transaction:
        source = sqlite3.connect(str(path), timeout=10)
        owns_source = True
    destination = sqlite3.connect(str(temp_path), timeout=10)
    try:
        source.backup(destination)
        destination.commit()
        destination.close()
        if owns_source:
            source.close()
        os.replace(temp_path, snapshot)
    except Exception:
        destination.close()
        if owns_source:
            source.close()
        temp_path.unlink(missing_ok=True)
        raise
    return snapshot


def _assert_store_info_version(
    conn: sqlite3.Connection,
    version: int,
) -> None:
    if not _table_exists(conn, "store_info"):
        return
    rows = conn.execute("SELECT schema_version FROM store_info").fetchall()
    if not rows:
        return
    stored = {int(row[0]) for row in rows}
    if stored != {version}:
        raise MigrationError(
            "truth: store_info.schema_version does not match PRAGMA user_version"
        )


def migrate(
    conn: sqlite3.Connection,
    db_path: str | Path,
    snapshot: bool = True,
) -> int:
    """Migrate a truth store on open and return its final schema version."""
    if conn.in_transaction:
        raise MigrationError("truth: migrate requires an idle connection")

    initial = current_version(conn)
    target = TRUTH_MIGRATIONS.target_version
    if initial > target:
        raise SchemaVersionTooNew(
            f"truth: DB at v{initial} but this code only knows up to v{target}"
        )

    while True:
        observed = current_version(conn)
        if observed > target:
            raise SchemaVersionTooNew(
                f"truth: DB at v{observed} but this code only knows up to v{target}"
            )
        applied = [
            item for item in TRUTH_MIGRATIONS.migrations if item.version <= observed
        ]
        preflight = _TruthMigrationRunner(TRUTH_MIGRATIONS.name, applied)
        try:
            preflight.run(conn)
            break
        except SchemaVersionTooNew:
            if current_version(conn) > target:
                raise

    _assert_store_info_version(conn, current_version(conn))
    was_fresh = initial == 0
    for migration in TRUTH_MIGRATIONS.migrations:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            locked_version = current_version(conn)
            if locked_version > target:
                raise SchemaVersionTooNew(
                    f"truth: DB at v{locked_version} but this code only "
                    f"knows up to v{target}"
                )
            TRUTH_MIGRATIONS._verify_history_hashes(conn, locked_version)
            if locked_version >= migration.version:
                conn.execute("COMMIT")
                continue
            if snapshot and not was_fresh and locked_version > 0:
                snapshot_store(conn, db_path, locked_version)
            logger.info(
                "%s: applying v%d (%s)",
                TRUTH_MIGRATIONS.name,
                migration.version,
                migration.description,
            )
            TRUTH_MIGRATIONS._apply_one_locked(conn, migration)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            logger.exception(
                "%s: v%d (%s) failed and rolled back",
                TRUTH_MIGRATIONS.name,
                migration.version,
                migration.description,
            )
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

        _assert_store_info_version(conn, current_version(conn))

    final = current_version(conn)
    _assert_store_info_version(conn, final)
    return final
