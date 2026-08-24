"""Durable domain bindings, change intents/receipts, and projection cursors."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
    source_foundation_read_only,
)


_SCHEMA_VERSION = 2
_ID = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROJECTION_MODES = {"none", "managed_file", "managed_section"}


class DocumentCausalityError(RuntimeError):
    code = "document_causality_error"
    retryable = False


class BindingConflict(DocumentCausalityError):
    code = "domain_document_binding_conflict"


class ChangeConflict(DocumentCausalityError):
    code = "document_change_conflict"
    retryable = True


@dataclass(frozen=True, slots=True)
class DomainDocumentBinding:
    binding_id: str
    domain_namespace: str
    domain_kind: str
    domain_entity_id: str
    domain_revision: str
    store_id: str
    document_id: str
    role: str
    lifecycle: str
    content_authority: str
    content_authority_epoch: int
    projection_path: str | None
    projection_mode: str
    migration_origin: str | None
    created_at: str
    created_by: str
    superseded_at: str | None
    superseded_by: str | None


@dataclass(frozen=True, slots=True)
class PreparedDocumentChange:
    change_id: str
    binding_id: str | None
    operation_kind: str
    idempotency_key: str
    request_sha256: str
    store_id: str
    document_id: str
    source_ref: str | None
    source_representation_id: str | None
    source_content_sha256: str | None
    exact_copied_text_sha256: str | None
    base_snapshot_sha256: str
    base_structured_head_sha256: str
    base_generation_sha256: str
    selector_json: str
    actors_json: str
    state: str
    result_snapshot_sha256: str | None
    result_structured_head_sha256: str | None
    result_projection_sha256: str | None
    result_update_sha256: str | None
    operation_manifest_sha256: str | None
    protocol_version: str | None
    runtime_version: str | None
    schema_version: str | None
    error_code: str | None
    prepared_at: str
    materialized_at: str | None
    committed_at: str | None


@dataclass(frozen=True, slots=True)
class DocumentChangeRecord:
    change_id: str
    binding_id: str | None
    operation_kind: str
    store_id: str
    document_id: str
    source_ref: str | None
    source_representation_id: str | None
    source_content_sha256: str | None
    exact_copied_text_sha256: str | None
    base_snapshot_sha256: str
    base_structured_head_sha256: str
    base_generation_sha256: str
    result_snapshot_sha256: str
    result_structured_head_sha256: str
    result_projection_sha256: str
    result_update_sha256: str
    selector_json: str
    actors_json: str
    assurance_json: str
    protocol_version: str
    runtime_version: str
    schema_version: str
    operation_manifest_sha256: str
    committed_at: str


@dataclass(frozen=True, slots=True)
class ProjectionCursor:
    binding_id: str
    content_authority_epoch: int
    document_head_sha256: str | None
    section_sha256: str | None
    file_sha256: str | None
    status: str
    divergence_source_ref: str | None
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _digest(value: str, label: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a sha256 digest")
    return value


def _identifier(value: str, label: str) -> str:
    if _ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a record id")
    return value


def _projection_mode(value: str) -> str:
    if value not in _PROJECTION_MODES:
        raise ValueError("invalid projection mode")
    return value


class DocumentCausalityStore:
    """Per-Co-work-store causality database beside the Truth store."""

    def __init__(self, truth_sidecar: str | Path) -> None:
        root = Path(truth_sidecar).expanduser().resolve()
        self.path = root / "document-causality.db"
        if source_foundation_read_only():
            if not self.path.is_file():
                raise DocumentCausalityError(
                    "document_causality_missing_during_restore_reconciliation"
                )
            self._validate_existing()
        else:
            root.mkdir(parents=True, exist_ok=True)
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
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        require_source_foundation_writable("document_causality.write")
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
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS causality_meta(
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS domain_document_bindings(
                    binding_id TEXT PRIMARY KEY,
                    domain_namespace TEXT NOT NULL,
                    domain_kind TEXT NOT NULL,
                    domain_entity_id TEXT NOT NULL,
                    domain_revision TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('current','superseded','retired')),
                    content_authority TEXT NOT NULL CHECK(content_authority IN ('domain','co_work')),
                    content_authority_epoch INTEGER NOT NULL CHECK(content_authority_epoch >= 0),
                    projection_path TEXT,
                    migration_origin TEXT,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    superseded_at TEXT,
                    superseded_by TEXT,
                    projection_mode TEXT NOT NULL DEFAULT 'managed_file'
                      CHECK(projection_mode IN ('none','managed_file','managed_section'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_domain_binding_current
                  ON domain_document_bindings(domain_namespace,domain_kind,domain_entity_id,role)
                  WHERE lifecycle='current';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_document_binding_current
                  ON domain_document_bindings(store_id,document_id,role)
                  WHERE lifecycle='current';

                CREATE TABLE IF NOT EXISTS document_change_intents(
                    change_id TEXT PRIMARY KEY,
                    binding_id TEXT REFERENCES domain_document_bindings(binding_id),
                    operation_kind TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    source_ref TEXT,
                    source_representation_id TEXT,
                    source_content_sha256 TEXT,
                    exact_copied_text_sha256 TEXT,
                    base_snapshot_sha256 TEXT NOT NULL,
                    base_structured_head_sha256 TEXT NOT NULL,
                    base_generation_sha256 TEXT NOT NULL,
                    selector_json TEXT NOT NULL,
                    actors_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('prepared','materialized','committed','failed')),
                    result_snapshot_sha256 TEXT,
                    result_structured_head_sha256 TEXT,
                    result_projection_sha256 TEXT,
                    result_update_sha256 TEXT,
                    operation_manifest_sha256 TEXT,
                    protocol_version TEXT,
                    runtime_version TEXT,
                    schema_version TEXT,
                    error_code TEXT,
                    prepared_at TEXT NOT NULL,
                    materialized_at TEXT,
                    committed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS document_change_records(
                    change_id TEXT PRIMARY KEY REFERENCES document_change_intents(change_id),
                    binding_id TEXT REFERENCES domain_document_bindings(binding_id),
                    operation_kind TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    source_ref TEXT,
                    source_representation_id TEXT,
                    source_content_sha256 TEXT,
                    exact_copied_text_sha256 TEXT,
                    base_snapshot_sha256 TEXT NOT NULL,
                    base_structured_head_sha256 TEXT NOT NULL,
                    base_generation_sha256 TEXT NOT NULL,
                    result_snapshot_sha256 TEXT NOT NULL,
                    result_structured_head_sha256 TEXT NOT NULL,
                    result_projection_sha256 TEXT NOT NULL,
                    result_update_sha256 TEXT NOT NULL,
                    selector_json TEXT NOT NULL,
                    actors_json TEXT NOT NULL,
                    assurance_json TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    runtime_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    operation_manifest_sha256 TEXT NOT NULL,
                    committed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS document_projection_cursors(
                    binding_id TEXT PRIMARY KEY REFERENCES domain_document_bindings(binding_id),
                    content_authority_epoch INTEGER NOT NULL,
                    document_head_sha256 TEXT,
                    section_sha256 TEXT,
                    file_sha256 TEXT,
                    status TEXT NOT NULL CHECK(status IN ('pending','committed','paused_diverged','failed')),
                    divergence_source_ref TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_projection_intents(
                    projection_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    binding_id TEXT NOT NULL REFERENCES domain_document_bindings(binding_id),
                    content_authority_epoch INTEGER NOT NULL,
                    document_head_sha256 TEXT NOT NULL,
                    expected_section_sha256 TEXT,
                    result_section_sha256 TEXT NOT NULL,
                    result_projection_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('prepared','committed','paused_diverged','failed')),
                    error_code TEXT,
                    prepared_at TEXT NOT NULL,
                    committed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS document_projection_receipts(
                    projection_id TEXT PRIMARY KEY REFERENCES document_projection_intents(projection_id),
                    binding_id TEXT NOT NULL,
                    content_authority_epoch INTEGER NOT NULL,
                    document_head_sha256 TEXT NOT NULL,
                    base_file_sha256 TEXT NOT NULL,
                    result_file_sha256 TEXT NOT NULL,
                    result_section_sha256 TEXT NOT NULL,
                    committed_at TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS document_change_records_no_update
                BEFORE UPDATE ON document_change_records BEGIN
                  SELECT RAISE(ABORT, 'document change records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS document_change_records_no_delete
                BEFORE DELETE ON document_change_records BEGIN
                  SELECT RAISE(ABORT, 'document change records are immutable');
                END;
                """
            )
            row = conn.execute(
                "SELECT value FROM causality_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO causality_meta(key,value) VALUES('schema_version',?)",
                    (str(_SCHEMA_VERSION),),
                )
            elif int(row["value"]) == 1:
                columns = {
                    str(item["name"])
                    for item in conn.execute(
                        "PRAGMA table_info(domain_document_bindings)"
                    ).fetchall()
                }
                if "projection_mode" not in columns:
                    conn.execute(
                        "ALTER TABLE domain_document_bindings ADD COLUMN "
                        "projection_mode TEXT NOT NULL DEFAULT 'managed_file' "
                        "CHECK(projection_mode IN ('none','managed_file','managed_section'))"
                    )
                    conn.execute(
                        "UPDATE domain_document_bindings SET projection_mode="
                        "CASE WHEN domain_namespace='journal' "
                        "THEN 'managed_section' ELSE 'managed_file' END"
                    )
                conn.execute(
                    "UPDATE causality_meta SET value=? WHERE key='schema_version'",
                    (str(_SCHEMA_VERSION),),
                )
            elif int(row["value"]) != _SCHEMA_VERSION:
                raise DocumentCausalityError("unsupported_document_causality_schema")

    def _validate_existing(self) -> None:
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchall()
                row = conn.execute(
                    "SELECT value FROM causality_meta WHERE key='schema_version'"
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise DocumentCausalityError(
                "invalid_document_causality_store_during_restore_reconciliation"
            ) from exc
        if integrity != [("ok",)] or row is None or int(row[0]) != _SCHEMA_VERSION:
            raise DocumentCausalityError(
                "invalid_document_causality_store_during_restore_reconciliation"
            )

    @staticmethod
    def _binding(row: sqlite3.Row) -> DomainDocumentBinding:
        return DomainDocumentBinding(**dict(row))

    @staticmethod
    def _intent(row: sqlite3.Row) -> PreparedDocumentChange:
        return PreparedDocumentChange(**dict(row))

    @staticmethod
    def _record(row: sqlite3.Row) -> DocumentChangeRecord:
        return DocumentChangeRecord(**dict(row))

    def ensure_binding(
        self,
        *,
        domain_namespace: str,
        domain_kind: str,
        domain_entity_id: str,
        domain_revision: str,
        store_id: str,
        document_id: str,
        role: str,
        created_by: str,
        projection_path: str | None = None,
        projection_mode: str = "managed_file",
        migration_origin: str | None = None,
    ) -> DomainDocumentBinding:
        projection_mode = _projection_mode(projection_mode)
        if projection_mode == "none" and projection_path is not None:
            raise ValueError("projection_path must be absent when projection_mode is none")
        identity = "\0".join(
            (domain_namespace, domain_kind, domain_entity_id, role, store_id, document_id)
        )
        binding_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE domain_namespace=? "
                "AND domain_kind=? AND domain_entity_id=? AND role=? AND lifecycle='current'",
                (domain_namespace, domain_kind, domain_entity_id, role),
            ).fetchone()
            if row is not None:
                existing = self._binding(row)
                if (
                    existing.store_id != store_id
                    or existing.document_id != document_id
                    or existing.projection_mode != projection_mode
                    or existing.projection_path != projection_path
                ):
                    raise BindingConflict()
                return existing
            now = _now()
            try:
                conn.execute(
                    "INSERT INTO domain_document_bindings "
                    "(binding_id,domain_namespace,domain_kind,domain_entity_id,"
                    "domain_revision,store_id,document_id,role,lifecycle,content_authority,"
                    "content_authority_epoch,projection_path,migration_origin,created_at,"
                    "created_by,superseded_at,superseded_by,projection_mode) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        binding_id, domain_namespace, domain_kind, domain_entity_id,
                        domain_revision, store_id, document_id, role, "current", "domain", 0,
                        projection_path, migration_origin, now, created_by, None, None,
                        projection_mode,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BindingConflict() from exc
            row = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
            assert row is not None
            return self._binding(row)

    def get_binding(self, binding_id: str) -> DomainDocumentBinding | None:
        _identifier(binding_id, "binding_id")
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
        return None if row is None else self._binding(row)

    def binding_for_domain(
        self, domain_namespace: str, domain_kind: str, domain_entity_id: str, role: str
    ) -> DomainDocumentBinding | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE domain_namespace=? "
                "AND domain_kind=? AND domain_entity_id=? AND role=? AND lifecycle='current'",
                (domain_namespace, domain_kind, domain_entity_id, role),
            ).fetchone()
        return None if row is None else self._binding(row)

    def binding_for_document(
        self,
        store_id: str,
        document_id: str,
        *,
        role: str | None = None,
    ) -> DomainDocumentBinding | None:
        """Resolve one current reverse binding without guessing on ambiguity."""

        query = (
            "SELECT * FROM domain_document_bindings WHERE store_id=? AND document_id=? "
            "AND lifecycle='current'"
        )
        params: tuple[object, ...] = (store_id, document_id)
        if role is not None:
            query += " AND role=?"
            params = (*params, role)
        query += " ORDER BY role"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        if len(rows) > 1:
            raise BindingConflict()
        return None if not rows else self._binding(rows[0])

    def list_bindings(
        self,
        *,
        content_authority: str | None = None,
    ) -> tuple[DomainDocumentBinding, ...]:
        query = "SELECT * FROM domain_document_bindings WHERE lifecycle='current'"
        params: tuple[object, ...] = ()
        if content_authority is not None:
            if content_authority not in {"domain", "co_work"}:
                raise ValueError("invalid content authority")
            query += " AND content_authority=?"
            params = (content_authority,)
        query += " ORDER BY domain_namespace,domain_kind,domain_entity_id,role"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(self._binding(row) for row in rows)

    def supersede_binding(
        self,
        binding_id: str,
        *,
        domain_revision: str,
        store_id: str,
        document_id: str,
        created_by: str,
        projection_path: str | None = None,
        projection_mode: str | None = None,
        migration_origin: str | None = None,
    ) -> DomainDocumentBinding:
        """Atomically replace one current reverse binding with its successor."""

        _identifier(binding_id, "binding_id")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
            if row is None:
                raise KeyError("binding_not_found")
            current = self._binding(row)
            if current.lifecycle != "current":
                if current.superseded_by:
                    successor = conn.execute(
                        "SELECT * FROM domain_document_bindings WHERE binding_id=?",
                        (current.superseded_by,),
                    ).fetchone()
                    if successor is not None:
                        resolved = self._binding(successor)
                        if resolved.store_id == store_id and resolved.document_id == document_id:
                            return resolved
                raise BindingConflict()
            if current.store_id == store_id and current.document_id == document_id:
                return current
            next_projection_mode = _projection_mode(
                current.projection_mode if projection_mode is None else projection_mode
            )
            if next_projection_mode == "none" and projection_path is not None:
                raise ValueError(
                    "projection_path must be absent when projection_mode is none"
                )
            identity = "\0".join(
                (
                    current.domain_namespace,
                    current.domain_kind,
                    current.domain_entity_id,
                    current.role,
                    store_id,
                    document_id,
                )
            )
            successor_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
            now = _now()
            conn.execute(
                "UPDATE domain_document_bindings SET lifecycle='superseded',"
                "superseded_at=?,superseded_by=? WHERE binding_id=? AND lifecycle='current'",
                (now, successor_id, binding_id),
            )
            try:
                conn.execute(
                    "INSERT INTO domain_document_bindings "
                    "(binding_id,domain_namespace,domain_kind,domain_entity_id,"
                    "domain_revision,store_id,document_id,role,lifecycle,content_authority,"
                    "content_authority_epoch,projection_path,migration_origin,created_at,"
                    "created_by,superseded_at,superseded_by,projection_mode) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        successor_id,
                        current.domain_namespace,
                        current.domain_kind,
                        current.domain_entity_id,
                        domain_revision,
                        store_id,
                        document_id,
                        current.role,
                        "current",
                        "domain",
                        0,
                        projection_path,
                        migration_origin,
                        now,
                        created_by,
                        None,
                        None,
                        next_projection_mode,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BindingConflict() from exc
            successor = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?",
                (successor_id,),
            ).fetchone()
            assert successor is not None
            return self._binding(successor)

    def configure_projection_mode(
        self,
        binding_id: str,
        *,
        projection_mode: str,
        projection_path: str | None,
    ) -> DomainDocumentBinding:
        """Change projection policy while domain authority is still fenced.

        This is intentionally unavailable after Co-work authority activates: a
        cutover receipt must never silently change whether external writes are
        expected.  Existing prepared projection work also blocks the change.
        """

        _identifier(binding_id, "binding_id")
        projection_mode = _projection_mode(projection_mode)
        if projection_mode == "none" and projection_path is not None:
            raise ValueError("projection_path must be absent when projection_mode is none")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise KeyError("binding_not_found")
            current = self._binding(row)
            if current.lifecycle != "current" or current.content_authority != "domain":
                raise BindingConflict()
            pending = conn.execute(
                "SELECT 1 FROM document_projection_intents WHERE binding_id=? "
                "AND state='prepared' LIMIT 1",
                (binding_id,),
            ).fetchone()
            if pending is not None:
                raise BindingConflict()
            if (
                current.projection_mode == projection_mode
                and current.projection_path == projection_path
            ):
                return current
            conn.execute(
                "UPDATE domain_document_bindings SET projection_mode=?,projection_path=? "
                "WHERE binding_id=?",
                (projection_mode, projection_path, binding_id),
            )
            if projection_mode == "none":
                conn.execute(
                    "DELETE FROM document_projection_cursors WHERE binding_id=?",
                    (binding_id,),
                )
            refreshed = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            assert refreshed is not None
            return self._binding(refreshed)

    def retire_binding(self, binding_id: str) -> DomainDocumentBinding:
        """Retire a current binding without deleting its causality history."""

        _identifier(binding_id, "binding_id")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
            if row is None:
                raise KeyError("binding_not_found")
            current = self._binding(row)
            if current.lifecycle == "retired":
                return current
            if current.lifecycle != "current":
                raise BindingConflict()
            conn.execute(
                "UPDATE domain_document_bindings SET lifecycle='retired',"
                "superseded_at=? WHERE binding_id=?",
                (_now(), binding_id),
            )
            refreshed = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
            assert refreshed is not None
            return self._binding(refreshed)

    def orphaned_bindings(
        self,
        document_exists: Callable[[str, str], bool],
    ) -> tuple[DomainDocumentBinding, ...]:
        """Query current reverse bindings whose document target is absent."""

        return tuple(
            binding
            for binding in self.list_bindings()
            if not document_exists(binding.store_id, binding.document_id)
        )

    def cutover_to_cowork(self, binding_id: str, *, domain_revision: str) -> DomainDocumentBinding:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
            if row is None:
                raise KeyError("binding_not_found")
            current = self._binding(row)
            if current.lifecycle != "current":
                raise BindingConflict()
            if current.content_authority == "co_work":
                if current.domain_revision != domain_revision:
                    raise BindingConflict()
                return current
            conn.execute(
                "UPDATE domain_document_bindings SET content_authority='co_work', "
                "content_authority_epoch=content_authority_epoch+1, domain_revision=? "
                "WHERE binding_id=?",
                (domain_revision, binding_id),
            )
            if current.projection_mode == "none":
                conn.execute(
                    "DELETE FROM document_projection_cursors WHERE binding_id=?",
                    (binding_id,),
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO document_projection_cursors VALUES(?,?,?,?,?,?,?,?)",
                    (binding_id, current.content_authority_epoch + 1, None, None, None,
                     "pending", None, _now()),
                )
            refreshed = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
            assert refreshed is not None
            return self._binding(refreshed)

    def rollback_to_domain(
        self,
        binding_id: str,
        *,
        domain_revision: str,
        expected_epoch: int,
    ) -> DomainDocumentBinding:
        """Fence one Co-work authority epoch and restore domain authority.

        ``expected_epoch`` is the Co-work epoch being rolled back.  A retry
        after the transaction committed is idempotent only when it names that
        same prior epoch and revision.  Prepared projections from the fenced
        epoch become failed, and the cursor advances to a content-free failed
        epoch so a stale worker cannot publish an old Co-work head.
        """

        _identifier(binding_id, "binding_id")
        if not isinstance(expected_epoch, int) or expected_epoch < 0:
            raise ValueError("expected_epoch must be a non-negative integer")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
            if row is None:
                raise KeyError("binding_not_found")
            current = self._binding(row)
            if current.lifecycle != "current":
                raise BindingConflict()
            if current.content_authority == "domain":
                if (
                    current.content_authority_epoch == expected_epoch + 1
                    and current.domain_revision == domain_revision
                ):
                    return current
                raise BindingConflict()
            if (
                current.content_authority != "co_work"
                or current.content_authority_epoch != expected_epoch
            ):
                raise BindingConflict()
            next_epoch = expected_epoch + 1
            conn.execute(
                "UPDATE domain_document_bindings SET content_authority='domain',"
                "content_authority_epoch=?,domain_revision=? WHERE binding_id=?",
                (next_epoch, domain_revision, binding_id),
            )
            conn.execute(
                "UPDATE document_projection_intents SET state='failed',"
                "error_code='authority_rolled_back' WHERE binding_id=? "
                "AND content_authority_epoch=? AND state='prepared'",
                (binding_id, expected_epoch),
            )
            if current.projection_mode == "none":
                conn.execute(
                    "DELETE FROM document_projection_cursors WHERE binding_id=?",
                    (binding_id,),
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO document_projection_cursors "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (binding_id, next_epoch, None, None, None, "failed", None, _now()),
                )
            refreshed = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
            assert refreshed is not None
            return self._binding(refreshed)

    def prepare_change(
        self,
        *,
        idempotency_key: str,
        operation_kind: str,
        store_id: str,
        document_id: str,
        base_snapshot_sha256: str,
        base_structured_head_sha256: str,
        base_generation_sha256: str,
        selector: Mapping[str, Any],
        actors: Mapping[str, Any],
        binding_id: str | None = None,
        source_ref: str | None = None,
        source_representation_id: str | None = None,
        source_content_sha256: str | None = None,
        exact_copied_text_sha256: str | None = None,
    ) -> PreparedDocumentChange:
        request = {
            "operation_kind": operation_kind, "store_id": store_id,
            "document_id": document_id, "binding_id": binding_id,
            "source_ref": source_ref, "source_representation_id": source_representation_id,
            "source_content_sha256": source_content_sha256,
            "exact_copied_text_sha256": exact_copied_text_sha256,
            "base_snapshot_sha256": base_snapshot_sha256,
            "base_structured_head_sha256": base_structured_head_sha256,
            "base_generation_sha256": base_generation_sha256,
            "selector": selector, "actors": actors,
        }
        request_sha = hashlib.sha256(_json(request).encode("utf-8")).hexdigest()
        change_id = hashlib.sha256(f"document-change:{idempotency_key}".encode()).hexdigest()[:32]
        for value, label in (
            (base_snapshot_sha256, "base snapshot"),
            (base_structured_head_sha256, "base head"),
            (base_generation_sha256, "base generation"),
        ):
            _digest(value, label)
        for value, label in (
            (source_content_sha256, "source content"),
            (exact_copied_text_sha256, "exact copied text"),
        ):
            if value is not None:
                _digest(value, label)
        with self.transaction() as conn:
            if binding_id is not None:
                binding = conn.execute(
                    "SELECT * FROM domain_document_bindings WHERE binding_id=?",
                    (binding_id,),
                ).fetchone()
                if (
                    binding is None
                    or binding["lifecycle"] != "current"
                    or binding["store_id"] != store_id
                    or binding["document_id"] != document_id
                ):
                    raise BindingConflict()
            row = conn.execute(
                "SELECT * FROM document_change_intents WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                existing = self._intent(row)
                if existing.request_sha256 != request_sha:
                    raise ChangeConflict()
                return existing
            conn.execute(
                "INSERT INTO document_change_intents "
                "(change_id,binding_id,operation_kind,idempotency_key,request_sha256,"
                "store_id,document_id,source_ref,source_representation_id,source_content_sha256,"
                "exact_copied_text_sha256,base_snapshot_sha256,base_structured_head_sha256,"
                "base_generation_sha256,selector_json,actors_json,state,prepared_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    change_id, binding_id, operation_kind, idempotency_key, request_sha,
                    store_id, document_id, source_ref, source_representation_id,
                    source_content_sha256, exact_copied_text_sha256,
                    base_snapshot_sha256, base_structured_head_sha256,
                    base_generation_sha256, _json(selector), _json(actors),
                    "prepared", _now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM document_change_intents WHERE change_id=?", (change_id,)
            ).fetchone()
            assert row is not None
            return self._intent(row)

    def record_materialized(
        self,
        change_id: str,
        *,
        result_snapshot_sha256: str,
        result_structured_head_sha256: str,
        result_projection_sha256: str,
        result_update_sha256: str,
        operation_manifest_sha256: str,
        protocol_version: str,
        runtime_version: str,
        schema_version: str,
    ) -> PreparedDocumentChange:
        values = (
            result_snapshot_sha256, result_structured_head_sha256,
            result_projection_sha256, result_update_sha256, operation_manifest_sha256,
        )
        for value in values:
            _digest(value, "materialized result")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM document_change_intents WHERE change_id=?", (change_id,)
            ).fetchone()
            if row is None:
                raise KeyError("document_change_not_found")
            current = self._intent(row)
            if current.state in {"materialized", "committed"}:
                if (
                    current.result_snapshot_sha256 != result_snapshot_sha256
                    or current.result_structured_head_sha256 != result_structured_head_sha256
                    or current.result_projection_sha256 != result_projection_sha256
                    or current.result_update_sha256 != result_update_sha256
                    or current.operation_manifest_sha256 != operation_manifest_sha256
                    or current.protocol_version != protocol_version
                    or current.runtime_version != runtime_version
                    or current.schema_version != schema_version
                ):
                    raise ChangeConflict()
                return current
            if current.state != "prepared":
                raise ChangeConflict()
            conn.execute(
                "UPDATE document_change_intents SET state='materialized',"
                "result_snapshot_sha256=?,result_structured_head_sha256=?,"
                "result_projection_sha256=?,result_update_sha256=?,"
                "operation_manifest_sha256=?,protocol_version=?,runtime_version=?,"
                "schema_version=?,materialized_at=? WHERE change_id=?",
                (*values, protocol_version, runtime_version, schema_version, _now(), change_id),
            )
            refreshed = conn.execute(
                "SELECT * FROM document_change_intents WHERE change_id=?", (change_id,)
            ).fetchone()
            assert refreshed is not None
            return self._intent(refreshed)

    def commit_change(
        self,
        change_id: str,
        *,
        assurance: Mapping[str, Any],
    ) -> DocumentChangeRecord:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM document_change_records WHERE change_id=?", (change_id,)
            ).fetchone()
            if existing is not None:
                return self._record(existing)
            row = conn.execute(
                "SELECT * FROM document_change_intents WHERE change_id=?", (change_id,)
            ).fetchone()
            if row is None:
                raise KeyError("document_change_not_found")
            intent = self._intent(row)
            if intent.state != "materialized" or None in (
                intent.result_snapshot_sha256,
                intent.result_structured_head_sha256,
                intent.result_projection_sha256,
                intent.result_update_sha256,
                intent.operation_manifest_sha256,
                intent.protocol_version,
                intent.runtime_version,
                intent.schema_version,
            ):
                raise ChangeConflict()
            committed = _now()
            conn.execute(
                "INSERT INTO document_change_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent.change_id, intent.binding_id, intent.operation_kind,
                    intent.store_id, intent.document_id, intent.source_ref,
                    intent.source_representation_id, intent.source_content_sha256,
                    intent.exact_copied_text_sha256, intent.base_snapshot_sha256,
                    intent.base_structured_head_sha256, intent.base_generation_sha256,
                    intent.result_snapshot_sha256, intent.result_structured_head_sha256,
                    intent.result_projection_sha256, intent.result_update_sha256,
                    intent.selector_json, intent.actors_json, _json(assurance),
                    intent.protocol_version, intent.runtime_version, intent.schema_version,
                    intent.operation_manifest_sha256, committed,
                ),
            )
            conn.execute(
                "UPDATE document_change_intents SET state='committed',committed_at=? "
                "WHERE change_id=?", (committed, change_id)
            )
            record = conn.execute(
                "SELECT * FROM document_change_records WHERE change_id=?", (change_id,)
            ).fetchone()
            assert record is not None
            return self._record(record)

    def get_change(self, change_id: str) -> DocumentChangeRecord | None:
        _identifier(change_id, "change_id")
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM document_change_records WHERE change_id=?", (change_id,)
            ).fetchone()
        return None if row is None else self._record(row)

    def changes_for_binding(
        self, binding_id: str, *, limit: int = 100
    ) -> tuple[DocumentChangeRecord, ...]:
        _identifier(binding_id, "binding_id")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM document_change_records WHERE binding_id=? "
                "ORDER BY committed_at DESC,change_id DESC LIMIT ?",
                (binding_id, max(1, min(limit, 1000))),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def incomplete_changes(self) -> tuple[PreparedDocumentChange, ...]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM document_change_intents WHERE state IN ('prepared','materialized') "
                "ORDER BY prepared_at,change_id"
            ).fetchall()
        return tuple(self._intent(row) for row in rows)

    def intent_for_idempotency(self, idempotency_key: str) -> PreparedDocumentChange | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM document_change_intents WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return None if row is None else self._intent(row)

    def exact_source_change_for_consumer(
        self, consumer_id: str
    ) -> DocumentChangeRecord | None:
        """Resolve a source-usage consumer to its exact committed change.

        Journal migration/source-change consumers use their opaque operation
        ID as the suffix of the document-change idempotency key.  Redaction
        routing needs this read-only identity join when no capture-entry
        reverse mirror exists; it must not guess from source text or paths.
        """

        if not consumer_id or len(consumer_id) > 512:
            raise ValueError("consumer_id is invalid")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT r.*,i.idempotency_key AS source_consumer_key "
                "FROM document_change_records AS r "
                "JOIN document_change_intents AS i ON i.change_id=r.change_id "
                "WHERE r.operation_kind='exact_source_copy' "
                "AND (i.idempotency_key=? OR substr(i.idempotency_key,?)=?) "
                "ORDER BY r.committed_at DESC,r.change_id DESC",
                (consumer_id, -(len(consumer_id) + 1), f":{consumer_id}"),
            ).fetchall()
        if len(rows) > 1:
            raise BindingConflict()
        if not rows:
            return None
        values = dict(rows[0])
        values.pop("source_consumer_key", None)
        return DocumentChangeRecord(**values)

    def projection_cursor(self, binding_id: str) -> ProjectionCursor | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM document_projection_cursors WHERE binding_id=?", (binding_id,)
            ).fetchone()
        return None if row is None else ProjectionCursor(**dict(row))

    def initialize_projection_base(
        self,
        binding_id: str,
        *,
        content_authority_epoch: int,
        section_sha256: str,
        file_sha256: str,
    ) -> ProjectionCursor:
        _digest(section_sha256, "section")
        _digest(file_sha256, "file")
        with self.transaction() as conn:
            binding = conn.execute(
                "SELECT projection_mode FROM domain_document_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if binding is None:
                raise KeyError("binding_not_found")
            if binding["projection_mode"] == "none":
                raise ChangeConflict()
            row = conn.execute(
                "SELECT * FROM document_projection_cursors WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise KeyError("projection_cursor_not_found")
            current = ProjectionCursor(**dict(row))
            if current.content_authority_epoch != content_authority_epoch:
                raise ChangeConflict()
            if current.section_sha256 is not None:
                if (
                    current.section_sha256 != section_sha256
                    or current.file_sha256 != file_sha256
                ):
                    raise ChangeConflict()
                return current
            conn.execute(
                "UPDATE document_projection_cursors SET section_sha256=?,file_sha256=?,"
                "updated_at=? WHERE binding_id=?",
                (section_sha256, file_sha256, _now(), binding_id),
            )
            refreshed = conn.execute(
                "SELECT * FROM document_projection_cursors WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            assert refreshed is not None
            return ProjectionCursor(**dict(refreshed))

    def prepare_projection(
        self,
        *,
        binding_id: str,
        content_authority_epoch: int,
        document_head_sha256: str,
        expected_section_sha256: str | None,
        result_section_sha256: str,
        result_projection_sha256: str,
    ) -> str:
        document_head_sha256 = _digest(document_head_sha256, "document head")
        if expected_section_sha256 is not None:
            expected_section_sha256 = _digest(expected_section_sha256, "expected section")
        result_section_sha256 = _digest(result_section_sha256, "result section")
        result_projection_sha256 = _digest(result_projection_sha256, "result projection")
        key = f"{binding_id}:{content_authority_epoch}:{document_head_sha256}"
        projection_id = hashlib.sha256(key.encode()).hexdigest()[:32]
        with self.transaction() as conn:
            binding = conn.execute(
                "SELECT * FROM domain_document_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if (
                binding is None
                or binding["lifecycle"] != "current"
                or binding["content_authority"] != "co_work"
                or binding["projection_mode"] == "none"
                or int(binding["content_authority_epoch"]) != content_authority_epoch
            ):
                raise ChangeConflict()
            row = conn.execute(
                "SELECT * FROM document_projection_intents WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is not None:
                if (
                    row["expected_section_sha256"] != expected_section_sha256
                    or row["result_section_sha256"] != result_section_sha256
                    or row["result_projection_sha256"] != result_projection_sha256
                ):
                    raise ChangeConflict()
                return str(row["projection_id"])
            conn.execute(
                "INSERT INTO document_projection_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    projection_id, key, binding_id, content_authority_epoch,
                    document_head_sha256, expected_section_sha256,
                    result_section_sha256, result_projection_sha256,
                    "prepared", None, _now(), None,
                ),
            )
        return projection_id

    def commit_projection(
        self,
        projection_id: str,
        *,
        base_file_sha256: str,
        result_file_sha256: str,
        result_section_sha256: str,
    ) -> ProjectionCursor:
        with self.transaction() as conn:
            intent = conn.execute(
                "SELECT * FROM document_projection_intents WHERE projection_id=?",
                (projection_id,),
            ).fetchone()
            if intent is None:
                raise KeyError("projection_not_found")
            binding = conn.execute(
                "SELECT lifecycle,content_authority,content_authority_epoch,projection_mode "
                "FROM domain_document_bindings WHERE binding_id=?",
                (intent["binding_id"],),
            ).fetchone()
            if (
                intent["state"] not in {"prepared", "committed"}
                or binding is None
                or binding["lifecycle"] != "current"
                or binding["content_authority"] != "co_work"
                or binding["projection_mode"] == "none"
                or int(binding["content_authority_epoch"])
                != int(intent["content_authority_epoch"])
            ):
                raise ChangeConflict()
            receipt = conn.execute(
                "SELECT * FROM document_projection_receipts WHERE projection_id=?",
                (projection_id,),
            ).fetchone()
            base_file_sha256 = _digest(base_file_sha256, "base file")
            result_file_sha256 = _digest(result_file_sha256, "result file")
            result_section_sha256 = _digest(result_section_sha256, "result section")
            if result_section_sha256 != intent["result_section_sha256"]:
                raise ChangeConflict()
            if receipt is not None and (
                receipt["base_file_sha256"] != base_file_sha256
                or receipt["result_file_sha256"] != result_file_sha256
                or receipt["result_section_sha256"] != result_section_sha256
            ):
                raise ChangeConflict()
            if receipt is None:
                committed = _now()
                conn.execute(
                    "INSERT INTO document_projection_receipts VALUES(?,?,?,?,?,?,?,?)",
                    (
                        projection_id, intent["binding_id"], intent["content_authority_epoch"],
                        intent["document_head_sha256"],
                        base_file_sha256,
                        result_file_sha256,
                        result_section_sha256, committed,
                    ),
                )
                conn.execute(
                    "UPDATE document_projection_intents SET state='committed',committed_at=? "
                    "WHERE projection_id=?", (committed, projection_id)
                )
            conn.execute(
                "INSERT INTO document_projection_cursors VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(binding_id) DO UPDATE SET "
                "content_authority_epoch=excluded.content_authority_epoch,"
                "document_head_sha256=excluded.document_head_sha256,"
                "section_sha256=excluded.section_sha256,file_sha256=excluded.file_sha256,"
                "status='committed',divergence_source_ref=NULL,updated_at=excluded.updated_at",
                (
                    intent["binding_id"], intent["content_authority_epoch"],
                    intent["document_head_sha256"], result_section_sha256,
                    result_file_sha256, "committed", None, _now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM document_projection_cursors WHERE binding_id=?",
                (intent["binding_id"],),
            ).fetchone()
            assert row is not None
            return ProjectionCursor(**dict(row))

    def pause_diverged(
        self,
        binding_id: str,
        *,
        divergence_source_ref: str,
    ) -> ProjectionCursor:
        with self.transaction() as conn:
            binding = conn.execute(
                "SELECT projection_mode FROM domain_document_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if binding is None:
                raise KeyError("binding_not_found")
            if binding["projection_mode"] == "none":
                raise ChangeConflict()
            cursor = conn.execute(
                "SELECT * FROM document_projection_cursors WHERE binding_id=?", (binding_id,)
            ).fetchone()
            if cursor is None:
                raise KeyError("projection_cursor_not_found")
            conn.execute(
                "UPDATE document_projection_cursors SET status='paused_diverged',"
                "divergence_source_ref=?,updated_at=? WHERE binding_id=?",
                (divergence_source_ref, _now(), binding_id),
            )
            row = conn.execute(
                "SELECT * FROM document_projection_cursors WHERE binding_id=?", (binding_id,)
            ).fetchone()
            assert row is not None
            return ProjectionCursor(**dict(row))

    def export_bundle(self) -> dict[str, Any]:
        tables = (
            "domain_document_bindings", "document_change_intents",
            "document_change_records", "document_projection_cursors",
            "document_projection_intents", "document_projection_receipts",
        )
        with self.connection() as conn:
            return {
                "schema": "work-buddy-document-causality-export/v1",
                "schema_version": _SCHEMA_VERSION,
                "tables": {
                    table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in tables
                },
            }

    @staticmethod
    def _bundle_sha256(bundle: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                bundle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def export_recovery_bundle(self, *, store_id: str) -> dict[str, Any]:
        """Bind a portable causality payload to one permanent Truth store ID."""

        _identifier(store_id, "store_id")
        payload = self.export_bundle()
        return {
            "schema": "wb.truth-store-document-causality-recovery/v1",
            "store_id": store_id,
            "payload_sha256": self._bundle_sha256(payload),
            "payload": payload,
        }

    @staticmethod
    def _normalize_export_bundle(
        bundle: Mapping[str, Any],
        *,
        error_code: str,
    ) -> dict[str, Any]:
        """Upgrade portable v1 rows without invalidating their signed envelope.

        Schema version 1 pre-dates explicit projection policy.  Those backups
        remain restorable: Journal bindings used managed sections and every
        other legacy binding used managed files.  The caller must validate an
        enclosing recovery digest *before* invoking this normalizer.
        """

        if (
            not isinstance(bundle, Mapping)
            or bundle.get("schema") != "work-buddy-document-causality-export/v1"
            or bundle.get("schema_version") not in {1, _SCHEMA_VERSION}
            or not isinstance(bundle.get("tables"), Mapping)
        ):
            raise DocumentCausalityError(error_code)
        version = int(bundle["schema_version"])
        normalized_tables: dict[str, list[dict[str, Any]]] = {}
        tables = bundle["tables"]
        assert isinstance(tables, Mapping)
        for table, rows in tables.items():
            if not isinstance(table, str) or not isinstance(rows, list):
                raise DocumentCausalityError(error_code)
            normalized_rows: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    raise DocumentCausalityError(error_code)
                record = dict(row)
                if version == 1 and table == "domain_document_bindings":
                    if "projection_mode" in record:
                        raise DocumentCausalityError(error_code)
                    record["projection_mode"] = (
                        "managed_section"
                        if record.get("domain_namespace") == "journal"
                        else "managed_file"
                    )
                normalized_rows.append(record)
            normalized_tables[table] = normalized_rows
        return {
            "schema": "work-buddy-document-causality-export/v1",
            "schema_version": _SCHEMA_VERSION,
            "tables": normalized_tables,
        }

    @classmethod
    def validate_recovery_bundle(
        cls,
        bundle: Mapping[str, Any],
        *,
        expected_store_id: str,
        expected_document_ids: set[str] | None = None,
    ) -> Mapping[str, Any]:
        """Validate envelope identity and every directly store-bound row."""

        _identifier(expected_store_id, "expected_store_id")
        if (
            not isinstance(bundle, Mapping)
            or set(bundle) != {"schema", "store_id", "payload_sha256", "payload"}
            or bundle.get("schema")
            != "wb.truth-store-document-causality-recovery/v1"
            or not isinstance(bundle.get("payload"), Mapping)
        ):
            raise DocumentCausalityError("invalid_document_causality_recovery_bundle")
        if bundle.get("store_id") != expected_store_id:
            raise DocumentCausalityError(
                "document_causality_recovery_store_identity_mismatch"
            )
        raw_payload = bundle["payload"]
        assert isinstance(raw_payload, Mapping)
        if bundle.get("payload_sha256") != cls._bundle_sha256(raw_payload):
            raise DocumentCausalityError("document_causality_recovery_digest_mismatch")
        payload = cls._normalize_export_bundle(
            raw_payload,
            error_code="invalid_document_causality_recovery_bundle",
        )
        tables = payload["tables"]
        assert isinstance(tables, Mapping)
        expected_tables = {
            "domain_document_bindings",
            "document_change_intents",
            "document_change_records",
            "document_projection_cursors",
            "document_projection_intents",
            "document_projection_receipts",
        }
        if set(tables) != expected_tables or any(
            not isinstance(rows, list) for rows in tables.values()
        ):
            raise DocumentCausalityError("invalid_document_causality_recovery_bundle")
        for table in (
            "domain_document_bindings",
            "document_change_intents",
            "document_change_records",
        ):
            rows = tables.get(table, [])
            if not isinstance(rows, list) or any(
                not isinstance(row, Mapping)
                or row.get("store_id") != expected_store_id
                for row in rows
            ):
                raise DocumentCausalityError(
                    "document_causality_recovery_store_identity_mismatch"
                )
            if expected_document_ids is not None and any(
                row.get("document_id") not in expected_document_ids for row in rows
            ):
                raise DocumentCausalityError(
                    "document_causality_recovery_document_identity_mismatch"
                )
        return payload

    def import_recovery_bundle(
        self,
        bundle: Mapping[str, Any],
        *,
        expected_store_id: str,
        expected_document_ids: set[str] | None = None,
    ) -> None:
        """Import into a clean causality DB after envelope/identity validation."""

        payload = self.validate_recovery_bundle(
            bundle,
            expected_store_id=expected_store_id,
            expected_document_ids=expected_document_ids,
        )
        tables = payload["tables"]
        assert isinstance(tables, Mapping)
        order = (
            "domain_document_bindings",
            "document_change_intents",
            "document_change_records",
            "document_projection_cursors",
            "document_projection_intents",
            "document_projection_receipts",
        )
        # Clean-target proof, strict inserts, FK validation, and row-count
        # validation share one transaction. Any malformed or duplicate row
        # therefore leaves the recovery target empty rather than half imported.
        with self.transaction() as conn:
            for table in order:
                if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                    raise DocumentCausalityError(
                        "document_causality_recovery_target_not_empty"
                    )
            for table in order:
                rows = tables.get(table, [])
                if not isinstance(rows, list):
                    raise DocumentCausalityError(
                        "invalid_document_causality_recovery_bundle"
                    )
                columns = [
                    str(row[1])
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                ]
                for record in rows:
                    if not isinstance(record, Mapping) or set(record) != set(columns):
                        raise DocumentCausalityError(
                            "invalid_document_causality_recovery_bundle"
                        )
                    placeholders = ",".join("?" for _ in columns)
                    try:
                        conn.execute(
                            f"INSERT INTO {table} ({','.join(columns)}) "
                            f"VALUES({placeholders})",
                            tuple(record[column] for column in columns),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise DocumentCausalityError(
                            "invalid_document_causality_recovery_bundle"
                        ) from exc
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise DocumentCausalityError(
                    "document_causality_recovery_foreign_key_failure"
                )
            for table, rows in tables.items():
                if not isinstance(rows, list):
                    raise DocumentCausalityError(
                        "invalid_document_causality_recovery_bundle"
                    )
                count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if count != len(rows):
                    raise DocumentCausalityError(
                        "document_causality_recovery_row_count_mismatch"
                    )

    def import_bundle(self, bundle: Mapping[str, Any]) -> None:
        normalized = self._normalize_export_bundle(
            bundle,
            error_code="invalid_document_causality_export",
        )
        tables = normalized["tables"]
        assert isinstance(tables, Mapping)
        order = (
            "domain_document_bindings", "document_change_intents",
            "document_change_records", "document_projection_cursors",
            "document_projection_intents", "document_projection_receipts",
        )
        with self.transaction() as conn:
            for table in order:
                rows = tables.get(table, [])
                if not isinstance(rows, list):
                    raise DocumentCausalityError("invalid_document_causality_export")
                columns = [
                    str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                ]
                for record in rows:
                    if not isinstance(record, Mapping) or set(record) != set(columns):
                        raise DocumentCausalityError("invalid_document_causality_export")
                    placeholders = ",".join("?" for _ in columns)
                    conn.execute(
                        f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES({placeholders})",
                        tuple(record[column] for column in columns),
                    )


__all__ = [
    "BindingConflict",
    "ChangeConflict",
    "DocumentCausalityError",
    "DocumentCausalityStore",
    "DocumentChangeRecord",
    "DomainDocumentBinding",
    "PreparedDocumentChange",
    "ProjectionCursor",
]
