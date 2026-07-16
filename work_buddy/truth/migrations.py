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

SCHEMA_VERSION = 1

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
