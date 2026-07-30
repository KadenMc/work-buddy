"""Versioned SQLite schema for targeted truth stores."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from work_buddy.logging_config import get_logger
from work_buddy.storage.migrations import (
    HASH_FORMAT_CURRENT,
    Migration,
    MigrationError,
    MigrationRunner,
    SchemaVersionTooNew,
)


logger = get_logger(__name__)

SCHEMA_VERSION = 7

# Redacted spans retain their immutable identity/hash but not their quote or
# quote context.  Keep the selector valid JSON (and valid for the existing
# selector parser) so recovery exports and integrity scans can still process
# the row without preserving any source text or source-text length.
REDACTED_SELECTOR_JSON = (
    '[{"exact":"[redacted]","prefix":"","suffix":"","type":"TextQuoteSelector"}]'
)


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
