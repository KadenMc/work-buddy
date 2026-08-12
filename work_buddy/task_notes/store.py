"""Durable, disabled-by-default authority and recovery coordinator."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
    source_foundation_read_only,
)
from work_buddy.task_notes.models import (
    AuthorityEpoch,
    AuthorityState,
    ChangeOperationState,
    ComparisonState,
    ProjectionState,
    SagaState,
    SourceDependencyState,
    TaskNoteChangeOperation,
    TaskNoteMigration,
    TaskNoteSaga,
    TaskNoteSourceDependency,
)


SCHEMA_VERSION = 4


class TaskNoteMigrationError(RuntimeError):
    code = "task_note_migration_error"


class TaskNoteCutoverBlocked(TaskNoteMigrationError):
    code = "task_note_cutover_blocked"


class TaskNoteMigrationConflict(TaskNoteMigrationError):
    code = "task_note_migration_conflict"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _json(values: Sequence[str]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _require_current_journal_exit_evidence(
    evidence: Mapping[str, Any] | None,
) -> None:
    """Validate the exact current Journal migration exit receipt.

    The public task-note operator obtains this value from
    ``latest_current_exit_evidence`` immediately before cutover.  A mutable
    task-local Boolean is deliberately insufficient: Journal owns the cohort
    inventory and the static production-callsite digest.
    """

    from work_buddy.journal_capture.migration import CALLSITE_INVENTORY_SHA256

    expected_fields = {
        "receipt_id",
        "inventory_sha256",
        "callsite_inventory_sha256",
        "authority_summary",
        "created_at",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != expected_fields:
        raise TaskNoteCutoverBlocked(
            "current Journal exit evidence is required before task-note cutover"
        )
    if evidence.get("callsite_inventory_sha256") != CALLSITE_INVENTORY_SHA256:
        raise TaskNoteCutoverBlocked("Journal callsite exit evidence is stale")
    for field, length in (
        ("receipt_id", 32),
        ("inventory_sha256", 64),
        ("callsite_inventory_sha256", 64),
    ):
        value = evidence.get(field)
        if (
            not isinstance(value, str)
            or len(value) != length
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise TaskNoteCutoverBlocked("Journal exit evidence is malformed")
    summary = evidence.get("authority_summary")
    if (
        not isinstance(summary, Mapping)
        or summary.get("schema") != "wb.journal-exit-evidence/v1"
        or summary.get("cutoverGate") != "open"
        or isinstance(summary.get("days"), bool)
        or not isinstance(summary.get("days"), int)
        or isinstance(summary.get("entities"), bool)
        or not isinstance(summary.get("entities"), int)
        or not isinstance(evidence.get("created_at"), str)
    ):
        raise TaskNoteCutoverBlocked("Journal exit evidence is malformed")


class TaskNoteMigrationStore:
    """SQLite state machine for per-entity authority and operation sagas.

    Merely constructing this store does not enable a cutover.  The task-note
    cutover gate starts closed, each note must independently have recorded
    parity, and current Journal-owned exit evidence is required at transition
    time.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        if source_foundation_read_only():
            if not self.path.is_file():
                raise TaskNoteMigrationError(
                    "task_note_state_missing_during_restore_reconciliation"
                )
            self._validate_existing()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        read_only = source_foundation_read_only()
        conn = sqlite3.connect(
            (
                f"file:{self.path.resolve()}?mode=ro"
                if read_only
                else str(self.path)
            ),
            timeout=10.0,
            uri=read_only,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        require_source_foundation_writable("task_note_migration.write")
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
                CREATE TABLE IF NOT EXISTS migration_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS content_authority_epochs(
                    domain_namespace TEXT NOT NULL,
                    entity_kind TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'legacy_authoritative','shadow_imported',
                        'cowork_authoritative','retired'
                    )),
                    epoch INTEGER NOT NULL CHECK(epoch >= 0),
                    domain_revision TEXT,
                    rollback_deadline TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(domain_namespace,entity_kind,entity_id)
                );
                CREATE TABLE IF NOT EXISTS task_note_migrations(
                    note_uuid TEXT PRIMARY KEY,
                    source_ref TEXT,
                    source_content_sha256 TEXT,
                    legacy_file_sha256 TEXT,
                    legacy_normalized_sha256 TEXT,
                    document_projection_sha256 TEXT,
                    document_normalized_sha256 TEXT,
                    byte_parity INTEGER,
                    normalized_parity INTEGER,
                    comparison_state TEXT NOT NULL CHECK(comparison_state IN (
                        'pending','parity','mismatch'
                    )),
                    binding_id TEXT,
                    store_id TEXT,
                    document_id TEXT,
                    projection_base_sha256 TEXT,
                    projection_result_sha256 TEXT,
                    projection_generation INTEGER NOT NULL DEFAULT 0,
                    projection_document_head TEXT,
                    projection_state TEXT NOT NULL CHECK(projection_state IN (
                        'none','current','paused_diverged','retired'
                    )),
                    divergence_source_ref TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_note_sagas(
                    saga_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL CHECK(operation IN (
                        'create','delete','retire','recover'
                    )),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    note_uuid TEXT NOT NULL,
                    task_id TEXT,
                    state TEXT NOT NULL CHECK(state IN (
                        'prepared','running','completed','recoverable'
                    )),
                    required_steps_json TEXT NOT NULL,
                    completed_steps_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_note_source_dependencies(
                    usage_id TEXT PRIMARY KEY,
                    note_uuid TEXT NOT NULL,
                    consumer_id TEXT NOT NULL UNIQUE,
                    relationship TEXT NOT NULL CHECK(relationship IN (
                        'shadow_import','whole_document_replace'
                    )),
                    source_ref TEXT NOT NULL,
                    representation_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    redaction_epoch INTEGER NOT NULL CHECK(redaction_epoch >= 0),
                    binding_id TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    result_document_head_sha256 TEXT,
                    state TEXT NOT NULL CHECK(state IN (
                        'reserved','acknowledged','released','review_required'
                    )),
                    review_reason TEXT,
                    superseded_by_usage_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_note_source_dependency_note
                    ON task_note_source_dependencies(note_uuid,state,created_at);
                CREATE TABLE IF NOT EXISTS task_note_change_operations(
                    operation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    note_uuid TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'prepared','source_reserved','document_committed',
                        'acknowledged','completed','review_required','recoverable'
                    )),
                    source_ref TEXT,
                    representation_id TEXT,
                    source_content_sha256 TEXT,
                    source_usage_id TEXT,
                    change_id TEXT,
                    result_document_head_sha256 TEXT,
                    projection_state TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_usage_id)
                        REFERENCES task_note_source_dependencies(usage_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_note_change_recovery
                    ON task_note_change_operations(state,created_at);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO migration_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO migration_meta(key,value) VALUES('task_note_cutover_gate','0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO migration_meta(key,value) VALUES('journal_cutover_gate','0')"
            )
            version = conn.execute(
                "SELECT value FROM migration_meta WHERE key='schema_version'"
            ).fetchone()
            if version is None or int(version[0]) > SCHEMA_VERSION:
                raise TaskNoteMigrationError("task_note_schema_too_new")
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(task_note_source_dependencies)"
                ).fetchall()
            }
            if "review_reason" not in columns:
                conn.execute(
                    "ALTER TABLE task_note_source_dependencies "
                    "ADD COLUMN review_reason TEXT"
                )
            if int(version[0]) < SCHEMA_VERSION:
                conn.execute(
                    "DELETE FROM migration_meta WHERE key='journal_exit_gate'"
                )
                conn.execute(
                    "UPDATE migration_meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )

    def _validate_existing(self) -> None:
        try:
            with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchall()
                version = conn.execute(
                    "SELECT value FROM migration_meta WHERE key='schema_version'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise TaskNoteMigrationError(
                "task_note_state_invalid_during_restore_reconciliation"
            ) from exc
        if integrity != [("ok",)] or version is None or int(version[0]) != SCHEMA_VERSION:
            raise TaskNoteMigrationError(
                "task_note_state_invalid_during_restore_reconciliation"
            )

    def set_gate(self, name: str, enabled: bool) -> None:
        if name not in {
            "task_note_cutover_gate",
            "journal_cutover_gate",
        }:
            raise ValueError("unknown migration gate")
        with self.transaction() as conn:
            conn.execute(
                "UPDATE migration_meta SET value=? WHERE key=?",
                ("1" if enabled else "0", name),
            )

    def gate_enabled(self, name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM migration_meta WHERE key=?", (name,)
            ).fetchone()
        return row is not None and row[0] == "1"

    @staticmethod
    def _authority(row: sqlite3.Row) -> AuthorityEpoch:
        return AuthorityEpoch(
            domain_namespace=str(row["domain_namespace"]),
            entity_kind=str(row["entity_kind"]),
            entity_id=str(row["entity_id"]),
            state=AuthorityState(row["state"]),
            epoch=int(row["epoch"]),
            domain_revision=row["domain_revision"],
            rollback_deadline=row["rollback_deadline"],
            updated_at=str(row["updated_at"]),
        )

    def ensure_authority(
        self,
        domain_namespace: str,
        entity_kind: str,
        entity_id: str,
        *,
        domain_revision: str | None = None,
    ) -> AuthorityEpoch:
        now = _now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO content_authority_epochs VALUES(?,?,?,?,?,?,?,?)",
                (
                    domain_namespace,
                    entity_kind,
                    entity_id,
                    AuthorityState.LEGACY.value,
                    0,
                    domain_revision,
                    None,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
            assert row is not None
            return self._authority(row)

    def get_authority(
        self, domain_namespace: str, entity_kind: str, entity_id: str
    ) -> AuthorityEpoch | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
        return None if row is None else self._authority(row)

    def mark_shadow(
        self,
        domain_namespace: str,
        entity_kind: str,
        entity_id: str,
        *,
        domain_revision: str | None = None,
    ) -> AuthorityEpoch:
        self.ensure_authority(
            domain_namespace, entity_kind, entity_id, domain_revision=domain_revision
        )
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
            assert row is not None
            if row["state"] == AuthorityState.LEGACY.value:
                conn.execute(
                    "UPDATE content_authority_epochs SET state=?,domain_revision=?,updated_at=? "
                    "WHERE domain_namespace=? AND entity_kind=? AND entity_id=?",
                    (
                        AuthorityState.SHADOW.value,
                        domain_revision,
                        _now(),
                        domain_namespace,
                        entity_kind,
                        entity_id,
                    ),
                )
            refreshed = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
            assert refreshed is not None
            return self._authority(refreshed)

    def cutover(
        self,
        domain_namespace: str,
        entity_kind: str,
        entity_id: str,
        *,
        domain_revision: str,
        rollback_deadline: str | None = None,
        journal_exit_evidence: Mapping[str, Any] | None = None,
    ) -> AuthorityEpoch:
        if domain_namespace == "tasks":
            _require_current_journal_exit_evidence(journal_exit_evidence)
            if not self.gate_enabled("task_note_cutover_gate"):
                raise TaskNoteCutoverBlocked("task-note cutover gate is closed")
            migration = self.get_task_note(entity_id)
            if (
                migration is None
                or migration.comparison_state is not ComparisonState.PARITY
                or migration.binding_id is None
            ):
                raise TaskNoteCutoverBlocked("task-note parity is not established")
            if rollback_deadline is None:
                raise TaskNoteCutoverBlocked("task-note rollback window is required")
            try:
                deadline = datetime.fromisoformat(rollback_deadline.replace("Z", "+00:00"))
            except ValueError as exc:
                raise TaskNoteCutoverBlocked("task-note rollback deadline is invalid") from exc
            if deadline.tzinfo is None or deadline <= datetime.now(UTC):
                raise TaskNoteCutoverBlocked("task-note rollback window has closed")
        elif domain_namespace == "journal":
            if not self.gate_enabled("journal_cutover_gate"):
                raise TaskNoteCutoverBlocked("Journal cutover gate is closed")
        else:
            raise ValueError("unsupported authority namespace")

        current = self.ensure_authority(
            domain_namespace, entity_kind, entity_id, domain_revision=domain_revision
        )
        if current.state is AuthorityState.COWORK:
            return current
        if current.state not in {AuthorityState.SHADOW, AuthorityState.LEGACY}:
            raise TaskNoteMigrationConflict("entity cannot be cut over")
        with self.transaction() as conn:
            conn.execute(
                "UPDATE content_authority_epochs SET state=?,epoch=epoch+1,"
                "domain_revision=?,rollback_deadline=?,updated_at=? WHERE "
                "domain_namespace=? AND entity_kind=? AND entity_id=?",
                (
                    AuthorityState.COWORK.value,
                    domain_revision,
                    rollback_deadline,
                    _now(),
                    domain_namespace,
                    entity_kind,
                    entity_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
            assert row is not None
        return self._authority(row)

    def validate_cutover(
        self,
        domain_namespace: str,
        entity_kind: str,
        entity_id: str,
        *,
        rollback_deadline: str | None,
        journal_exit_evidence: Mapping[str, Any] | None = None,
    ) -> AuthorityEpoch:
        """Validate a transition without claiming cross-store authority.

        Production coordinators commit the DocumentCausalityStore binding
        first, then use ``mirror_authority`` below. This preflight keeps all
        closed-gate/parity/deadline checks ahead of that canonical commit.
        """

        if domain_namespace == "tasks":
            _require_current_journal_exit_evidence(journal_exit_evidence)
            if not self.gate_enabled("task_note_cutover_gate"):
                raise TaskNoteCutoverBlocked("task-note cutover gate is closed")
            migration = self.get_task_note(entity_id)
            if (
                migration is None
                or migration.comparison_state is not ComparisonState.PARITY
                or migration.binding_id is None
            ):
                raise TaskNoteCutoverBlocked("task-note parity is not established")
            if rollback_deadline is None:
                raise TaskNoteCutoverBlocked("task-note rollback window is required")
            try:
                deadline = datetime.fromisoformat(
                    rollback_deadline.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise TaskNoteCutoverBlocked(
                    "task-note rollback deadline is invalid"
                ) from exc
            if deadline.tzinfo is None or deadline <= datetime.now(UTC):
                raise TaskNoteCutoverBlocked("task-note rollback window has closed")
        elif domain_namespace == "journal":
            if not self.gate_enabled("journal_cutover_gate"):
                raise TaskNoteCutoverBlocked("Journal cutover gate is closed")
        else:
            raise ValueError("unsupported authority namespace")
        current = self.ensure_authority(domain_namespace, entity_kind, entity_id)
        if current.state not in {
            AuthorityState.SHADOW,
            AuthorityState.LEGACY,
            AuthorityState.COWORK,
        }:
            raise TaskNoteMigrationConflict("entity cannot be cut over")
        return current

    def record_cutover_intent(
        self,
        domain_namespace: str,
        entity_kind: str,
        entity_id: str,
        *,
        expected_epoch: int,
        domain_revision: str,
        rollback_deadline: str,
    ) -> AuthorityEpoch:
        """Durably retain the rollback window before canonical cutover.

        The document binding is the authority epoch source.  This row is its
        recovery mirror, but the requested rollback deadline exists only at
        the task-note boundary.  Recording it on the pre-cutover mirror makes
        a crash after the canonical epoch commit recoverable without inventing
        or silently dropping the user's rollback window.
        """

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
            if row is None:
                raise TaskNoteMigrationConflict("authority mirror is unavailable")
            current = self._authority(row)
            if (
                current.epoch != expected_epoch
                or current.state not in {AuthorityState.SHADOW, AuthorityState.LEGACY}
                or current.domain_revision != domain_revision
            ):
                raise TaskNoteMigrationConflict("cutover intent disagrees with authority")
            if current.rollback_deadline not in {None, rollback_deadline}:
                raise TaskNoteMigrationConflict(
                    "cutover intent has a different rollback deadline"
                )
            conn.execute(
                "UPDATE content_authority_epochs SET rollback_deadline=?,updated_at=? "
                "WHERE domain_namespace=? AND entity_kind=? AND entity_id=?",
                (
                    rollback_deadline,
                    _now(),
                    domain_namespace,
                    entity_kind,
                    entity_id,
                ),
            )
            refreshed = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
            assert refreshed is not None
            return self._authority(refreshed)

    def validate_rollback(
        self,
        domain_namespace: str,
        entity_kind: str,
        entity_id: str,
    ) -> AuthorityEpoch:
        current = self.get_authority(domain_namespace, entity_kind, entity_id)
        if current is None or current.state is not AuthorityState.COWORK:
            raise TaskNoteMigrationConflict("entity is not Co-work authoritative")
        if domain_namespace == "tasks" and current.rollback_deadline is None:
            raise TaskNoteCutoverBlocked("task-note rollback window is unavailable")
        if current.rollback_deadline:
            deadline = datetime.fromisoformat(
                current.rollback_deadline.replace("Z", "+00:00")
            )
            if deadline.tzinfo is None or datetime.now(UTC) > deadline:
                raise TaskNoteCutoverBlocked("rollback window has closed")
        return current

    def mirror_authority(
        self,
        domain_namespace: str,
        entity_kind: str,
        entity_id: str,
        *,
        state: AuthorityState,
        epoch: int,
        domain_revision: str,
        rollback_deadline: str | None,
    ) -> AuthorityEpoch:
        """Mirror one already-committed canonical binding authority epoch."""

        if state not in {AuthorityState.COWORK, AuthorityState.LEGACY}:
            raise ValueError("unsupported mirrored authority state")
        current = self.ensure_authority(
            domain_namespace,
            entity_kind,
            entity_id,
            domain_revision=domain_revision,
        )
        if epoch < current.epoch or epoch > current.epoch + 1:
            raise TaskNoteMigrationConflict("authority epoch cannot be reconciled")
        if epoch == current.epoch:
            if (
                current.state is not state
                or current.domain_revision != domain_revision
                or current.rollback_deadline != rollback_deadline
            ):
                raise TaskNoteMigrationConflict("authority mirror disagrees")
            return current
        with self.transaction() as conn:
            conn.execute(
                "UPDATE content_authority_epochs SET state=?,epoch=?,"
                "domain_revision=?,rollback_deadline=?,updated_at=? WHERE "
                "domain_namespace=? AND entity_kind=? AND entity_id=?",
                (
                    state.value,
                    epoch,
                    domain_revision,
                    rollback_deadline,
                    _now(),
                    domain_namespace,
                    entity_kind,
                    entity_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
            assert row is not None
            return self._authority(row)

    def rollback(
        self,
        domain_namespace: str,
        entity_kind: str,
        entity_id: str,
        *,
        domain_revision: str,
    ) -> AuthorityEpoch:
        current = self.get_authority(domain_namespace, entity_kind, entity_id)
        if current is None or current.state is not AuthorityState.COWORK:
            raise TaskNoteMigrationConflict("entity is not Co-work authoritative")
        if current.rollback_deadline:
            deadline = datetime.fromisoformat(
                current.rollback_deadline.replace("Z", "+00:00")
            )
            if deadline.tzinfo is None or datetime.now(UTC) > deadline:
                raise TaskNoteCutoverBlocked("rollback window has closed")
        with self.transaction() as conn:
            conn.execute(
                "UPDATE content_authority_epochs SET state=?,epoch=epoch+1,"
                "domain_revision=?,rollback_deadline=NULL,updated_at=? WHERE "
                "domain_namespace=? AND entity_kind=? AND entity_id=?",
                (
                    AuthorityState.LEGACY.value,
                    domain_revision,
                    _now(),
                    domain_namespace,
                    entity_kind,
                    entity_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
            assert row is not None
            return self._authority(row)

    def retire(self, domain_namespace: str, entity_kind: str, entity_id: str) -> AuthorityEpoch:
        current = self.ensure_authority(domain_namespace, entity_kind, entity_id)
        if current.state is AuthorityState.RETIRED:
            return current
        with self.transaction() as conn:
            conn.execute(
                "UPDATE content_authority_epochs SET state=?,epoch=epoch+1,updated_at=? "
                "WHERE domain_namespace=? AND entity_kind=? AND entity_id=?",
                (
                    AuthorityState.RETIRED.value,
                    _now(),
                    domain_namespace,
                    entity_kind,
                    entity_id,
                ),
            )
            if domain_namespace == "tasks" and entity_kind == "task_note":
                conn.execute(
                    "UPDATE task_note_migrations SET projection_state=?,updated_at=? "
                    "WHERE note_uuid=?",
                    (ProjectionState.RETIRED.value, _now(), entity_id),
                )
            row = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
            assert row is not None
            return self._authority(row)

    def mirror_retired_authority(
        self,
        domain_namespace: str,
        entity_kind: str,
        entity_id: str,
        *,
        epoch: int,
    ) -> AuthorityEpoch:
        """Mirror a canonical binding retirement without inventing an epoch."""

        current = self.get_authority(domain_namespace, entity_kind, entity_id)
        if current is None or current.epoch != epoch:
            raise TaskNoteMigrationConflict("retired authority epoch disagrees")
        if current.state is AuthorityState.RETIRED:
            return current
        with self.transaction() as conn:
            conn.execute(
                "UPDATE content_authority_epochs SET state=?,rollback_deadline=NULL,"
                "updated_at=? WHERE domain_namespace=? AND entity_kind=? AND entity_id=?",
                (
                    AuthorityState.RETIRED.value,
                    _now(),
                    domain_namespace,
                    entity_kind,
                    entity_id,
                ),
            )
            if domain_namespace == "tasks" and entity_kind == "task_note":
                conn.execute(
                    "UPDATE task_note_migrations SET projection_state=?,updated_at=? "
                    "WHERE note_uuid=?",
                    (ProjectionState.RETIRED.value, _now(), entity_id),
                )
            row = conn.execute(
                "SELECT * FROM content_authority_epochs WHERE domain_namespace=? "
                "AND entity_kind=? AND entity_id=?",
                (domain_namespace, entity_kind, entity_id),
            ).fetchone()
            assert row is not None
            return self._authority(row)

    @staticmethod
    def _migration(row: sqlite3.Row) -> TaskNoteMigration:
        return TaskNoteMigration(
            note_uuid=str(row["note_uuid"]),
            source_ref=row["source_ref"],
            source_content_sha256=row["source_content_sha256"],
            legacy_file_sha256=row["legacy_file_sha256"],
            legacy_normalized_sha256=row["legacy_normalized_sha256"],
            document_projection_sha256=row["document_projection_sha256"],
            document_normalized_sha256=row["document_normalized_sha256"],
            byte_parity=(None if row["byte_parity"] is None else bool(row["byte_parity"])),
            normalized_parity=(
                None
                if row["normalized_parity"] is None
                else bool(row["normalized_parity"])
            ),
            comparison_state=ComparisonState(row["comparison_state"]),
            binding_id=row["binding_id"],
            store_id=row["store_id"],
            document_id=row["document_id"],
            projection_base_sha256=row["projection_base_sha256"],
            projection_result_sha256=row["projection_result_sha256"],
            projection_generation=int(row["projection_generation"]),
            projection_document_head=row["projection_document_head"],
            projection_state=ProjectionState(row["projection_state"]),
            divergence_source_ref=row["divergence_source_ref"],
            updated_at=str(row["updated_at"]),
        )

    def get_task_note(self, note_uuid: str) -> TaskNoteMigration | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_migrations WHERE note_uuid=?", (note_uuid,)
            ).fetchone()
        return None if row is None else self._migration(row)

    def list_task_notes(self) -> tuple[TaskNoteMigration, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_note_migrations ORDER BY note_uuid"
            ).fetchall()
        return tuple(self._migration(row) for row in rows)

    def record_shadow(
        self,
        *,
        note_uuid: str,
        source_ref: str,
        source_content_sha256: str,
        legacy_file_sha256: str,
        legacy_normalized_sha256: str,
        document_projection_sha256: str,
        document_normalized_sha256: str,
        binding_id: str,
        store_id: str,
        document_id: str,
        byte_parity: bool,
        normalized_parity: bool,
        domain_revision: str,
    ) -> TaskNoteMigration:
        comparison = (
            ComparisonState.PARITY if normalized_parity else ComparisonState.MISMATCH
        )
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM task_note_migrations WHERE note_uuid=?", (note_uuid,)
            ).fetchone()
            if existing is not None:
                prior = self._migration(existing)
                if prior.projection_state is ProjectionState.PAUSED_DIVERGED:
                    raise TaskNoteMigrationConflict("diverged note requires review")
            conn.execute(
                "INSERT INTO task_note_migrations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(note_uuid) DO UPDATE SET source_ref=excluded.source_ref,"
                "source_content_sha256=excluded.source_content_sha256,"
                "legacy_file_sha256=excluded.legacy_file_sha256,"
                "legacy_normalized_sha256=excluded.legacy_normalized_sha256,"
                "document_projection_sha256=excluded.document_projection_sha256,"
                "document_normalized_sha256=excluded.document_normalized_sha256,"
                "byte_parity=excluded.byte_parity,normalized_parity=excluded.normalized_parity,"
                "comparison_state=excluded.comparison_state,binding_id=excluded.binding_id,"
                "store_id=excluded.store_id,document_id=excluded.document_id,updated_at=excluded.updated_at",
                (
                    note_uuid,
                    source_ref,
                    source_content_sha256,
                    legacy_file_sha256,
                    legacy_normalized_sha256,
                    document_projection_sha256,
                    document_normalized_sha256,
                    int(byte_parity),
                    int(normalized_parity),
                    comparison.value,
                    binding_id,
                    store_id,
                    document_id,
                    None,
                    None,
                    0,
                    None,
                    ProjectionState.NONE.value,
                    None,
                    _now(),
                ),
            )
            authority = conn.execute(
                "SELECT state FROM content_authority_epochs WHERE "
                "domain_namespace='tasks' AND entity_kind='task_note' AND entity_id=?",
                (note_uuid,),
            ).fetchone()
            if authority is None or authority["state"] != AuthorityState.COWORK.value:
                # A fresh shadow after rollback establishes a new file base.
                # Do not carry a compatibility marker hash/generation from the
                # prior authority epoch into a later cutover.
                conn.execute(
                    "UPDATE task_note_migrations SET projection_base_sha256=NULL,"
                    "projection_result_sha256=NULL,projection_generation=0,"
                    "projection_document_head=NULL,projection_state=?,"
                    "divergence_source_ref=NULL WHERE note_uuid=?",
                    (ProjectionState.NONE.value, note_uuid),
                )
            row = conn.execute(
                "SELECT * FROM task_note_migrations WHERE note_uuid=?", (note_uuid,)
            ).fetchone()
            assert row is not None
        self.mark_shadow(
            "tasks", "task_note", note_uuid, domain_revision=domain_revision
        )
        return self._migration(row)

    def reset_projection_after_rollback(self, note_uuid: str) -> TaskNoteMigration:
        """Forget the fenced Co-work compatibility marker after exact unwrap."""

        with self.transaction() as conn:
            conn.execute(
                "UPDATE task_note_migrations SET projection_base_sha256=NULL,"
                "projection_result_sha256=NULL,projection_generation=0,"
                "projection_document_head=NULL,projection_state=?,"
                "divergence_source_ref=NULL,updated_at=? WHERE note_uuid=?",
                (ProjectionState.NONE.value, _now(), note_uuid),
            )
            row = conn.execute(
                "SELECT * FROM task_note_migrations WHERE note_uuid=?", (note_uuid,)
            ).fetchone()
            if row is None:
                raise KeyError("task_note_migration_not_found")
            return self._migration(row)

    def record_projection(
        self,
        note_uuid: str,
        *,
        base_sha256: str,
        result_sha256: str,
        generation: int,
        document_head: str,
    ) -> TaskNoteMigration:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE task_note_migrations SET projection_base_sha256=?,"
                "projection_result_sha256=?,projection_generation=?,"
                "projection_document_head=?,projection_state=?,"
                "divergence_source_ref=NULL,updated_at=? WHERE note_uuid=?",
                (
                    base_sha256,
                    result_sha256,
                    generation,
                    document_head,
                    ProjectionState.CURRENT.value,
                    _now(),
                    note_uuid,
                ),
            )
            row = conn.execute(
                "SELECT * FROM task_note_migrations WHERE note_uuid=?", (note_uuid,)
            ).fetchone()
            if row is None:
                raise KeyError("task_note_migration_not_found")
            return self._migration(row)

    def pause_diverged(self, note_uuid: str, *, source_ref: str) -> TaskNoteMigration:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE task_note_migrations SET projection_state=?,"
                "divergence_source_ref=?,updated_at=? WHERE note_uuid=?",
                (ProjectionState.PAUSED_DIVERGED.value, source_ref, _now(), note_uuid),
            )
            row = conn.execute(
                "SELECT * FROM task_note_migrations WHERE note_uuid=?", (note_uuid,)
            ).fetchone()
            if row is None:
                raise KeyError("task_note_migration_not_found")
            return self._migration(row)

    @staticmethod
    def _saga(row: sqlite3.Row) -> TaskNoteSaga:
        return TaskNoteSaga(
            saga_id=str(row["saga_id"]),
            operation=str(row["operation"]),
            idempotency_key=str(row["idempotency_key"]),
            request_sha256=str(row["request_sha256"]),
            note_uuid=str(row["note_uuid"]),
            task_id=row["task_id"],
            state=SagaState(row["state"]),
            required_steps=tuple(json.loads(row["required_steps_json"])),
            completed_steps=tuple(json.loads(row["completed_steps_json"])),
            attempts=int(row["attempts"]),
            error_code=row["error_code"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def begin_saga(
        self,
        *,
        operation: str,
        idempotency_key: str,
        request_sha256: str,
        note_uuid: str,
        task_id: str | None,
        required_steps: Sequence[str],
    ) -> TaskNoteSaga:
        if operation not in {"create", "delete", "retire", "recover"}:
            raise ValueError("unsupported task-note saga")
        saga_id = hashlib.sha256(
            f"task-note-saga\0{operation}\0{idempotency_key}".encode()
        ).hexdigest()[:32]
        now = _now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_sagas WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                existing = self._saga(row)
                if (
                    existing.operation != operation
                    or existing.request_sha256 != request_sha256
                    or existing.note_uuid != note_uuid
                    or existing.task_id != task_id
                    or existing.required_steps != tuple(required_steps)
                ):
                    raise TaskNoteMigrationConflict("task-note saga idempotency conflict")
                return existing
            conn.execute(
                "INSERT INTO task_note_sagas VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    saga_id,
                    operation,
                    idempotency_key,
                    request_sha256,
                    note_uuid,
                    task_id,
                    SagaState.PREPARED.value,
                    _json(required_steps),
                    "[]",
                    0,
                    None,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM task_note_sagas WHERE saga_id=?", (saga_id,)
            ).fetchone()
            assert row is not None
            return self._saga(row)

    def complete_saga_step(self, saga_id: str, step: str) -> TaskNoteSaga:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_sagas WHERE saga_id=?", (saga_id,)
            ).fetchone()
            if row is None:
                raise KeyError("task_note_saga_not_found")
            saga = self._saga(row)
            if step not in saga.required_steps:
                raise TaskNoteMigrationConflict("unexpected task-note saga step")
            completed = list(saga.completed_steps)
            if step not in completed:
                completed.append(step)
            finished = set(completed) == set(saga.required_steps)
            conn.execute(
                "UPDATE task_note_sagas SET state=?,completed_steps_json=?,"
                "attempts=attempts+1,error_code=NULL,updated_at=? WHERE saga_id=?",
                (
                    SagaState.COMPLETED.value if finished else SagaState.RUNNING.value,
                    _json(completed),
                    _now(),
                    saga_id,
                ),
            )
            refreshed = conn.execute(
                "SELECT * FROM task_note_sagas WHERE saga_id=?", (saga_id,)
            ).fetchone()
            assert refreshed is not None
            return self._saga(refreshed)

    def fail_saga(self, saga_id: str, *, error_code: str) -> TaskNoteSaga:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE task_note_sagas SET state=?,attempts=attempts+1,error_code=?,"
                "updated_at=? WHERE saga_id=?",
                (SagaState.RECOVERABLE.value, error_code, _now(), saga_id),
            )
            row = conn.execute(
                "SELECT * FROM task_note_sagas WHERE saga_id=?", (saga_id,)
            ).fetchone()
            if row is None:
                raise KeyError("task_note_saga_not_found")
            return self._saga(row)

    def get_saga(self, saga_id: str) -> TaskNoteSaga | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_sagas WHERE saga_id=?", (saga_id,)
            ).fetchone()
        return None if row is None else self._saga(row)

    def recoverable_sagas(self) -> tuple[TaskNoteSaga, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_note_sagas WHERE state!='completed' "
                "ORDER BY created_at,saga_id"
            ).fetchall()
        return tuple(self._saga(row) for row in rows)

    @staticmethod
    def _source_dependency(row: sqlite3.Row) -> TaskNoteSourceDependency:
        return TaskNoteSourceDependency(
            usage_id=str(row["usage_id"]),
            note_uuid=str(row["note_uuid"]),
            consumer_id=str(row["consumer_id"]),
            relationship=str(row["relationship"]),
            source_ref=str(row["source_ref"]),
            representation_id=str(row["representation_id"]),
            content_sha256=str(row["content_sha256"]),
            redaction_epoch=int(row["redaction_epoch"]),
            binding_id=str(row["binding_id"]),
            store_id=str(row["store_id"]),
            document_id=str(row["document_id"]),
            result_document_head_sha256=row["result_document_head_sha256"],
            state=SourceDependencyState(row["state"]),
            review_reason=row["review_reason"],
            superseded_by_usage_id=row["superseded_by_usage_id"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def record_source_dependency(
        self,
        *,
        usage_id: str,
        note_uuid: str,
        consumer_id: str,
        relationship: str,
        source_ref: str,
        representation_id: str,
        content_sha256: str,
        redaction_epoch: int,
        binding_id: str,
        store_id: str,
        document_id: str,
    ) -> TaskNoteSourceDependency:
        """Persist the reverse managed-copy target before Source acknowledgement."""

        now = _now()
        expected = (
            note_uuid,
            consumer_id,
            relationship,
            source_ref,
            representation_id,
            content_sha256,
            redaction_epoch,
            binding_id,
            store_id,
            document_id,
        )
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_source_dependencies WHERE usage_id=?",
                (usage_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO task_note_source_dependencies "
                    "(usage_id,note_uuid,consumer_id,relationship,source_ref,"
                    "representation_id,content_sha256,redaction_epoch,binding_id,"
                    "store_id,document_id,result_document_head_sha256,state,"
                    "review_reason,superseded_by_usage_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,NULL,NULL,?,?)",
                    (*((usage_id,) + expected), SourceDependencyState.RESERVED.value, now, now),
                )
                row = conn.execute(
                    "SELECT * FROM task_note_source_dependencies WHERE usage_id=?",
                    (usage_id,),
                ).fetchone()
            else:
                actual = (
                    str(row["note_uuid"]),
                    str(row["consumer_id"]),
                    str(row["relationship"]),
                    str(row["source_ref"]),
                    str(row["representation_id"]),
                    str(row["content_sha256"]),
                    int(row["redaction_epoch"]),
                    str(row["binding_id"]),
                    str(row["store_id"]),
                    str(row["document_id"]),
                )
                if actual != expected:
                    raise TaskNoteMigrationConflict(
                        "task-note Source dependency idempotency conflict"
                    )
            assert row is not None
            return self._source_dependency(row)

    def get_source_dependency(self, usage_id: str) -> TaskNoteSourceDependency | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_source_dependencies WHERE usage_id=?",
                (usage_id,),
            ).fetchone()
        return None if row is None else self._source_dependency(row)

    def get_source_dependency_by_consumer(
        self, consumer_id: str
    ) -> TaskNoteSourceDependency | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_source_dependencies WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()
        return None if row is None else self._source_dependency(row)

    def source_dependencies_for_note(
        self, note_uuid: str, *, active_only: bool = False
    ) -> tuple[TaskNoteSourceDependency, ...]:
        sql = "SELECT * FROM task_note_source_dependencies WHERE note_uuid=?"
        values: tuple[object, ...] = (note_uuid,)
        if active_only:
            sql += " AND state IN ('reserved','acknowledged','review_required')"
        sql += " ORDER BY created_at,usage_id"
        with self._connect() as conn:
            rows = conn.execute(sql, values).fetchall()
        return tuple(self._source_dependency(row) for row in rows)

    def update_source_dependency(
        self,
        usage_id: str,
        *,
        state: SourceDependencyState | None = None,
        result_document_head_sha256: str | None = None,
        superseded_by_usage_id: str | None = None,
        review_reason: str | None = None,
    ) -> TaskNoteSourceDependency:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_source_dependencies WHERE usage_id=?",
                (usage_id,),
            ).fetchone()
            if row is None:
                raise KeyError("task_note_source_dependency_not_found")
            conn.execute(
                "UPDATE task_note_source_dependencies SET state=?,"
                "result_document_head_sha256=COALESCE(?,result_document_head_sha256),"
                "superseded_by_usage_id=COALESCE(?,superseded_by_usage_id),"
                "review_reason=CASE WHEN ? IS NOT NULL THEN ? "
                "WHEN ? IN ('acknowledged','released') THEN NULL "
                "ELSE review_reason END,updated_at=? "
                "WHERE usage_id=?",
                (
                    (state or SourceDependencyState(row["state"])).value,
                    result_document_head_sha256,
                    superseded_by_usage_id,
                    review_reason,
                    review_reason,
                    (state or SourceDependencyState(row["state"])).value,
                    _now(),
                    usage_id,
                ),
            )
            refreshed = conn.execute(
                "SELECT * FROM task_note_source_dependencies WHERE usage_id=?",
                (usage_id,),
            ).fetchone()
            assert refreshed is not None
            return self._source_dependency(refreshed)

    def resolve_source_redaction_target(
        self,
        consumer_id: str,
        *,
        current_document_head_sha256: str,
        has_direct_changes: bool,
    ) -> dict[str, object] | None:
        """Classify one Sources effect without exposing or rereading content.

        Only the exact head produced by the acknowledged Source use may take
        the automatic whole-document scrub path.  Any later/direct edit is a
        mixed document and therefore enters review instead of being erased.
        """

        dependency = self.get_source_dependency_by_consumer(consumer_id)
        if dependency is None:
            return None
        if dependency.state is SourceDependencyState.RELEASED:
            disposition = "released"
            reason = "source_copy_superseded"
        elif dependency.state is SourceDependencyState.REVIEW_REQUIRED:
            disposition = "review"
            reason = dependency.review_reason or "source_copy_already_requires_review"
        elif dependency.result_document_head_sha256 is None:
            # The Sources reservation intentionally precedes the cross-store
            # document commit. Redaction must retry until recovery proves
            # whether a managed copy committed; absence is not proof of either
            # a clean release or a mixed document.
            disposition = "pending"
            reason = "source_copy_commit_unresolved"
        elif has_direct_changes:
            disposition = "review"
            reason = "document_contains_direct_edits"
        elif dependency.result_document_head_sha256 != current_document_head_sha256:
            disposition = "review"
            reason = "document_head_changed_after_source_copy"
        else:
            disposition = "scrub"
            reason = "exact_source_copy_is_current"
        return {
            "schema": "wb.task-note-source-redaction-target/v1",
            "disposition": disposition,
            "reason": reason,
            "usageId": dependency.usage_id,
            "noteUuid": dependency.note_uuid,
            "bindingId": dependency.binding_id,
            "storeId": dependency.store_id,
            "documentId": dependency.document_id,
            "relationship": dependency.relationship,
        }

    @staticmethod
    def _change_operation(row: sqlite3.Row) -> TaskNoteChangeOperation:
        return TaskNoteChangeOperation(
            operation_id=str(row["operation_id"]),
            idempotency_key=str(row["idempotency_key"]),
            request_sha256=str(row["request_sha256"]),
            note_uuid=str(row["note_uuid"]),
            state=ChangeOperationState(row["state"]),
            source_ref=row["source_ref"],
            representation_id=row["representation_id"],
            source_content_sha256=row["source_content_sha256"],
            source_usage_id=row["source_usage_id"],
            change_id=row["change_id"],
            result_document_head_sha256=row["result_document_head_sha256"],
            projection_state=row["projection_state"],
            error_code=row["error_code"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def begin_change_operation(
        self, *, idempotency_key: str, request_sha256: str, note_uuid: str
    ) -> TaskNoteChangeOperation:
        operation_id = hashlib.sha256(
            f"task-note-source-change\0{idempotency_key}".encode("utf-8")
        ).hexdigest()[:32]
        now = _now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_change_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                existing = self._change_operation(row)
                if (
                    existing.request_sha256 != request_sha256
                    or existing.note_uuid != note_uuid
                ):
                    raise TaskNoteMigrationConflict(
                        "task-note change idempotency conflict"
                    )
                return existing
            conn.execute(
                "INSERT INTO task_note_change_operations "
                "(operation_id,idempotency_key,request_sha256,note_uuid,state,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    operation_id,
                    idempotency_key,
                    request_sha256,
                    note_uuid,
                    ChangeOperationState.PREPARED.value,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM task_note_change_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            assert row is not None
            return self._change_operation(row)

    def get_change_operation(self, operation_id: str) -> TaskNoteChangeOperation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_change_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return None if row is None else self._change_operation(row)

    def change_operation_for_key(
        self, idempotency_key: str
    ) -> TaskNoteChangeOperation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_change_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return None if row is None else self._change_operation(row)

    def advance_change_operation(
        self,
        operation_id: str,
        *,
        state: ChangeOperationState,
        source_ref: str | None = None,
        representation_id: str | None = None,
        source_content_sha256: str | None = None,
        source_usage_id: str | None = None,
        change_id: str | None = None,
        result_document_head_sha256: str | None = None,
        projection_state: str | None = None,
        error_code: str | None = None,
    ) -> TaskNoteChangeOperation:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM task_note_change_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError("task_note_change_operation_not_found")
            conn.execute(
                "UPDATE task_note_change_operations SET state=?,"
                "source_ref=COALESCE(?,source_ref),"
                "representation_id=COALESCE(?,representation_id),"
                "source_content_sha256=COALESCE(?,source_content_sha256),"
                "source_usage_id=COALESCE(?,source_usage_id),"
                "change_id=COALESCE(?,change_id),"
                "result_document_head_sha256=COALESCE(?,result_document_head_sha256),"
                "projection_state=COALESCE(?,projection_state),error_code=?,updated_at=? "
                "WHERE operation_id=?",
                (
                    state.value,
                    source_ref,
                    representation_id,
                    source_content_sha256,
                    source_usage_id,
                    change_id,
                    result_document_head_sha256,
                    projection_state,
                    error_code,
                    _now(),
                    operation_id,
                ),
            )
            refreshed = conn.execute(
                "SELECT * FROM task_note_change_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            assert refreshed is not None
            return self._change_operation(refreshed)

    def recoverable_change_operations(self) -> tuple[TaskNoteChangeOperation, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_note_change_operations "
                "WHERE state NOT IN ('completed','review_required') "
                "ORDER BY created_at,operation_id"
            ).fetchall()
        return tuple(self._change_operation(row) for row in rows)

    def status_summary(self) -> dict[str, object]:
        """Return content-free rollout, parity, projection, and recovery counts."""

        with self._connect() as conn:
            gates = {
                str(row["key"]): row["value"] == "1"
                for row in conn.execute(
                    "SELECT key,value FROM migration_meta WHERE key LIKE '%_gate' "
                    "ORDER BY key"
                ).fetchall()
            }
            authority = [
                {
                    "domainNamespace": str(row["domain_namespace"]),
                    "entityKind": str(row["entity_kind"]),
                    "state": str(row["state"]),
                    "count": int(row["count"]),
                }
                for row in conn.execute(
                    "SELECT domain_namespace,entity_kind,state,COUNT(*) count "
                    "FROM content_authority_epochs GROUP BY domain_namespace,entity_kind,state "
                    "ORDER BY domain_namespace,entity_kind,state"
                ).fetchall()
            ]
            comparisons = {
                str(row["comparison_state"]): int(row["count"])
                for row in conn.execute(
                    "SELECT comparison_state,COUNT(*) count FROM task_note_migrations "
                    "GROUP BY comparison_state ORDER BY comparison_state"
                ).fetchall()
            }
            projections = {
                str(row["projection_state"]): int(row["count"])
                for row in conn.execute(
                    "SELECT projection_state,COUNT(*) count FROM task_note_migrations "
                    "GROUP BY projection_state ORDER BY projection_state"
                ).fetchall()
            }
            sagas = {
                str(row["state"]): int(row["count"])
                for row in conn.execute(
                    "SELECT state,COUNT(*) count FROM task_note_sagas "
                    "GROUP BY state ORDER BY state"
                ).fetchall()
            }
            source_dependencies = {
                str(row["state"]): int(row["count"])
                for row in conn.execute(
                    "SELECT state,COUNT(*) count FROM task_note_source_dependencies "
                    "GROUP BY state ORDER BY state"
                ).fetchall()
            }
            changes = {
                str(row["state"]): int(row["count"])
                for row in conn.execute(
                    "SELECT state,COUNT(*) count FROM task_note_change_operations "
                    "GROUP BY state ORDER BY state"
                ).fetchall()
            }
        return {
            "schema": "work-buddy-task-note-migration-status/v1",
            "gates": gates,
            "authority": authority,
            "comparisons": comparisons,
            "projections": projections,
            "sagas": sagas,
            "sourceDependencies": source_dependencies,
            "sourceChanges": changes,
        }


class JournalAuthorityCoordinator:
    """Per-Running-Note and per-logical-day Log authority epochs.

    This extends the proven pilot's model without writing or replacing any
    legacy Journal Markdown.  Projection/import remains a separate, explicit
    step owned by the Journal migration track.
    """

    def __init__(self, store: TaskNoteMigrationStore) -> None:
        self.store = store

    def running_note(self, stable_note_id: str, *, revision: str | None = None) -> AuthorityEpoch:
        return self.store.ensure_authority(
            "journal", "running_note", stable_note_id, domain_revision=revision
        )

    def logical_day_log(self, day_id: str, *, revision: str | None = None) -> AuthorityEpoch:
        return self.store.ensure_authority(
            "journal", "logical_day_log", day_id, domain_revision=revision
        )

    def mark_shadow(self, entity_kind: str, entity_id: str, *, revision: str) -> AuthorityEpoch:
        if entity_kind not in {"running_note", "logical_day_log"}:
            raise ValueError("invalid Journal authority entity")
        return self.store.mark_shadow(
            "journal", entity_kind, entity_id, domain_revision=revision
        )

    def cutover(self, entity_kind: str, entity_id: str, *, revision: str) -> AuthorityEpoch:
        if entity_kind not in {"running_note", "logical_day_log"}:
            raise ValueError("invalid Journal authority entity")
        return self.store.cutover(
            "journal", entity_kind, entity_id, domain_revision=revision
        )


__all__ = [
    "JournalAuthorityCoordinator",
    "TaskNoteCutoverBlocked",
    "TaskNoteMigrationConflict",
    "TaskNoteMigrationError",
    "TaskNoteMigrationStore",
]
