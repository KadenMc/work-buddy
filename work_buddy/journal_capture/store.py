"""SQLite authority for Journal captures, entries, and domain effects."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
    source_foundation_read_only,
)
from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    EffectState,
    JournalCapture,
    JournalCaptureError,
    JournalCaptureConflict,
    JournalDocumentBinding,
    JournalDocumentUsageTransition,
    JournalEffect,
    JournalEntry,
    JournalMigrationComparison,
    JournalMigrationRecord,
    JournalMigrationState,
    ProcessingState,
    ProjectionState,
)
from work_buddy.paths import resolve


_SCHEMA_VERSION = 6


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else None


class JournalCaptureStore:
    """Domain store kept separate from Sources and legacy Markdown.

    Cross-database delivery is idempotent by ``source_effect_id`` and
    ``submission_id``.  No exact source bytes are placed in the capture row;
    domain composition is stored only after Journal accepts the command.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        self.path = (
            Path(path).expanduser().resolve()
            if path is not None
            else resolve("db/journal-capture").expanduser().resolve()
        )
        self.read_only = bool(read_only)
        if self.read_only or source_foundation_read_only():
            if not self.path.is_file():
                raise JournalCaptureError(
                    "journal_capture_state_missing_during_restore_reconciliation"
                )
            self._validate_existing()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        read_only = self.read_only or source_foundation_read_only()
        conn = sqlite3.connect(
            f"file:{self.path}?mode=ro" if read_only else self.path,
            timeout=10.0,
            uri=read_only,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise JournalCaptureError("journal_capture_store_is_read_only")
        require_source_foundation_writable("journal_capture.write")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
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
                    CHECK (processing_status IN ('not_requested','pending','running','succeeded','failed'))
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
                    CHECK (processing_status IN ('not_requested','pending','running','succeeded','failed')),
                    CHECK (projection_state IN ('pending','prepared','committed','failed','paused_diverged'))
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
            row = conn.execute(
                "SELECT value FROM journal_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO journal_meta(key,value) VALUES('schema_version',?)",
                    (str(_SCHEMA_VERSION),),
                )
            else:
                version = int(row["value"])
                if version < 1 or version > _SCHEMA_VERSION:
                    raise RuntimeError("unsupported_journal_capture_schema")
            if row is not None and version == 1:
                columns = {
                    str(item[1])
                    for item in conn.execute("PRAGMA table_info(journal_captures)").fetchall()
                }
                if "source_usage_id" not in columns:
                    conn.execute(
                        "ALTER TABLE journal_captures ADD COLUMN source_usage_id TEXT"
                    )
                version = 2
            if row is not None and version < 4:
                columns = {
                    str(item[1])
                    for item in conn.execute(
                        "PRAGMA table_info(journal_document_bindings)"
                    ).fetchall()
                }
                additions = (
                    (
                        "source_use_kind",
                        "TEXT NOT NULL DEFAULT 'exact_insertion'",
                    ),
                    (
                        "source_disclosure_kind",
                        "TEXT NOT NULL DEFAULT 'exact_readable_copy'",
                    ),
                    (
                        "source_redaction_policy",
                        "TEXT NOT NULL DEFAULT 'scrub'",
                    ),
                    (
                        "source_maintenance_state",
                        "TEXT NOT NULL DEFAULT 'clean'",
                    ),
                    (
                        "source_maintenance_json",
                        "TEXT NOT NULL DEFAULT '{}'",
                    ),
                )
                for name, declaration in additions:
                    if name not in columns:
                        conn.execute(
                            f"ALTER TABLE journal_document_bindings "
                            f"ADD COLUMN {name} {declaration}"
                        )
                version = 4
            if row is not None and version < 5:
                # v5 consists only of the idempotent migration/evidence tables
                # created above; no legacy row rewrite is required.
                version = 5
            if row is not None and version < 6:
                columns = {
                    str(item[1])
                    for item in conn.execute(
                        "PRAGMA table_info(journal_content_migrations)"
                    ).fetchall()
                }
                if "structural_parity" not in columns:
                    conn.execute(
                        "ALTER TABLE journal_content_migrations "
                        "ADD COLUMN structural_parity INTEGER"
                    )
                version = 6
            if row is not None and version < _SCHEMA_VERSION:
                raise RuntimeError("unsupported_journal_capture_schema")
            if row is not None:
                # New tables are created idempotently above. Advancing only
                # after all additive migrations complete keeps restart
                # recovery deterministic across every supported legacy store.
                conn.execute(
                    "UPDATE journal_meta SET value=? WHERE key='schema_version'",
                    (str(_SCHEMA_VERSION),),
                )

    def _validate_existing(self) -> None:
        try:
            with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchall()
                version = conn.execute(
                    "SELECT value FROM journal_meta WHERE key='schema_version'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise JournalCaptureError(
                "journal_capture_state_invalid_during_restore_reconciliation"
            ) from exc
        try:
            schema_version = None if version is None else int(version[0])
        except (TypeError, ValueError):
            schema_version = None
        if integrity != [("ok",)] or schema_version != _SCHEMA_VERSION:
            raise JournalCaptureError(
                "journal_capture_state_invalid_during_restore_reconciliation"
            )

    def create_capture(
        self,
        *,
        client_mutation_id: str,
        request_sha256: str,
        source_ref: str,
        representation_id: str,
        submission_id: str,
        command_id: str,
        source_effect_id: str,
        source_usage_id: str | None = None,
        day_id: str,
        requested_target: CaptureTarget,
        mode: CaptureMode,
        input_mode: str,
        stated_at: str | None,
        submitted_at: str,
        authorization_fingerprint: str,
        authorization_expires_at: str | None = None,
    ) -> JournalCapture:
        now = _utc_now()
        capture_id = uuid.uuid4().hex
        processing = (
            ProcessingState.PENDING
            if mode is CaptureMode.SMART or requested_target is CaptureTarget.AUTO
            else ProcessingState.NOT_REQUESTED
        )
        effect_types = ["materialize"]
        if requested_target is CaptureTarget.AUTO:
            effect_types.insert(0, "auto_route")
        elif mode is CaptureMode.SMART:
            effect_types.append("smart_annotate")
        with self.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM journal_captures WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
            if prior is not None:
                if prior["request_sha256"] != request_sha256:
                    raise JournalCaptureConflict(
                        "That capture key was already used for different input."
                    )
                return self._capture(prior)
            by_submission = conn.execute(
                "SELECT * FROM journal_captures WHERE submission_id=? OR source_effect_id=?",
                (submission_id, source_effect_id),
            ).fetchone()
            if by_submission is not None:
                if by_submission["request_sha256"] != request_sha256:
                    raise JournalCaptureConflict(
                        "The source command is already bound to a different capture."
                    )
                return self._capture(by_submission)
            conn.execute(
                """
                INSERT INTO journal_captures(
                    capture_id,client_mutation_id,request_sha256,source_ref,
                    representation_id,submission_id,command_id,source_effect_id,source_usage_id,
                    day_id,requested_target,mode,input_mode,stated_at,submitted_at,
                    processing_status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    capture_id,
                    client_mutation_id,
                    request_sha256,
                    source_ref,
                    representation_id,
                    submission_id,
                    command_id,
                    source_effect_id,
                    source_usage_id,
                    day_id,
                    requested_target.value,
                    mode.value,
                    input_mode,
                    stated_at,
                    submitted_at,
                    processing.value,
                    now,
                    now,
                ),
            )
            for effect_type in effect_types:
                effect_id = (
                    source_effect_id
                    if effect_type == "materialize" and len(effect_types) == 1
                    else hashlib.sha256(
                        f"{source_effect_id}:{effect_type}".encode("utf-8")
                    ).hexdigest()[:32]
                )
                conn.execute(
                    """
                    INSERT INTO journal_effects(
                        effect_id,capture_id,effect_type,state,
                        authorization_fingerprint,authorization_expires_at,
                        created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        effect_id,
                        capture_id,
                        effect_type,
                        EffectState.PENDING.value,
                        authorization_fingerprint,
                        authorization_expires_at,
                        now,
                        now,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM journal_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            assert row is not None
            return self._capture(row)

    def get_capture(self, capture_id: str) -> JournalCapture | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
        return None if row is None else self._capture(row)

    def get_capture_by_mutation(self, client_mutation_id: str) -> JournalCapture | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_captures WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
        return None if row is None else self._capture(row)

    def get_capture_by_source_effect(self, source_effect_id: str) -> JournalCapture | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_captures WHERE source_effect_id=?",
                (source_effect_id,),
            ).fetchone()
        return None if row is None else self._capture(row)

    def list_captures(self, day_id: str, *, limit: int = 20) -> list[JournalCapture]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM journal_captures
                   WHERE day_id=? ORDER BY submitted_at DESC LIMIT ?""",
                (day_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._capture(row) for row in rows]

    def ensure_entry(
        self,
        *,
        capture_id: str,
        entry_kind: CaptureTarget,
        markdown: str,
        content_sha256: str,
        projection_marker: str,
        created_at: str,
    ) -> JournalEntry:
        if entry_kind is CaptureTarget.AUTO:
            raise ValueError("auto is not an entry kind")
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM journal_entries WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["entry_kind"] != entry_kind.value
                    or existing["content_sha256"] != content_sha256
                ):
                    raise JournalCaptureConflict(
                        "The capture is already materialized differently."
                    )
                return self._entry(existing)
            capture = conn.execute(
                "SELECT * FROM journal_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if capture is None:
                raise KeyError("journal_capture_not_found")
            entry_id = hashlib.sha256(f"journal-entry:{capture_id}".encode()).hexdigest()[:32]
            now = _utc_now()
            conn.execute(
                """
                INSERT INTO journal_entries(
                    entry_id,capture_id,day_id,entry_kind,source_ref,
                    content_sha256,markdown,created_at,updated_at,
                    processing_status,projection_marker
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    entry_id,
                    capture_id,
                    capture["day_id"],
                    entry_kind.value,
                    capture["source_ref"],
                    content_sha256,
                    markdown,
                    created_at,
                    now,
                    capture["processing_status"],
                    projection_marker,
                ),
            )
            conn.execute(
                """UPDATE journal_captures
                   SET resolved_target=?,entry_id=?,revision=revision+1,updated_at=?
                   WHERE capture_id=?""",
                (entry_kind.value, entry_id, now, capture_id),
            )
            row = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            assert row is not None
            return self._entry(row)

    def get_entry(self, entry_id: str) -> JournalEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
        return None if row is None else self._entry(row)

    def get_document_binding(self, entry_id: str) -> JournalDocumentBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
        return None if row is None else self._document_binding(row)

    def list_document_bindings(self) -> tuple[JournalDocumentBinding, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_document_bindings "
                "WHERE state != 'retired' ORDER BY entry_id"
            ).fetchall()
        return tuple(self._document_binding(row) for row in rows)

    def get_document_binding_by_source_consumer(
        self, source_consumer_id: str
    ) -> JournalDocumentBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE source_consumer_id=?",
                (source_consumer_id,),
            ).fetchone()
        return None if row is None else self._document_binding(row)

    def record_document_binding(
        self,
        *,
        entry_id: str,
        binding_id: str,
        store_id: str,
        document_id: str,
        change_id: str,
        source_consumer_id: str,
        source_usage_id: str,
        source_use_kind: str = "exact_insertion",
        source_disclosure_kind: str = "exact_readable_copy",
        source_redaction_policy: str = "scrub",
        cowork_href: str,
        content_authority_epoch: int,
        entry_version: int,
        inspection: Mapping[str, Any],
        state: str = "current",
    ) -> JournalDocumentBinding:
        """Persist the Journal-side navigation/authority mirror idempotently."""

        encoded = _json(inspection)
        assert encoded is not None
        now = _utc_now()
        with self.transaction() as conn:
            entry = conn.execute(
                "SELECT entry_id FROM journal_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if entry is None:
                raise KeyError("journal_entry_not_found")
            prior = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if prior is not None:
                immutable = (
                    prior["binding_id"],
                    prior["store_id"],
                    prior["document_id"],
                    prior["source_consumer_id"],
                    prior["source_usage_id"],
                    prior["source_use_kind"],
                    prior["source_disclosure_kind"],
                    prior["source_redaction_policy"],
                )
                if immutable != (
                    binding_id,
                    store_id,
                    document_id,
                    source_consumer_id,
                    source_usage_id,
                    source_use_kind,
                    source_disclosure_kind,
                    source_redaction_policy,
                ):
                    raise JournalCaptureConflict(
                        "That Running Note is already bound to another document."
                    )
                conn.execute(
                    "UPDATE journal_document_bindings SET change_id=?,cowork_href=?,"
                    "content_authority_epoch=?,entry_version=?,inspection_json=?,"
                    "state=?,updated_at=? WHERE entry_id=?",
                    (
                        change_id,
                        cowork_href,
                        content_authority_epoch,
                        entry_version,
                        encoded,
                        state,
                        now,
                        entry_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO journal_document_bindings "
                    "(entry_id,binding_id,store_id,document_id,change_id,"
                    "source_consumer_id,source_usage_id,source_use_kind,"
                    "source_disclosure_kind,source_redaction_policy,"
                    "source_maintenance_state,source_maintenance_json,cowork_href,"
                    "content_authority_epoch,entry_version,inspection_json,state,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,"
                    "'clean','{}',?,?,?,?,?,?,?)",
                    (
                        entry_id,
                        binding_id,
                        store_id,
                        document_id,
                        change_id,
                        source_consumer_id,
                        source_usage_id,
                        source_use_kind,
                        source_disclosure_kind,
                        source_redaction_policy,
                        cowork_href,
                        content_authority_epoch,
                        entry_version,
                        encoded,
                        state,
                        now,
                        now,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?", (entry_id,)
            ).fetchone()
            assert row is not None
            return self._document_binding(row)

    def transition_document_source_usage(
        self,
        *,
        entry_id: str,
        binding_id: str,
        change_id: str,
        expected_prior_usage_id: str,
        next_usage_id: str,
        next_use_kind: str,
        next_disclosure_kind: str,
        next_redaction_policy: str,
    ) -> tuple[JournalDocumentBinding, JournalDocumentUsageTransition]:
        """Atomically publish a new active dependency and its recovery receipt."""

        transition_id = hashlib.sha256(
            (
                "journal-document-source-transition/v1\0"
                + binding_id
                + "\0"
                + change_id
            ).encode("utf-8")
        ).hexdigest()[:32]
        now = _utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if row is None or row["binding_id"] != binding_id:
                raise JournalCaptureConflict(
                    "That Running Note source dependency is no longer current."
                )
            prior = conn.execute(
                "SELECT * FROM journal_document_usage_transitions WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            immutable = (
                transition_id,
                entry_id,
                binding_id,
                change_id,
                expected_prior_usage_id,
                next_usage_id,
                next_use_kind,
                next_disclosure_kind,
                next_redaction_policy,
            )
            if prior is not None:
                actual = (
                    prior["transition_id"],
                    prior["entry_id"],
                    prior["binding_id"],
                    prior["change_id"],
                    prior["prior_usage_id"],
                    prior["next_usage_id"],
                    prior["next_use_kind"],
                    prior["next_disclosure_kind"],
                    prior["next_redaction_policy"],
                )
                if actual != immutable:
                    raise JournalCaptureConflict(
                        "That Running Note source dependency changed concurrently."
                    )
            active_usage = str(row["source_usage_id"])
            if active_usage not in {expected_prior_usage_id, next_usage_id}:
                raise JournalCaptureConflict(
                    "That Running Note source dependency changed concurrently."
                )
            if prior is None:
                conn.execute(
                    "INSERT INTO journal_document_usage_transitions "
                    "(transition_id,entry_id,binding_id,change_id,prior_usage_id,"
                    "next_usage_id,next_use_kind,next_disclosure_kind,"
                    "next_redaction_policy,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'mirror_updated',?,?)",
                    (*immutable, now, now),
                )
            if active_usage == expected_prior_usage_id:
                conn.execute(
                    "UPDATE journal_document_bindings SET source_usage_id=?,"
                    "source_use_kind=?,source_disclosure_kind=?,source_redaction_policy=?,"
                    "source_maintenance_state='clean',source_maintenance_json='{}',"
                    "updated_at=? WHERE entry_id=?",
                    (
                        next_usage_id,
                        next_use_kind,
                        next_disclosure_kind,
                        next_redaction_policy,
                        now,
                        entry_id,
                    ),
                )
            binding = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?", (entry_id,)
            ).fetchone()
            transition = conn.execute(
                "SELECT * FROM journal_document_usage_transitions WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            assert binding is not None and transition is not None
            return self._document_binding(binding), self._usage_transition(transition)

    def complete_document_source_usage_transition(
        self, transition_id: str
    ) -> JournalDocumentUsageTransition:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_usage_transitions WHERE transition_id=?",
                (transition_id,),
            ).fetchone()
            if row is None:
                raise KeyError("journal_document_usage_transition_not_found")
            if row["state"] != "complete":
                conn.execute(
                    "UPDATE journal_document_usage_transitions SET state='complete',"
                    "updated_at=? WHERE transition_id=?",
                    (_utc_now(), transition_id),
                )
            updated = conn.execute(
                "SELECT * FROM journal_document_usage_transitions WHERE transition_id=?",
                (transition_id,),
            ).fetchone()
            assert updated is not None
            return self._usage_transition(updated)

    def get_document_source_usage_transition(
        self, entry_id: str
    ) -> JournalDocumentUsageTransition | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_usage_transitions WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
        return None if row is None else self._usage_transition(row)

    def mark_document_source_review_required(
        self,
        entry_id: str,
        *,
        details: Mapping[str, Any],
    ) -> JournalDocumentBinding:
        """Persist content-free attention without resolving the Source effect."""

        encoded = _json(details)
        assert encoded is not None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT entry_id FROM journal_document_bindings WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise KeyError("journal_document_binding_not_found")
            conn.execute(
                "UPDATE journal_document_bindings SET "
                "source_maintenance_state='review_required',"
                "source_maintenance_json=?,updated_at=? WHERE entry_id=?",
                (encoded, _utc_now(), entry_id),
            )
            updated = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?", (entry_id,)
            ).fetchone()
            assert updated is not None
            return self._document_binding(updated)

    def retire_document_binding(self, entry_id: str) -> JournalDocumentBinding | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if row is None:
                return None
            if row["state"] != "retired":
                conn.execute(
                    "UPDATE journal_document_bindings SET state='retired',updated_at=? "
                    "WHERE entry_id=?",
                    (_utc_now(), entry_id),
                )
            updated = conn.execute(
                "SELECT * FROM journal_document_bindings WHERE entry_id=?", (entry_id,)
            ).fetchone()
            assert updated is not None
            return self._document_binding(updated)

    def list_running_notes(self, day_id: str) -> list[JournalEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM journal_entries
                   WHERE day_id=? AND entry_kind='running_notes'
                   AND resolution_state NOT IN ('deleted','redacted')
                   ORDER BY created_at DESC""",
                (day_id,),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def list_log_entries(self, day_id: str) -> list[JournalEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM journal_entries
                   WHERE day_id=? AND entry_kind='log'
                   AND resolution_state NOT IN ('deleted','redacted')
                   ORDER BY created_at""",
                (day_id,),
            ).fetchall()
        return [self._entry(row) for row in rows]

    # ------------------------------------------------------------------
    # Journal prose migration.  Canonical authority/epoch lives in the
    # document-kernel binding; these rows are a content-free recovery mirror.
    # ------------------------------------------------------------------

    def record_migration_selection(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        day_id: str,
        marker_id: str,
        selection_start: int | None,
        selection_end: int | None,
        selected_file_sha256: str,
        selected_section_sha256: str,
    ) -> JournalMigrationRecord:
        if entity_kind not in {"running_note", "logical_day_log"}:
            raise ValueError("invalid Journal migration entity kind")
        if (selection_start is None) != (selection_end is None):
            raise ValueError("Journal selection boundaries must be paired")
        now = _utc_now()
        with self.transaction() as conn:
            if entity_kind == "running_note":
                prior_selection = conn.execute(
                    "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                    "AND day_id=? AND selection_start IS ? AND selection_end IS ? "
                    "AND selected_section_sha256=?",
                    (
                        entity_kind,
                        day_id,
                        selection_start,
                        selection_end,
                        selected_section_sha256,
                    ),
                ).fetchone()
                if prior_selection is not None:
                    return self._migration(prior_selection)
            row = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            immutable = (
                day_id,
                marker_id,
                selection_start,
                selection_end,
                selected_file_sha256,
                selected_section_sha256,
            )
            if row is None:
                conn.execute(
                    "INSERT INTO journal_content_migrations "
                    "(entity_kind,entity_id,day_id,marker_id,selection_start,"
                    "selection_end,selected_file_sha256,selected_section_sha256,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity_kind,
                        entity_id,
                        *immutable,
                        now,
                        now,
                    ),
                )
            else:
                actual = (
                    str(row["day_id"]),
                    str(row["marker_id"]),
                    row["selection_start"],
                    row["selection_end"],
                    str(row["selected_file_sha256"]),
                    str(row["selected_section_sha256"]),
                )
                if actual != immutable:
                    raise JournalCaptureConflict(
                        "That Journal selection changed after identity assignment."
                    )
            refreshed = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            assert refreshed is not None
            return self._migration(refreshed)

    def record_migration_shadow(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        source_ref: str,
        representation_id: str,
        source_content_sha256: str,
        binding_id: str,
        store_id: str,
        document_id: str,
        byte_parity: bool,
        normalized_parity: bool,
        structural_parity: bool,
        operation_id: str,
    ) -> JournalMigrationRecord:
        comparison = (
            JournalMigrationComparison.PARITY
            if byte_parity or normalized_parity or structural_parity
            else JournalMigrationComparison.MISMATCH
        )
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            if row is None:
                raise KeyError("journal_migration_selection_not_found")
            if row["source_content_sha256"] not in {None, source_content_sha256}:
                raise JournalCaptureConflict(
                    "That Journal shadow import no longer matches its selection."
                )
            conn.execute(
                "UPDATE journal_content_migrations SET source_ref=?,"
                "representation_id=?,source_content_sha256=?,binding_id=?,store_id=?,"
                "document_id=?,comparison_state=?,byte_parity=?,normalized_parity=?,"
                "structural_parity=?,"
                "mirrored_state='shadow_imported',operation_id=?,error_code=NULL,"
                "updated_at=? WHERE entity_kind=? AND entity_id=?",
                (
                    source_ref,
                    representation_id,
                    source_content_sha256,
                    binding_id,
                    store_id,
                    document_id,
                    comparison.value,
                    int(byte_parity),
                    int(normalized_parity),
                    int(structural_parity),
                    operation_id,
                    _utc_now(),
                    entity_kind,
                    entity_id,
                ),
            )
            refreshed = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            assert refreshed is not None
            return self._migration(refreshed)

    def mirror_migration_authority(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        state: JournalMigrationState,
        authority_epoch: int,
        rollback_deadline: str | None,
        projection_state: str,
        divergence_source_ref: str | None = None,
        operation_id: str | None = None,
        error_code: str | None = None,
    ) -> JournalMigrationRecord:
        if projection_state not in {
            "none",
            "pending",
            "committed",
            "paused_diverged",
            "failed",
        }:
            raise ValueError("invalid Journal migration projection state")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT mirrored_authority_epoch FROM journal_content_migrations "
                "WHERE entity_kind=? AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            if row is None:
                raise KeyError("journal_migration_not_found")
            if authority_epoch < int(row["mirrored_authority_epoch"]):
                raise JournalCaptureConflict("The Journal authority epoch moved backwards.")
            conn.execute(
                "UPDATE journal_content_migrations SET mirrored_state=?,"
                "mirrored_authority_epoch=?,rollback_deadline=?,projection_state=?,"
                "divergence_source_ref=?,operation_id=COALESCE(?,operation_id),"
                "error_code=?,updated_at=? WHERE entity_kind=? AND entity_id=?",
                (
                    state.value,
                    authority_epoch,
                    rollback_deadline,
                    projection_state,
                    divergence_source_ref,
                    operation_id,
                    error_code,
                    _utc_now(),
                    entity_kind,
                    entity_id,
                ),
            )
            refreshed = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
            assert refreshed is not None
            return self._migration(refreshed)

    def get_migration(
        self, entity_kind: str, entity_id: str
    ) -> JournalMigrationRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE entity_kind=? "
                "AND entity_id=?",
                (entity_kind, entity_id),
            ).fetchone()
        return None if row is None else self._migration(row)

    def migrations_for_day(self, day_id: str) -> tuple[JournalMigrationRecord, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_content_migrations WHERE day_id=? "
                "ORDER BY entity_kind,entity_id",
                (day_id,),
            ).fetchall()
        return tuple(self._migration(row) for row in rows)

    def list_migrations(self) -> tuple[JournalMigrationRecord, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_content_migrations "
                "ORDER BY day_id,entity_kind,entity_id"
            ).fetchall()
        return tuple(self._migration(row) for row in rows)

    def begin_migration_operation(
        self,
        *,
        action: str,
        entity_kind: str,
        entity_id: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> tuple[str, str]:
        operation_id = hashlib.sha256(
            f"journal-migration\0{idempotency_key}".encode("utf-8")
        ).hexdigest()[:32]
        now = _utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_migration_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO journal_migration_operations VALUES(?,?,?,?,?,?,'prepared',NULL,?,?)",
                    (
                        operation_id,
                        idempotency_key,
                        action,
                        entity_kind,
                        entity_id,
                        request_sha256,
                        now,
                        now,
                    ),
                )
                return operation_id, "prepared"
            if (
                row["operation_id"] != operation_id
                or row["action"] != action
                or row["entity_kind"] != entity_kind
                or row["entity_id"] != entity_id
                or row["request_sha256"] != request_sha256
            ):
                raise JournalCaptureConflict("Journal migration idempotency conflict.")
            return str(row["operation_id"]), str(row["state"])

    def advance_migration_operation(
        self, operation_id: str, *, state: str, error_code: str | None = None
    ) -> None:
        if state not in {
            "prepared",
            "document_committed",
            "epoch_committed",
            "projection_committed",
            "completed",
            "recoverable",
            "paused_diverged",
        }:
            raise ValueError("invalid Journal migration operation state")
        with self.transaction() as conn:
            found = conn.execute(
                "SELECT operation_id FROM journal_migration_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if found is None:
                raise KeyError("journal_migration_operation_not_found")
            conn.execute(
                "UPDATE journal_migration_operations SET state=?,error_code=?,updated_at=? "
                "WHERE operation_id=?",
                (state, error_code, _utc_now(), operation_id),
            )

    def recoverable_migration_operations(self) -> tuple[Mapping[str, Any], ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_migration_operations WHERE state NOT IN "
                "('completed','paused_diverged') ORDER BY created_at,operation_id"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def record_exit_evidence(
        self,
        *,
        inventory_sha256: str,
        callsite_inventory_sha256: str,
        authority_summary: Mapping[str, Any],
    ) -> str:
        payload = _json(authority_summary)
        assert payload is not None
        receipt_id = hashlib.sha256(
            (
                "journal-exit-evidence/v1\0"
                + inventory_sha256
                + "\0"
                + callsite_inventory_sha256
                + "\0"
                + payload
            ).encode("utf-8")
        ).hexdigest()[:32]
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO journal_exit_evidence VALUES(?,?,?,?,?)",
                (
                    receipt_id,
                    inventory_sha256,
                    callsite_inventory_sha256,
                    payload,
                    _utc_now(),
                ),
            )
        return receipt_id

    def latest_exit_evidence(
        self,
        *,
        expected_inventory_sha256: str | None = None,
        expected_callsite_inventory_sha256: str | None = None,
    ) -> Mapping[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM journal_exit_evidence ORDER BY created_at DESC,receipt_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        if (
            expected_inventory_sha256 is not None
            and row["inventory_sha256"] != expected_inventory_sha256
        ):
            return None
        if (
            expected_callsite_inventory_sha256 is not None
            and row["callsite_inventory_sha256"]
            != expected_callsite_inventory_sha256
        ):
            return None
        return {
            "receipt_id": str(row["receipt_id"]),
            "inventory_sha256": str(row["inventory_sha256"]),
            "callsite_inventory_sha256": str(row["callsite_inventory_sha256"]),
            "authority_summary": json.loads(str(row["authority_summary_json"])),
            "created_at": str(row["created_at"]),
        }

    def mark_projection_prepared(
        self, entry_id: str, *, base_sha256: str
    ) -> JournalEntry:
        return self._update_projection(
            entry_id,
            state=ProjectionState.PREPARED,
            base_sha256=base_sha256,
            result_sha256=None,
        )

    def mark_projection_committed(
        self, entry_id: str, *, base_sha256: str, result_sha256: str
    ) -> JournalEntry:
        entry = self._update_projection(
            entry_id,
            state=ProjectionState.COMMITTED,
            base_sha256=base_sha256,
            result_sha256=result_sha256,
        )
        self.finish_effect(entry.capture_id, "materialize", succeeded=True)
        return entry

    def mark_projection_failed(self, entry_id: str, *, error_code: str) -> JournalEntry:
        entry = self._update_projection(
            entry_id,
            state=ProjectionState.FAILED,
            base_sha256=None,
            result_sha256=None,
        )
        self.finish_effect(
            entry.capture_id,
            "materialize",
            succeeded=False,
            error_code=error_code,
        )
        return entry

    def _update_projection(
        self,
        entry_id: str,
        *,
        state: ProjectionState,
        base_sha256: str | None,
        result_sha256: str | None,
    ) -> JournalEntry:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if existing is None:
                raise KeyError("journal_entry_not_found")
            conn.execute(
                """UPDATE journal_entries SET projection_state=?,
                   projection_base_sha256=COALESCE(?,projection_base_sha256),
                   projection_result_sha256=COALESCE(?,projection_result_sha256),
                   updated_at=? WHERE entry_id=?""",
                (state.value, base_sha256, result_sha256, _utc_now(), entry_id),
            )
            row = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            assert row is not None
            return self._entry(row)

    def lease_effect(
        self,
        effect_id: str,
        *,
        owner: str,
        lease_seconds: int = 60,
    ) -> JournalEffect | None:
        now = datetime.now(UTC)
        now_s = now.isoformat()
        expires = (now + timedelta(seconds=max(5, lease_seconds))).isoformat()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_effects WHERE effect_id=?", (effect_id,)
            ).fetchone()
            if row is None or row["state"] == EffectState.SUCCEEDED.value:
                return None
            if (
                row["authorization_expires_at"] is not None
                and row["authorization_expires_at"] <= now_s
            ):
                conn.execute(
                    """UPDATE journal_effects SET state='paused',
                       error_code='journal_authorization_expired',
                       lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                       WHERE effect_id=?""",
                    (now_s, effect_id),
                )
                return None
            if row["state"] == EffectState.PAUSED.value:
                return None
            if (
                row["state"] == EffectState.RUNNING.value
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > now_s
            ):
                if row["lease_owner"] != owner:
                    return None
                return self._effect(row)
            conn.execute(
                """UPDATE journal_effects SET state='running',attempts=attempts+1,
                   lease_owner=?,lease_expires_at=?,error_code=NULL,updated_at=?
                   WHERE effect_id=?""",
                (owner, expires, now_s, effect_id),
            )
            updated = conn.execute(
                "SELECT * FROM journal_effects WHERE effect_id=?", (effect_id,)
            ).fetchone()
            assert updated is not None
            return self._effect(updated)

    def reauthorize_effect(
        self,
        capture_id: str,
        effect_type: str,
        *,
        authorization_fingerprint: str,
        authorization_expires_at: str | None,
    ) -> JournalEffect:
        """Bind a fresh explicit authorization to one unsettled effect.

        Successful work is immutable. A still-live lease cannot be stolen by
        a retry gesture; an expired lease may be safely returned to pending.
        """

        now = _utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_effects WHERE capture_id=? AND effect_type=?",
                (capture_id, effect_type),
            ).fetchone()
            if row is None:
                raise KeyError("journal_effect_not_found")
            if row["state"] == EffectState.SUCCEEDED.value:
                return self._effect(row)
            if (
                row["state"] == EffectState.RUNNING.value
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > now
            ):
                raise JournalCaptureConflict(
                    "That Journal action is already running."
                )
            conn.execute(
                """UPDATE journal_effects SET state='pending',
                   authorization_fingerprint=?,authorization_expires_at=?,
                   lease_owner=NULL,lease_expires_at=NULL,error_code=NULL,updated_at=?
                   WHERE effect_id=?""",
                (
                    authorization_fingerprint,
                    authorization_expires_at,
                    now,
                    row["effect_id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM journal_effects WHERE effect_id=?", (row["effect_id"],)
            ).fetchone()
            assert updated is not None
            return self._effect(updated)

    def effects_for_capture(self, capture_id: str) -> list[JournalEffect]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_effects WHERE capture_id=? ORDER BY created_at,effect_type",
                (capture_id,),
            ).fetchall()
        return [self._effect(row) for row in rows]

    def pending_effects(self, *, limit: int = 20) -> list[JournalEffect]:
        now = _utc_now()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM journal_effects
                   WHERE state IN ('pending','failed')
                      OR (state='running' AND lease_expires_at <= ?)
                   ORDER BY updated_at LIMIT ?""",
                (now, max(1, min(limit, 100))),
            ).fetchall()
        return [self._effect(row) for row in rows]

    def finish_effect(
        self,
        capture_id: str,
        effect_type: str,
        *,
        succeeded: bool,
        error_code: str | None = None,
    ) -> None:
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE journal_effects SET state=?,error_code=?,
                   lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE capture_id=? AND effect_type=?""",
                (
                    EffectState.SUCCEEDED.value if succeeded else EffectState.FAILED.value,
                    error_code,
                    now,
                    capture_id,
                    effect_type,
                ),
            )

    def set_processing(
        self,
        capture_id: str,
        *,
        status: ProcessingState,
        annotation: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        resolved_target: CaptureTarget | None = None,
    ) -> JournalCapture:
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE journal_captures SET processing_status=?,
                   annotation_json=?,processing_error_code=?,
                   resolved_target=COALESCE(?,resolved_target),
                   revision=revision+1,updated_at=? WHERE capture_id=?""",
                (
                    status.value,
                    _json(annotation),
                    error_code,
                    resolved_target.value if resolved_target is not None else None,
                    now,
                    capture_id,
                ),
            )
            conn.execute(
                """UPDATE journal_entries SET processing_status=?,annotation_json=?,
                   processing_error_code=?,updated_at=? WHERE capture_id=?""",
                (status.value, _json(annotation), error_code, now, capture_id),
            )
            row = conn.execute(
                "SELECT * FROM journal_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if row is None:
                raise KeyError("journal_capture_not_found")
            return self._capture(row)

    def mark_source_redacted(
        self,
        *,
        source_effect_id: str,
        source_usage_id: str,
        source_ref: str,
        redaction_event_id: str,
        redaction_epoch: int,
        result_sha256: str,
        projection_state: str = "committed",
    ) -> JournalEntry | None:
        """Scrub a managed source copy and persist a content-free receipt.

        The Markdown adapter removes the readable block before this method is
        called.  Replays are idempotent by the source redaction event and usage
        IDs; no prior readable text is copied into the receipt.
        """

        if projection_state not in {"committed", "paused_diverged"}:
            raise ValueError("invalid_journal_redaction_projection_state")
        now = _utc_now()
        with self.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM journal_source_redactions WHERE redaction_event_id=?",
                (redaction_event_id,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["source_effect_id"] != source_effect_id
                    or prior["source_usage_id"] != source_usage_id
                    or prior["source_ref"] != source_ref
                    or int(prior["redaction_epoch"]) != redaction_epoch
                    or prior["result_sha256"] != result_sha256
                ):
                    raise JournalCaptureConflict(
                        "That source redaction is already bound differently."
                    )
                if prior["entry_id"] is None:
                    return None
                row = conn.execute(
                    "SELECT * FROM journal_entries WHERE entry_id=?",
                    (prior["entry_id"],),
                ).fetchone()
                return None if row is None else self._entry(row)

            capture = conn.execute(
                "SELECT * FROM journal_captures WHERE source_effect_id=?",
                (source_effect_id,),
            ).fetchone()
            if capture is not None:
                if capture["source_ref"] != source_ref:
                    raise JournalCaptureConflict(
                        "The source redaction does not match the Journal capture."
                    )
                bound_usage = capture["source_usage_id"]
                if bound_usage is not None and bound_usage != source_usage_id:
                    raise JournalCaptureConflict(
                        "The source redaction does not match the managed copy."
                    )
            entry = None
            if capture is not None and capture["entry_id"] is not None:
                entry = conn.execute(
                    "SELECT * FROM journal_entries WHERE entry_id=?",
                    (capture["entry_id"],),
                ).fetchone()
                if entry is None:
                    raise KeyError("journal_entry_not_found")
                conn.execute(
                    "UPDATE journal_entries SET markdown='[redacted]', "
                    "resolution_state='redacted', projection_state=?, "
                    "projection_result_sha256=?, version=version+1, updated_at=? "
                    "WHERE entry_id=?",
                    (projection_state, result_sha256, now, entry["entry_id"]),
                )
                conn.execute(
                    "UPDATE journal_captures SET revision=revision+1,updated_at=? "
                    "WHERE capture_id=?",
                    (now, capture["capture_id"]),
                )

            conn.execute(
                "INSERT INTO journal_source_redactions "
                "(redaction_event_id,source_effect_id,source_usage_id,source_ref,"
                "capture_id,entry_id,redaction_epoch,result_sha256,completed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    redaction_event_id,
                    source_effect_id,
                    source_usage_id,
                    source_ref,
                    capture["capture_id"] if capture is not None else None,
                    entry["entry_id"] if entry is not None else None,
                    redaction_epoch,
                    result_sha256,
                    now,
                ),
            )
            if entry is None:
                return None
            updated = conn.execute(
                "SELECT * FROM journal_entries WHERE entry_id=?",
                (entry["entry_id"],),
            ).fetchone()
            assert updated is not None
            return self._entry(updated)

    def record_mutation(
        self,
        *,
        client_mutation_id: str,
        request_sha256: str,
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        encoded = _json(result)
        assert encoded is not None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_mutations WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
            if row is not None:
                if row["request_sha256"] != request_sha256:
                    raise JournalCaptureConflict(
                        "That mutation key was already used for a different change."
                    )
                decoded = json.loads(row["result_json"])
                return decoded
            conn.execute(
                "INSERT INTO journal_mutations VALUES(?,?,?,?)",
                (client_mutation_id, request_sha256, encoded, _utc_now()),
            )
        return result

    @staticmethod
    def _capture(row: sqlite3.Row) -> JournalCapture:
        return JournalCapture(
            capture_id=row["capture_id"],
            client_mutation_id=row["client_mutation_id"],
            request_sha256=row["request_sha256"],
            source_ref=row["source_ref"],
            representation_id=row["representation_id"],
            submission_id=row["submission_id"],
            command_id=row["command_id"],
            source_effect_id=row["source_effect_id"],
            source_usage_id=row["source_usage_id"],
            day_id=row["day_id"],
            requested_target=CaptureTarget(row["requested_target"]),
            resolved_target=(
                CaptureTarget(row["resolved_target"])
                if row["resolved_target"] is not None
                else None
            ),
            mode=CaptureMode(row["mode"]),
            input_mode=row["input_mode"],
            stated_at=row["stated_at"],
            submitted_at=row["submitted_at"],
            persistence_status=row["persistence_status"],
            processing_status=ProcessingState(row["processing_status"]),
            processing_error_code=row["processing_error_code"],
            annotation=_decode_json(row["annotation_json"]),
            entry_id=row["entry_id"],
            revision=int(row["revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _entry(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            entry_id=row["entry_id"],
            capture_id=row["capture_id"],
            day_id=row["day_id"],
            entry_kind=CaptureTarget(row["entry_kind"]),
            source_ref=row["source_ref"],
            content_sha256=row["content_sha256"],
            markdown=row["markdown"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=int(row["version"]),
            resolution_state=row["resolution_state"],
            processing_status=ProcessingState(row["processing_status"]),
            annotation=_decode_json(row["annotation_json"]),
            processing_error_code=row["processing_error_code"],
            projection_state=ProjectionState(row["projection_state"]),
            projection_marker=row["projection_marker"],
            projection_base_sha256=row["projection_base_sha256"],
            projection_result_sha256=row["projection_result_sha256"],
        )

    @staticmethod
    def _effect(row: sqlite3.Row) -> JournalEffect:
        return JournalEffect(
            effect_id=row["effect_id"],
            capture_id=row["capture_id"],
            effect_type=row["effect_type"],
            state=EffectState(row["state"]),
            attempts=int(row["attempts"]),
            authorization_fingerprint=row["authorization_fingerprint"],
            authorization_expires_at=row["authorization_expires_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _document_binding(row: sqlite3.Row) -> JournalDocumentBinding:
        inspection = _decode_json(row["inspection_json"])
        if inspection is None:
            raise RuntimeError("invalid_journal_document_binding")
        return JournalDocumentBinding(
            entry_id=row["entry_id"],
            binding_id=row["binding_id"],
            store_id=row["store_id"],
            document_id=row["document_id"],
            change_id=row["change_id"],
            source_consumer_id=row["source_consumer_id"],
            source_usage_id=row["source_usage_id"],
            source_use_kind=row["source_use_kind"],
            source_disclosure_kind=row["source_disclosure_kind"],
            source_redaction_policy=row["source_redaction_policy"],
            source_maintenance_state=row["source_maintenance_state"],
            source_maintenance=_decode_json(row["source_maintenance_json"]) or {},
            cowork_href=row["cowork_href"],
            content_authority_epoch=int(row["content_authority_epoch"]),
            entry_version=int(row["entry_version"]),
            inspection=inspection,
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _usage_transition(row: sqlite3.Row) -> JournalDocumentUsageTransition:
        return JournalDocumentUsageTransition(**dict(row))

    @staticmethod
    def _migration(row: sqlite3.Row) -> JournalMigrationRecord:
        return JournalMigrationRecord(
            entity_kind=str(row["entity_kind"]),
            entity_id=str(row["entity_id"]),
            day_id=str(row["day_id"]),
            marker_id=str(row["marker_id"]),
            selection_start=row["selection_start"],
            selection_end=row["selection_end"],
            selected_file_sha256=row["selected_file_sha256"],
            selected_section_sha256=row["selected_section_sha256"],
            source_ref=row["source_ref"],
            representation_id=row["representation_id"],
            source_content_sha256=row["source_content_sha256"],
            binding_id=row["binding_id"],
            store_id=row["store_id"],
            document_id=row["document_id"],
            comparison_state=JournalMigrationComparison(row["comparison_state"]),
            byte_parity=(
                None if row["byte_parity"] is None else bool(row["byte_parity"])
            ),
            normalized_parity=(
                None
                if row["normalized_parity"] is None
                else bool(row["normalized_parity"])
            ),
            structural_parity=(
                None
                if row["structural_parity"] is None
                else bool(row["structural_parity"])
            ),
            rollback_deadline=row["rollback_deadline"],
            mirrored_state=JournalMigrationState(row["mirrored_state"]),
            mirrored_authority_epoch=int(row["mirrored_authority_epoch"]),
            projection_state=str(row["projection_state"]),
            divergence_source_ref=row["divergence_source_ref"],
            operation_id=row["operation_id"],
            error_code=row["error_code"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
