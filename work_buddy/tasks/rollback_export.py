"""Guarded reverse export from native Tasks into a staged legacy recovery set.

This module is deliberately dormant operator tooling.  ``prepare`` never
writes the native task database or an installed legacy tree: it reads one
SQLite snapshot, projects the current Co-work document heads, and atomically
publishes a *new* staging directory containing:

* a legacy task tree (master/archive lists, note files, and verified assets),
* a database whose schema is exactly the historical v11 shape, and
* hash manifests, downgrade/exception details, and native-only supplements.

Installing either staged target and changing traffic are separate, explicitly
confirmed cohort operations.  ``register_prepared_rollback`` is the narrow
integration point for :meth:`TaskMigrationLedger.prepare_rollback`; it
re-verifies the staging set and source snapshot immediately before asking the
ledger to fence writes.  ``complete_prepared_rollback`` is the resumable,
receipt-journaled installer and commits the higher rollback epoch only after
both staged targets and every Co-work-to-domain binding transition verify.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat as stat_module
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping, Sequence
from urllib.parse import quote

from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.protocol import structured_head_sha256
from work_buddy.truth import documents as truth_documents
from work_buddy.truth import ydoc_store

from .documents import TaskDocumentStoreManager, project_live_markdown
from .errors import TaskAuthorityUnavailable
from .migrations import (
    LEGACY_SCHEMA_VERSION,
    TASK_MIGRATIONS,
    _m001_bootstrap_v11,
)
from .runtime import (
    AUTHORITY_LATCH_SCHEMA,
    activation_authority_latch_path,
    authority_epoch,
    clear_pending_authority_latch,
)


ROLLBACK_EXPORT_CONFIRMATION = "STAGE NATIVE TASKS FOR LEGACY ROLLBACK"
RECEIPT_SCHEMA = "wb.task-rollback-export/v1"
TREE_MANIFEST_SCHEMA = "wb.task-rollback-tree-manifest/v1"
EXCEPTION_REPORT_SCHEMA = "wb.task-rollback-exceptions/v1"
SUPPLEMENT_SCHEMA = "wb.task-rollback-native-supplement/v1"
STOP_RECEIPT_SCHEMA = "wb.native-task-process-stop/v1"
INSTALL_JOURNAL_SCHEMA = "wb.task-rollback-install-journal/v1"

_RECEIPT_FILE = "rollback-export-receipt.json"
_TREE_MANIFEST_FILE = "legacy-tree-manifest.json"
_EXCEPTIONS_FILE = "rollback-exceptions.json"
_SUPPLEMENT_FILE = "native-supplement.json"
_LEGACY_TREE_DIR = "legacy-tree"
_LEGACY_DB_FILE = "task_metadata.v11.db"
_INSTALL_JOURNAL_FILE = "rollback-install-journal.json"

_ROLLBACK_EPOCH_RE = re.compile(r"^rollback:(\d+)$")
_NATIVE_EPOCH_RE = re.compile(r"^native:(\d+)$")
_LEGACY_TASK_ID_RE = re.compile(r"^t-[0-9a-f]+$", re.IGNORECASE)
_NOTE_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_LOCAL_FILE_TOKEN_RE = re.compile(r"wb-local-file:([A-Za-z0-9_.:-]+)")
_UNREPRESENTABLE_DESCRIPTION_RE = re.compile(r"[\r\n]|\[\[|#\S|[🆔📅✅🔽🔼⏫]")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_LEGACY_TASK_COLUMNS = (
    "task_id",
    "state",
    "urgency",
    "complexity",
    "contract",
    "note_uuid",
    "snooze_until",
    "created_at",
    "updated_at",
    "completed_at",
    "archived_at",
    "task_kind",
    "density",
    "outcome_text",
    "next_action_text",
    "definition_of_done",
    "creation_effort",
    "user_involvement",
    "creation_provenance",
    "has_deadline",
    "deadline_date",
    "has_dependency",
    "dependency_hint",
    "description",
    "risk_profile_json",
    "automation_tier_achievable",
    "last_actor",
    "agent_required_contexts",
    "user_required_contexts",
    "required_contexts_source",
    "current_action_item_id",
    "deleted_at",
    "created_by_session",
)
_LEGACY_HISTORY_COLUMNS = (
    "id",
    "task_id",
    "old_state",
    "new_state",
    "changed_at",
    "reason",
)
_LEGACY_SESSION_COLUMNS = ("id", "task_id", "session_id", "assigned_at")
_LEGACY_TAG_COLUMNS = ("task_id", "tag", "is_namespace")
_LEGACY_ACTION_COLUMNS = (
    "id",
    "task_id",
    "sequence",
    "description",
    "state",
    "risk_profile_json",
    "agent_required_contexts",
    "user_required_contexts",
    "definition_of_done",
    "authorship",
    "completed_at",
    "handoff_package_path",
    "created_at",
    "updated_at",
    "deleted_at",
)
_LEGACY_LWW_COLUMNS = (
    "id",
    "table_name",
    "row_pk",
    "field",
    "ts",
    "actor",
    "process",
    "from_surface",
    "to_surface",
)


class RollbackExportError(RuntimeError):
    """Stable, structured base error for reverse-export operators."""

    code = "task_rollback_export_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class RollbackExportBlocked(RollbackExportError):
    """The requested export cannot be represented or safely staged."""

    code = "task_rollback_export_blocked"


class RollbackExportVerificationError(RollbackExportError):
    """A staged artifact no longer matches its durable receipt."""

    code = "task_rollback_export_verification_failed"


@dataclass(frozen=True, slots=True)
class DateConflictResolution:
    """Explicit lossy choice for one distinct due/deadline pair."""

    use: Literal["due_date", "deadline_date"]
    reason: str

    def __post_init__(self) -> None:
        if self.use not in {"due_date", "deadline_date"}:
            raise ValueError("use must be 'due_date' or 'deadline_date'")
        if not self.reason.strip():
            raise ValueError("a non-empty resolution reason is required")


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    schema_version: int
    authority_epoch: str
    collection_revision: int
    process_generation: int
    rollback_fence: bool
    cowork_task_store_id: str | None
    cutover_receipt_id: str | None
    cohort: Mapping[str, Any]
    tasks: tuple[Mapping[str, Any], ...]
    tags: tuple[Mapping[str, Any], ...]
    history: tuple[Mapping[str, Any], ...]
    sessions: tuple[Mapping[str, Any], ...]
    actions: tuple[Mapping[str, Any], ...]
    lww_meta: tuple[Mapping[str, Any], ...]
    document_links: tuple[Mapping[str, Any], ...]
    recovered_documents: tuple[Mapping[str, Any], ...]
    local_file_roots: tuple[Mapping[str, Any], ...]
    local_file_links: tuple[Mapping[str, Any], ...]
    mutation_receipts: tuple[Mapping[str, Any], ...]
    event_outbox: tuple[Mapping[str, Any], ...]
    sync_status: Mapping[str, Any] | None
    database_snapshot_sha256: str
    content_snapshot_sha256: str


DocumentReader = Callable[[Mapping[str, Any]], str]
DocumentHeadReader = Callable[[Mapping[str, Any]], str]
MaintenanceVerifier = Callable[
    [Path, Mapping[str, Any], str, int], Mapping[str, Any]
]
Failpoint = Callable[[str], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_scalar(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes_sha256": _bytes_sha256(value), "$byte_length": len(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _quoted_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


_CONTROL_TABLES = {
    "task_system_state",
    "task_migration_cohorts",
    "task_migration_receipts",
}


def _sqlite_snapshot_payload(
    connection: sqlite3.Connection,
    *,
    exclude_tables: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    """Return a deterministic logical image of every selected SQLite object.

    Physical database bytes can change because of WAL checkpoints or page
    layout.  The rollback guard instead hashes schema objects plus every value
    in every user table, including multiplicity.  This is stable across those
    physical rewrites while still detecting any logical row or schema change.
    """

    objects = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": row[3],
        }
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        if str(row[2]) not in exclude_tables and str(row[1]) not in exclude_tables
    ]
    tables: list[Mapping[str, Any]] = []
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if str(row[0]) not in exclude_tables
    ]
    for name in names:
        cursor = connection.execute(f"SELECT * FROM {_quoted_identifier(name)}")
        columns = [str(item[0]) for item in cursor.description or ()]
        encoded_rows = [
            [_json_scalar(value) for value in row]
            for row in cursor.fetchall()
        ]
        encoded_rows.sort(key=_canonical_json)
        tables.append({"name": name, "columns": columns, "rows": encoded_rows})
    return {
        "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "schema": objects,
        "tables": tables,
    }


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_bytes(
        path,
        (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        ),
    )


def _replace_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably replace one small receipt file without an in-place write."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_relative_path(value: str, *, label: str) -> PurePosixPath:
    candidate = PurePosixPath(str(value).replace("\\", "/"))
    if (
        not str(candidate)
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise RollbackExportBlocked(
            f"{label} is not a safe relative path.",
            details={"path": value},
        )
    return candidate


def _relative_posix_path(target: PurePosixPath, *, from_directory: PurePosixPath) -> str:
    target_parts = target.parts
    base_parts = from_directory.parts
    common = 0
    while (
        common < len(target_parts)
        and common < len(base_parts)
        and target_parts[common].casefold() == base_parts[common].casefold()
    ):
        common += 1
    parts = ("..",) * (len(base_parts) - common) + target_parts[common:]
    if not parts:
        raise RollbackExportBlocked("A local asset cannot resolve to a directory.")
    return PurePosixPath(*parts).as_posix()


def _paths_collide(left: PurePosixPath, right: PurePosixPath) -> bool:
    a = tuple(part.casefold() for part in left.parts)
    b = tuple(part.casefold() for part in right.parts)
    shorter = min(len(a), len(b))
    return a[:shorter] == b[:shorter]


def _is_link_like(path: Path) -> bool:
    """Treat Windows reparse points (including junctions) like symlinks."""

    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return bool(
        attributes & int(getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )


def _row_dicts(rows: Sequence[sqlite3.Row]) -> tuple[Mapping[str, Any], ...]:
    return tuple(dict(row) for row in rows)


def _select_all(
    conn: sqlite3.Connection,
    table: str,
    *,
    order_by: str,
) -> tuple[Mapping[str, Any], ...]:
    return _row_dicts(conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall())


def _legacy_date(value: str | None, *, task_id: str, field: str) -> str | None:
    if not value:
        return None
    candidate = str(value)[:10]
    try:
        date.fromisoformat(candidate)
    except ValueError as exc:
        raise RollbackExportBlocked(
            f"Task {task_id} has an invalid {field}.",
            details={"task_id": task_id, field: value},
        ) from exc
    return candidate


def _epoch_number(value: str) -> int:
    match = re.fullmatch(r"(?:native|rollback):(\d+)", value)
    if match is None:
        raise RollbackExportBlocked(
            "The task authority epoch is not rollback-compatible.",
            details={"authority_epoch": value},
        )
    return int(match.group(1))


class ReverseLegacyTaskExportOperator:
    """Create and verify an isolated legacy rollback staging set."""

    def __init__(
        self,
        *,
        source_db_path: str | Path,
        staging_root: str | Path,
        document_reader: DocumentReader | None = None,
        document_head_reader: DocumentHeadReader | None = None,
        document_stores: TaskDocumentStoreManager | None = None,
        local_asset_root: str | Path | None = None,
        maintenance_verifier: MaintenanceVerifier | None = None,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.source_db_path = Path(source_db_path).expanduser().resolve()
        self.staging_root = Path(staging_root).expanduser().resolve()
        self.document_reader = document_reader
        self.document_head_reader = document_head_reader
        self.document_stores = document_stores
        self.local_asset_root = (
            Path(local_asset_root).expanduser().resolve()
            if local_asset_root is not None
            else None
        )
        self.maintenance_verifier = maintenance_verifier
        self.clock = clock

    def status(self, *, cohort_id: str | None = None) -> dict[str, Any]:
        """Return source readiness and staged-artifact verification status."""

        source: dict[str, Any]
        try:
            snapshot = self._read_snapshot(cohort_id=cohort_id, require_active=False)
            latch_evidence = (
                self._native_authority_latch_evidence(snapshot)
                if _NATIVE_EPOCH_RE.fullmatch(snapshot.authority_epoch)
                else None
            )
            conflicts = [
                {
                    "task_id": str(task["task_id"]),
                    "due_date": task.get("due_date"),
                    "deadline_date": task.get("deadline_date"),
                }
                for task in snapshot.tasks
                if task.get("due_date")
                and task.get("deadline_date")
                and task.get("due_date") != task.get("deadline_date")
            ]
            source = {
                "available": True,
                "schema_version": snapshot.schema_version,
                "authority_epoch": snapshot.authority_epoch,
                "collection_revision": snapshot.collection_revision,
                "process_generation": snapshot.process_generation,
                "rollback_fence": snapshot.rollback_fence,
                "cohort_id": snapshot.cohort.get("cohort_id"),
                "cohort_state": snapshot.cohort.get("state"),
                "counts": {
                    "tasks": len(snapshot.tasks),
                    "tags": len(snapshot.tags),
                    "history": len(snapshot.history),
                    "documents": len(snapshot.document_links)
                    + len(snapshot.recovered_documents),
                    "local_files": len(snapshot.local_file_links),
                },
                "unresolved_date_conflicts": conflicts,
                "authority_latch": latch_evidence,
            }
        except (OSError, sqlite3.Error, RollbackExportError) as exc:
            source = {
                "available": False,
                "error": str(exc),
                "code": getattr(exc, "code", "task_rollback_source_unavailable"),
            }

        if not self.staging_root.exists():
            staging: dict[str, Any] = {"state": "absent"}
        else:
            try:
                receipt = self.verify_staging()
                staging = {
                    "state": "verified",
                    "receipt_id": receipt["receipt_id"],
                    "cohort_id": receipt["cohort_id"],
                    "rollback_authority_epoch": receipt["rollback_authority_epoch"],
                    "counts": receipt["counts"],
                }
            except RollbackExportError as exc:
                staging = {
                    "state": "invalid",
                    "error": str(exc),
                    "code": exc.code,
                    "details": exc.details,
                }
        return {"source": source, "staging": staging}

    def prepare(
        self,
        *,
        cohort_id: str,
        rollback_authority_epoch: str,
        maintenance_receipt: str | Path,
        expected_process_generation: int,
        date_resolutions: Mapping[str, DateConflictResolution] | None = None,
        confirmation: str,
    ) -> dict[str, Any]:
        """Build a verified staging set without mutating either live target."""

        self._require_confirmation(confirmation)
        maintenance = self._verify_maintenance_receipt(
            maintenance_receipt,
            cohort_id=cohort_id,
            expected_process_generation=expected_process_generation,
        )
        if _ROLLBACK_EPOCH_RE.fullmatch(rollback_authority_epoch) is None:
            raise RollbackExportBlocked(
                "Rollback export requires a rollback:<integer> authority epoch."
            )
        resolutions = dict(date_resolutions or {})

        if self.staging_root.exists():
            existing = self.verify_staging()
            expected = {
                "cohort_id": cohort_id,
                "rollback_authority_epoch": rollback_authority_epoch,
                "source_process_generation": int(expected_process_generation),
            }
            if (
                all(existing.get(key) == value for key, value in expected.items())
                and existing.get("maintenance_stop_receipt") == maintenance
            ):
                return existing
            raise RollbackExportBlocked(
                "The staging target already contains a different verified export.",
                details={"staging_root": str(self.staging_root)},
            )

        snapshot = self._read_snapshot(cohort_id=cohort_id, require_active=True)
        self._native_authority_latch_evidence(snapshot)
        self._validate_prepare_request(
            snapshot,
            rollback_authority_epoch=rollback_authority_epoch,
            expected_process_generation=expected_process_generation,
        )
        date_mappings, downgrades = self._resolve_dates(snapshot.tasks, resolutions)
        self._validate_legacy_identity(snapshot)

        parent = self.staging_root.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{self.staging_root.name}.tmp-", dir=parent)
        ).resolve()
        try:
            receipt = self._write_staging(
                temporary,
                snapshot=snapshot,
                cohort_id=cohort_id,
                rollback_authority_epoch=rollback_authority_epoch,
                maintenance_receipt=maintenance,
                date_mappings=date_mappings,
                downgrades=downgrades,
                resolutions=resolutions,
            )
            self._require_source_unchanged(snapshot, receipt)
            self._verify_staging_at(temporary, expected_receipt=receipt)
            os.replace(temporary, self.staging_root)
            _fsync_directory(parent)
        except BaseException:
            if temporary.exists() and temporary.parent == parent:
                shutil.rmtree(temporary)
            raise
        return self.verify_staging()

    def verify_staging(self) -> dict[str, Any]:
        """Recompute every staged hash, schema check, and semantic count."""

        try:
            return self._verify_staging_at(self.staging_root)
        except OSError as exc:
            raise RollbackExportVerificationError(
                "A staged rollback artifact is missing or unreadable."
            ) from exc

    def rehearsal_evidence(self) -> dict[str, Any]:
        """Return the portable, hash-bound gate payload for a restore rehearsal.

        This method is read-only.  A production cutover gate can point at the
        returned receipt file and require every listed source/document/catalog,
        tree, and v11 database digest without installing or switching traffic.
        """

        receipt = self.verify_staging()
        path = self.staging_root / _RECEIPT_FILE
        length, digest = _file_digest(path)
        return {
            "schema": "wb.task-rollback-rehearsal-evidence/v1",
            "receipt_file": str(path),
            "receipt_byte_length": length,
            "receipt_sha256": digest,
            "receipt_id": receipt["receipt_id"],
            "cohort_id": receipt["cohort_id"],
            "source_inventory_sha256": receipt["source_inventory_sha256"],
            "source_manifest_sha256": receipt["source_manifest_sha256"],
            "source_cowork_task_store_id": receipt[
                "source_cowork_task_store_id"
            ],
            "source_snapshot_sha256": receipt["source_snapshot_sha256"],
            "source_database_snapshot_sha256": receipt[
                "source_database_snapshot_sha256"
            ],
            "document_heads_sha256": receipt["document_heads_sha256"],
            "source_local_file_catalog_sha256": receipt[
                "source_local_file_catalog_sha256"
            ],
            "source_authority_latch_sha256": receipt[
                "source_authority_latch_sha256"
            ],
            "staged_tree_sha256": receipt["staged_tree_sha256"],
            "legacy_database_sha256": receipt["legacy_database_sha256"],
            "legacy_database_semantic_sha256": receipt[
                "legacy_database_semantic_sha256"
            ],
            "counts": dict(receipt["counts"]),
        }

    def register_prepared_rollback(
        self,
        *,
        ledger: Any,
        actor: str,
        session_id: str | None,
        confirmation: str,
    ) -> dict[str, Any]:
        """Re-verify and pass the receipt to ``TaskMigrationLedger.prepare_rollback``.

        This call does not install files or change the active epoch.  It only
        asks the supplied ledger to enter its durable ``rollback_prepared``
        state and arm the mutation fence.
        """

        self._require_confirmation(confirmation)
        receipt = self.verify_staging()
        ledger_path = Path(ledger.store.path).expanduser().resolve()
        if ledger_path != self.source_db_path:
            raise RollbackExportBlocked(
                "The migration ledger is attached to a different task database."
            )
        snapshot = self._read_snapshot(
            cohort_id=str(receipt["cohort_id"]),
            require_active=True,
        )
        self._require_receipt_source(snapshot, receipt)
        self._revalidate_maintenance(receipt)
        result = ledger.prepare_rollback(
            str(receipt["cohort_id"]),
            rollback_authority_epoch=str(receipt["rollback_authority_epoch"]),
            reverse_export_receipt=receipt,
            actor=actor,
            session_id=session_id,
        )
        return {"cohort": result, "reverse_export_receipt": receipt}

    def complete_prepared_rollback(
        self,
        *,
        ledger: Any,
        causality: Any,
        legacy_tree_target: str | Path,
        legacy_database_target: str | Path,
        actor: str,
        session_id: str | None,
        confirmation: str,
        failpoint: Failpoint | None = None,
    ) -> dict[str, Any]:
        """Resume a fenced rollback install and commit its epoch last.

        This operator never starts a process.  It rolls the exact current Task
        binding set back to domain authority, installs the tree and v11 DB with
        independent same-directory swaps, verifies both, and only then commits
        the higher rollback epoch in the still-native control database.

        ``legacy_database_target`` must intentionally differ from the native
        control DB.  A compatible legacy configuration/restart is an external
        operator responsibility after this method returns.
        """

        self._require_confirmation(confirmation)
        receipt = self.verify_staging()
        ledger_path = Path(ledger.store.path).expanduser().resolve()
        if ledger_path != self.source_db_path:
            raise RollbackExportBlocked(
                "The migration ledger is attached to a different task database."
            )
        tree_target = Path(legacy_tree_target).expanduser().resolve()
        database_target = Path(legacy_database_target).expanduser().resolve()
        if database_target == self.source_db_path:
            raise RollbackExportBlocked(
                "The legacy v11 install must not overwrite the native control ledger."
            )
        if self._paths_overlap(tree_target, self.staging_root):
            raise RollbackExportBlocked("The legacy tree target overlaps rollback staging.")
        if database_target == self.staging_root or self.staging_root in database_target.parents:
            raise RollbackExportBlocked("The legacy database target overlaps rollback staging.")
        if self.source_db_path == tree_target or tree_target in self.source_db_path.parents:
            raise RollbackExportBlocked(
                "The legacy tree target contains the native control database."
            )
        if database_target == tree_target or tree_target in database_target.parents:
            raise RollbackExportBlocked(
                "The legacy database target must not be inside the legacy tree target."
            )
        self._require_unlinked_target(tree_target, label="legacy tree target")
        self._require_unlinked_target(database_target, label="legacy database target")
        if self.local_asset_root is not None:
            frozen = self.local_asset_root.resolve()
            if (
                tree_target == frozen
                or tree_target in frozen.parents
                or frozen in tree_target.parents
            ):
                raise RollbackExportBlocked(
                    "The legacy tree target overlaps immutable frozen evidence."
                )
            if (
                database_target == frozen
                or database_target in frozen.parents
                or frozen in database_target.parents
            ):
                raise RollbackExportBlocked(
                    "The legacy database target overlaps immutable frozen evidence."
                )

        journal_exists = self.install_journal_path.exists()
        # A new install must still prove the pre-commit maintenance state
        # before even creating its durable journal.  An existing journal is
        # loaded first because the native authority transaction may already
        # have committed even though the response/journal update was lost.
        if not journal_exists:
            self._revalidate_maintenance(receipt)
        journal = self._load_or_create_install_journal(
            receipt,
            causality=causality,
            tree_target=tree_target,
            database_target=database_target,
        )
        if not journal.get("authority_committed"):
            recovered = self._recover_committed_authority(receipt, journal)
            if recovered is not None:
                journal["authority_committed"] = recovered
                self._save_install_journal(journal)
        if journal.get("authority_committed"):
            self._clear_native_authority_latch(receipt, journal)
            self._verify_completed_authority(journal, receipt)
            self._verify_binding_plan(causality, journal, completed=True)
            self._verify_installed_targets(tree_target, database_target, receipt)
            return journal

        if journal_exists:
            self._revalidate_maintenance(receipt)
        self._require_prepared_source(receipt)
        self._transition_binding_plan(
            causality,
            journal,
            failpoint=failpoint,
        )
        self._revalidate_maintenance(receipt)
        self._install_tree_target(
            tree_target,
            receipt,
            journal,
            failpoint=failpoint,
        )
        self._install_database_target(
            database_target,
            receipt,
            journal,
            failpoint=failpoint,
        )
        self._verify_binding_plan(causality, journal, completed=True)
        self._verify_installed_targets(tree_target, database_target, receipt)
        journal["targets_verified"] = True
        self._save_install_journal(journal)
        self._trip(failpoint, "targets_verified")
        self._revalidate_maintenance(receipt)
        self._require_prepared_source(receipt)
        self._trip(failpoint, "before_final_target_verification")
        self._verify_installed_targets(tree_target, database_target, receipt)
        authority = self._commit_rollback_authority(
            ledger,
            receipt,
            journal,
            actor=actor,
            session_id=session_id,
        )
        self._trip(failpoint, "authority_db_committed")
        journal["authority_committed"] = authority
        self._save_install_journal(journal)
        self._clear_native_authority_latch(receipt, journal)
        self._trip(failpoint, "authority_latch_cleared")
        self._verify_completed_authority(journal, receipt)
        self._trip(failpoint, "authority_committed")
        return journal

    @property
    def install_journal_path(self) -> Path:
        return self.staging_root / _INSTALL_JOURNAL_FILE

    @staticmethod
    def _trip(failpoint: Failpoint | None, name: str) -> None:
        if failpoint is not None:
            failpoint(name)

    @staticmethod
    def _require_unlinked_target(path: Path, *, label: str) -> None:
        cursor = path
        while True:
            if cursor.exists() and _is_link_like(cursor):
                raise RollbackExportBlocked(
                    f"The {label} contains a linked or reparse-point component.",
                    details={"path": str(cursor)},
                )
            if cursor == cursor.parent:
                break
            cursor = cursor.parent

    @staticmethod
    def _paths_overlap(left: Path, right: Path) -> bool:
        return left == right or left in right.parents or right in left.parents

    def _load_or_create_install_journal(
        self,
        receipt: Mapping[str, Any],
        *,
        causality: Any,
        tree_target: Path,
        database_target: Path,
    ) -> dict[str, Any]:
        path = self.install_journal_path
        if path.exists():
            try:
                journal = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RollbackExportVerificationError(
                    "The rollback install journal is malformed."
                ) from exc
            if not isinstance(journal, dict) or journal.get("schema") != INSTALL_JOURNAL_SCHEMA:
                raise RollbackExportVerificationError(
                    "The rollback install journal schema is unsupported."
                )
            claimed = str(journal.pop("journal_sha256", ""))
            actual = _canonical_sha256(journal)
            journal["journal_sha256"] = claimed
            if claimed != actual:
                raise RollbackExportVerificationError(
                    "The rollback install journal was modified."
                )
            expected = {
                "receipt_id": receipt["receipt_id"],
                "tree_target": str(tree_target),
                "database_target": str(database_target),
            }
            observed = {key: journal.get(key) for key in expected}
            if observed != expected:
                raise RollbackExportBlocked(
                    "The rollback install resume targets do not match its journal.",
                    details={"expected": expected, "observed": observed},
                )
            return journal

        expected_current = {
            str(item["binding_id"]): item
            for item in receipt.get("source_documents") or []
            if item.get("binding_id") and item.get("lifecycle") == "current"
        }
        expected_retired = {
            str(item["binding_id"]): item
            for item in receipt.get("source_documents") or []
            if item.get("binding_id") and item.get("lifecycle") == "retired"
        }
        actual_current, actual_retired = self._task_binding_partitions(causality)
        if set(actual_current) != set(expected_current):
            raise RollbackExportBlocked(
                "The current task binding set changed before rollback.",
                details={
                    "missing": sorted(set(expected_current) - set(actual_current)),
                    "extra": sorted(set(actual_current) - set(expected_current)),
                },
            )
        domain_revision = (
            f"{receipt['rollback_authority_epoch']}:{receipt['staged_tree_sha256']}"
        )
        plan: list[dict[str, Any]] = []
        for binding_id, item in sorted(expected_current.items()):
            binding = actual_current[binding_id]
            if (
                binding.lifecycle != "current"
                or binding.content_authority != "co_work"
                or binding.store_id != str(item["store_id"])
                or binding.document_id != str(item["document_id"])
            ):
                raise RollbackExportBlocked(
                    "A current task binding no longer matches the reverse export.",
                    details={"binding_id": binding_id},
                )
            plan.append(
                {
                    "binding_id": binding_id,
                    "store_id": binding.store_id,
                    "document_id": binding.document_id,
                    "expected_epoch": int(binding.content_authority_epoch),
                    "rollback_epoch": int(binding.content_authority_epoch) + 1,
                    "domain_revision": domain_revision,
                    "completed": False,
                }
            )
        if set(actual_retired) != set(expected_retired):
            raise RollbackExportBlocked(
                "The retired task binding set changed before rollback.",
                details={
                    "missing": sorted(set(expected_retired) - set(actual_retired)),
                    "extra": sorted(set(actual_retired) - set(expected_retired)),
                },
            )
        retired_plan: list[dict[str, Any]] = []
        for binding_id, item in sorted(expected_retired.items()):
            binding = actual_retired[binding_id]
            if (
                binding.store_id != str(item["store_id"])
                or binding.document_id != str(item["document_id"])
            ):
                raise RollbackExportBlocked(
                    "A retired task binding no longer matches its exported document.",
                    details={"binding_id": binding_id},
                )
            retired_plan.append(
                {
                    "binding_id": binding_id,
                    "store_id": binding.store_id,
                    "document_id": binding.document_id,
                    "lifecycle": binding.lifecycle,
                    "content_authority": binding.content_authority,
                    "content_authority_epoch": int(binding.content_authority_epoch),
                    "domain_revision": binding.domain_revision,
                }
            )
        journal = {
            "schema": INSTALL_JOURNAL_SCHEMA,
            "receipt_id": receipt["receipt_id"],
            "cohort_id": receipt["cohort_id"],
            "rollback_authority_epoch": receipt["rollback_authority_epoch"],
            "tree_target": str(tree_target),
            "database_target": str(database_target),
            "binding_plan": plan,
            "retired_binding_plan": retired_plan,
            "tree_install": None,
            "database_install": None,
            "targets_verified": False,
            "authority_committed": None,
            "created_at": self.clock(),
        }
        self._save_install_journal(journal)
        return journal

    def _save_install_journal(self, journal: dict[str, Any]) -> None:
        journal.pop("journal_sha256", None)
        journal["journal_sha256"] = _canonical_sha256(journal)
        _replace_json(self.install_journal_path, journal)

    def _transition_binding_plan(
        self,
        causality: Any,
        journal: dict[str, Any],
        *,
        failpoint: Failpoint | None,
    ) -> None:
        for item in journal["binding_plan"]:
            binding_id = str(item["binding_id"])
            current = causality.get_binding(binding_id)
            if current is None:
                raise RollbackExportBlocked(
                    "A planned task binding disappeared.",
                    details={"binding_id": binding_id},
                )
            expected_epoch = int(item["expected_epoch"])
            if current.content_authority == "co_work":
                if int(current.content_authority_epoch) != expected_epoch:
                    raise RollbackExportBlocked(
                        "A task binding authority epoch changed before rollback.",
                        details={"binding_id": binding_id},
                    )
                current = causality.rollback_to_domain(
                    binding_id,
                    domain_revision=str(item["domain_revision"]),
                    expected_epoch=expected_epoch,
                )
            if (
                current.content_authority != "domain"
                or int(current.content_authority_epoch) != int(item["rollback_epoch"])
                or current.domain_revision != str(item["domain_revision"])
            ):
                raise RollbackExportBlocked(
                    "A task binding did not reach the exact rollback epoch.",
                    details={"binding_id": binding_id},
                )
            item["completed"] = True
            self._save_install_journal(journal)
            self._trip(failpoint, f"binding:{binding_id}")
        self._verify_binding_plan(causality, journal, completed=True)

    def _verify_binding_plan(
        self,
        causality: Any,
        journal: Mapping[str, Any],
        *,
        completed: bool,
    ) -> None:
        planned = {str(item["binding_id"]): item for item in journal["binding_plan"]}
        actual, retired = self._task_binding_partitions(causality)
        if set(actual) != set(planned):
            raise RollbackExportBlocked("The task binding set changed during rollback.")
        for binding_id, item in planned.items():
            binding = actual[binding_id]
            if completed and (
                binding.content_authority != "domain"
                or int(binding.content_authority_epoch) != int(item["rollback_epoch"])
                or binding.domain_revision != str(item["domain_revision"])
            ):
                raise RollbackExportBlocked(
                    "A rolled-back task binding failed final verification.",
                    details={"binding_id": binding_id},
                )
        retired_plan = {
            str(item["binding_id"]): item
            for item in journal.get("retired_binding_plan") or []
        }
        expected_retired = set(retired_plan)
        if set(retired) != expected_retired:
            raise RollbackExportBlocked(
                "The retired task binding set changed during rollback.",
                details={
                    "missing": sorted(expected_retired - set(retired)),
                    "extra": sorted(set(retired) - expected_retired),
                },
            )
        for binding_id, item in retired_plan.items():
            binding = retired[binding_id]
            observed = {
                "binding_id": binding.binding_id,
                "store_id": binding.store_id,
                "document_id": binding.document_id,
                "lifecycle": binding.lifecycle,
                "content_authority": binding.content_authority,
                "content_authority_epoch": int(binding.content_authority_epoch),
                "domain_revision": binding.domain_revision,
            }
            if observed != dict(item):
                raise RollbackExportBlocked(
                    "A retired task binding changed during rollback.",
                    details={"expected": dict(item), "observed": observed},
                )

    @staticmethod
    def _task_binding_partitions(
        causality: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if hasattr(causality, "list_all_bindings"):
            bindings = tuple(causality.list_all_bindings())
        elif hasattr(causality, "connection") and hasattr(causality, "_binding"):
            with causality.connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM domain_document_bindings "
                    "WHERE domain_namespace='tasks' AND domain_kind='task_knowledge' "
                    "AND role='task_knowledge' ORDER BY binding_id"
                ).fetchall()
            bindings = tuple(causality._binding(row) for row in rows)
        else:
            bindings = tuple(causality.list_bindings())
        selected = [
            binding
            for binding in bindings
            if binding.domain_namespace == "tasks"
            and binding.domain_kind == "task_knowledge"
            and binding.role == "task_knowledge"
        ]
        return (
            {
                binding.binding_id: binding
                for binding in selected
                if binding.lifecycle == "current"
            },
            {
                binding.binding_id: binding
                for binding in selected
                if binding.lifecycle == "retired"
            },
        )

    def _require_prepared_source(self, receipt: Mapping[str, Any]) -> None:
        snapshot = self._read_snapshot(
            cohort_id=str(receipt["cohort_id"]),
            require_active=False,
        )
        if (
            str(snapshot.cohort.get("state")) != "rollback_prepared"
            or snapshot.authority_epoch != receipt["source_authority_epoch"]
            or not snapshot.rollback_fence
            or snapshot.process_generation
            != int(receipt["source_process_generation"])
            or snapshot.collection_revision
            != int(receipt["source_collection_revision"])
            or snapshot.content_snapshot_sha256
            != receipt["source_content_snapshot_sha256"]
        ):
            raise RollbackExportBlocked(
                "The native task source is not the exact fenced rollback snapshot."
            )
        _content, documents = self._read_documents(self._document_rows(snapshot))
        local_files = self._local_file_catalog(snapshot, verify_assets=True)
        latch = self._native_authority_latch_evidence(snapshot)
        if (
            list(documents) != list(receipt.get("source_documents") or [])
            or _canonical_sha256(documents) != receipt.get("document_heads_sha256")
            or local_files != receipt.get("source_local_file_catalog")
            or _canonical_sha256(local_files)
            != receipt.get("source_local_file_catalog_sha256")
            or latch != receipt.get("source_authority_latch")
            or _canonical_sha256(latch)
            != receipt.get("source_authority_latch_sha256")
        ):
            raise RollbackExportBlocked(
                "Co-work heads or local files changed after rollback preparation."
            )

    def _install_tree_target(
        self,
        target: Path,
        receipt: Mapping[str, Any],
        journal: dict[str, Any],
        *,
        failpoint: Failpoint | None,
    ) -> None:
        desired = str(receipt["staged_tree_sha256"])
        if target.is_dir() and not target.is_symlink():
            try:
                if self._tree_manifest(target)["tree_sha256"] == desired:
                    journal["tree_install"] = {
                        "state": "verified",
                        "target": str(target),
                        "tree_sha256": desired,
                        "backup": (
                            journal.get("tree_install") or {}
                        ).get("backup"),
                    }
                    self._save_install_journal(journal)
                    return
            except RollbackExportError:
                pass
        if target.exists() and (target.is_symlink() or not target.is_dir()):
            raise RollbackExportBlocked("The legacy tree target is not a regular directory.")
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = target.with_name(
            f".{target.name}.pre-rollback.{str(receipt['receipt_id'])}"
        )
        temporary = target.with_name(
            f".{target.name}.installing.{str(receipt['receipt_id'])}"
        )
        state = dict(journal.get("tree_install") or {})
        if not state:
            state = {
                "state": "planned",
                "target": str(target),
                "backup": str(backup),
                "temporary": str(temporary),
                "tree_sha256": desired,
                "had_target": target.exists(),
            }
            journal["tree_install"] = state
            self._save_install_journal(journal)
        if temporary.exists():
            if temporary.is_symlink() or not temporary.is_dir():
                raise RollbackExportBlocked("The rollback tree temporary is unsafe.")
            if self._tree_manifest(temporary)["tree_sha256"] != desired:
                raise RollbackExportBlocked("The rollback tree temporary was modified.")
        elif not target.exists() or self._tree_manifest(target)["tree_sha256"] != desired:
            shutil.copytree(self.staging_root / _LEGACY_TREE_DIR, temporary)
            if self._tree_manifest(temporary)["tree_sha256"] != desired:
                raise RollbackExportVerificationError(
                    "The copied rollback tree differs from staging."
                )
        state["state"] = "prepared"
        self._save_install_journal(journal)
        self._trip(failpoint, "tree_prepared")
        if target.exists() and self._tree_manifest(target)["tree_sha256"] != desired:
            if backup.exists():
                raise RollbackExportBlocked(
                    "The legacy tree backup target already exists."
                )
            os.replace(target, backup)
            _fsync_directory(target.parent)
        state["state"] = "backup_moved"
        self._save_install_journal(journal)
        self._trip(failpoint, "tree_backup_moved")
        if not target.exists():
            if not temporary.exists():
                raise RollbackExportBlocked("The prepared rollback tree is unavailable.")
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        if self._tree_manifest(target)["tree_sha256"] != desired:
            raise RollbackExportVerificationError(
                "The installed rollback tree differs from staging."
            )
        state["state"] = "verified"
        self._save_install_journal(journal)
        self._trip(failpoint, "tree_installed")

    def _install_database_target(
        self,
        target: Path,
        receipt: Mapping[str, Any],
        journal: dict[str, Any],
        *,
        failpoint: Failpoint | None,
    ) -> None:
        desired = str(receipt["legacy_database_sha256"])
        if target.is_file() and not target.is_symlink():
            _length, digest = _file_digest(target)
            if digest == desired:
                self._verify_v11_database(target)
                journal["database_install"] = {
                    "state": "verified",
                    "target": str(target),
                    "sha256": desired,
                    "backup": (
                        journal.get("database_install") or {}
                    ).get("backup"),
                }
                self._save_install_journal(journal)
                return
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise RollbackExportBlocked("The legacy database target is not a regular file.")
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = target.with_name(
            f".{target.name}.pre-rollback.{str(receipt['receipt_id'])}"
        )
        temporary = target.with_name(
            f".{target.name}.installing.{str(receipt['receipt_id'])}"
        )
        state = dict(journal.get("database_install") or {})
        if not state:
            state = {
                "state": "planned",
                "target": str(target),
                "backup": str(backup),
                "temporary": str(temporary),
                "sha256": desired,
                "had_target": target.exists(),
            }
            journal["database_install"] = state
            self._save_install_journal(journal)
        if temporary.exists():
            if temporary.is_symlink() or not temporary.is_file():
                raise RollbackExportBlocked("The rollback DB temporary is unsafe.")
            _length, digest = _file_digest(temporary)
            if digest != desired:
                raise RollbackExportBlocked("The rollback DB temporary was modified.")
        elif not target.exists() or _file_digest(target)[1] != desired:
            with (self.staging_root / _LEGACY_DB_FILE).open("rb") as source:
                _write_bytes(temporary, source.read())
            if _file_digest(temporary)[1] != desired:
                raise RollbackExportVerificationError(
                    "The copied rollback database differs from staging."
                )
            self._verify_v11_database(temporary)
        state["state"] = "prepared"
        self._save_install_journal(journal)
        self._trip(failpoint, "database_prepared")
        if target.exists() and _file_digest(target)[1] != desired:
            if backup.exists():
                raise RollbackExportBlocked(
                    "The legacy database backup target already exists."
                )
            os.replace(target, backup)
            _fsync_directory(target.parent)
        state["state"] = "backup_moved"
        self._save_install_journal(journal)
        self._trip(failpoint, "database_backup_moved")
        if not target.exists():
            if not temporary.exists():
                raise RollbackExportBlocked("The prepared rollback DB is unavailable.")
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        if _file_digest(target)[1] != desired:
            raise RollbackExportVerificationError(
                "The installed rollback database differs from staging."
            )
        self._verify_v11_database(target)
        state["state"] = "verified"
        self._save_install_journal(journal)
        self._trip(failpoint, "database_installed")

    def _verify_installed_targets(
        self,
        tree_target: Path,
        database_target: Path,
        receipt: Mapping[str, Any],
    ) -> None:
        if _is_link_like(tree_target) or _is_link_like(database_target):
            raise RollbackExportVerificationError(
                "A rollback install target became a filesystem link."
            )
        if self._tree_manifest(tree_target)["tree_sha256"] != receipt[
            "staged_tree_sha256"
        ]:
            raise RollbackExportVerificationError("The installed legacy tree changed.")
        length, digest = _file_digest(database_target)
        if (
            length != int(receipt["legacy_database_byte_length"])
            or digest != receipt["legacy_database_sha256"]
        ):
            raise RollbackExportVerificationError("The installed legacy DB changed.")
        semantic = self._verify_v11_database(database_target)
        if semantic["semantic_sha256"] != receipt["legacy_database_semantic_sha256"]:
            raise RollbackExportVerificationError(
                "The installed legacy DB semantic digest changed."
            )

    def _commit_rollback_authority(
        self,
        ledger: Any,
        receipt: Mapping[str, Any],
        journal: Mapping[str, Any],
        *,
        actor: str,
        session_id: str | None,
    ) -> Mapping[str, Any]:
        now = self.clock()
        journal_sha = str(journal.get("journal_sha256") or "")
        binding_transitions = [
            {
                "binding_id": str(item["binding_id"]),
                "before_authority": "co_work",
                "before_epoch": int(item["expected_epoch"]),
                "after_authority": "domain",
                "after_epoch": int(item["rollback_epoch"]),
                "domain_revision": str(item["domain_revision"]),
                "result": "applied",
            }
            for item in journal["binding_plan"]
        ]
        payload = {
            "reverse_export_receipt_id": receipt["receipt_id"],
            "rollback_authority_epoch": receipt["rollback_authority_epoch"],
            "tree_target": journal["tree_target"],
            "database_target": journal["database_target"],
            "staged_tree_sha256": receipt["staged_tree_sha256"],
            "legacy_database_sha256": receipt["legacy_database_sha256"],
            "install_journal_sha256": journal_sha,
            "binding_transitions_sha256": _canonical_sha256(binding_transitions),
        }
        control_receipt_id = "tmig_" + _canonical_sha256(
            {
                "cohort_id": receipt["cohort_id"],
                "operation": "rollback_activate",
                "payload": payload,
            }
        )[:32]
        with ledger.store.transaction() as conn:
            cohort = conn.execute(
                "SELECT * FROM task_migration_cohorts WHERE cohort_id=?",
                (receipt["cohort_id"],),
            ).fetchone()
            system = conn.execute(
                "SELECT * FROM task_system_state WHERE id=1"
            ).fetchone()
            if cohort is None or system is None:
                raise RollbackExportBlocked("Rollback control state is incomplete.")
            if (
                str(system["authority_epoch"])
                == str(receipt["rollback_authority_epoch"])
                and str(cohort["state"]) == "rolled_back"
                and str(system["cutover_receipt_id"]) == control_receipt_id
            ):
                self._record_or_verify_rollback_transitions(
                    conn,
                    receipt,
                    binding_transitions,
                    applied_at=now,
                    allow_insert=False,
                )
                return {
                    "receipt_id": control_receipt_id,
                    "authority_epoch": receipt["rollback_authority_epoch"],
                    "process_generation": int(system["process_generation"]),
                    "committed_at": str(cohort["updated_at"]),
                    "install_journal_sha256": journal_sha,
                    "binding_transitions_sha256": payload[
                        "binding_transitions_sha256"
                    ],
                }
            if (
                str(cohort["state"]) != "rollback_prepared"
                or str(cohort["rollback_authority_epoch"])
                != str(receipt["rollback_authority_epoch"])
                or str(system["authority_epoch"])
                != str(receipt["source_authority_epoch"])
                or not bool(system["rollback_fence"])
                or int(system["process_generation"])
                != int(receipt["source_process_generation"])
            ):
                raise RollbackExportBlocked(
                    "The rollback authority compare-and-swap no longer matches."
                )
            self._record_or_verify_rollback_transitions(
                conn,
                receipt,
                binding_transitions,
                applied_at=now,
                allow_insert=True,
            )
            encoded = _canonical_json(payload)
            conn.execute(
                "INSERT OR IGNORE INTO task_migration_receipts "
                "(receipt_id, cohort_id, operation, status, payload_sha256, "
                "payload_json, actor, session_id, created_at, completed_at) "
                "VALUES (?, ?, 'rollback_activate', 'completed', ?, ?, ?, ?, ?, ?)",
                (
                    control_receipt_id,
                    receipt["cohort_id"],
                    _bytes_sha256(encoded.encode("utf-8")),
                    encoded,
                    actor,
                    session_id,
                    now,
                    now,
                ),
            )
            stored_control = conn.execute(
                "SELECT cohort_id, operation, status, payload_sha256, payload_json "
                "FROM task_migration_receipts WHERE receipt_id=?",
                (control_receipt_id,),
            ).fetchone()
            expected_control = {
                "cohort_id": str(receipt["cohort_id"]),
                "operation": "rollback_activate",
                "status": "completed",
                "payload_sha256": _bytes_sha256(encoded.encode("utf-8")),
                "payload_json": encoded,
            }
            if stored_control is None or {
                key: str(stored_control[key]) for key in expected_control
            } != expected_control:
                raise RollbackExportBlocked(
                    "The rollback activation receipt ID is already bound to other data."
                )
            conn.execute(
                "UPDATE task_migration_cohorts SET state='rolled_back', updated_at=? "
                "WHERE cohort_id=? AND state='rollback_prepared'",
                (now, receipt["cohort_id"]),
            )
            changed = conn.execute(
                "UPDATE task_system_state SET authority_epoch=?, "
                "cutover_receipt_id=?, rollback_fence=0, "
                "process_generation=process_generation+1, updated_at=? "
                "WHERE id=1 AND authority_epoch=? AND rollback_fence=1 "
                "AND process_generation=?",
                (
                    receipt["rollback_authority_epoch"],
                    control_receipt_id,
                    now,
                    receipt["source_authority_epoch"],
                    int(receipt["source_process_generation"]),
                ),
            ).rowcount
            if changed != 1:
                raise RollbackExportBlocked("The rollback authority switch lost its CAS.")
        return {
            "receipt_id": control_receipt_id,
            "authority_epoch": receipt["rollback_authority_epoch"],
            "process_generation": int(receipt["source_process_generation"]) + 1,
            "committed_at": now,
            "install_journal_sha256": journal_sha,
            "binding_transitions_sha256": payload[
                "binding_transitions_sha256"
            ],
        }

    @staticmethod
    def _record_or_verify_rollback_transitions(
        connection: sqlite3.Connection,
        receipt: Mapping[str, Any],
        transitions: Sequence[Mapping[str, Any]],
        *,
        applied_at: str,
        allow_insert: bool,
    ) -> None:
        expected_ids = {str(item["binding_id"]) for item in transitions}
        existing_rows = connection.execute(
            "SELECT * FROM task_migration_binding_transitions "
            "WHERE cohort_id=? AND direction='rollback_to_domain' "
            "ORDER BY binding_id",
            (receipt["cohort_id"],),
        ).fetchall()
        existing = {str(row["binding_id"]): row for row in existing_rows}
        if set(existing) - expected_ids:
            raise RollbackExportBlocked(
                "Unexpected rollback binding-transition evidence already exists."
            )
        for item in transitions:
            binding_id = str(item["binding_id"])
            row = existing.get(binding_id)
            if row is None:
                if not allow_insert:
                    raise RollbackExportBlocked(
                        "A committed rollback binding transition is missing."
                    )
                connection.execute(
                    "INSERT INTO task_migration_binding_transitions "
                    "(cohort_id, binding_id, direction, before_authority, "
                    "before_epoch, after_authority, after_epoch, domain_revision, "
                    "result, applied_at) VALUES (?, ?, 'rollback_to_domain', "
                    "?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt["cohort_id"],
                        binding_id,
                        item["before_authority"],
                        item["before_epoch"],
                        item["after_authority"],
                        item["after_epoch"],
                        item["domain_revision"],
                        item["result"],
                        applied_at,
                    ),
                )
                continue
            observed = {
                key: row[key]
                for key in (
                    "binding_id",
                    "before_authority",
                    "before_epoch",
                    "after_authority",
                    "after_epoch",
                    "domain_revision",
                    "result",
                )
            }
            expected = dict(item)
            if observed != expected or not str(row["applied_at"] or ""):
                raise RollbackExportBlocked(
                    "A rollback binding-transition receipt was modified.",
                    details={"binding_id": binding_id},
                )

    def _verify_completed_authority(
        self,
        journal: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> None:
        snapshot = self._read_snapshot(
            cohort_id=str(receipt["cohort_id"]),
            require_active=False,
        )
        authority = journal.get("authority_committed") or {}
        if (
            snapshot.authority_epoch != receipt["rollback_authority_epoch"]
            or snapshot.rollback_fence
            or snapshot.process_generation
            != int(receipt["source_process_generation"]) + 1
            or str(snapshot.cohort.get("state")) != "rolled_back"
            or authority.get("authority_epoch") != receipt["rollback_authority_epoch"]
            or snapshot.cutover_receipt_id != authority.get("receipt_id")
            or int(authority.get("process_generation", -1))
            != snapshot.process_generation
        ):
            raise RollbackExportBlocked(
                "The completed rollback authority receipt no longer matches control state."
            )
        transitions = [
            {
                "binding_id": str(item["binding_id"]),
                "before_authority": "co_work",
                "before_epoch": int(item["expected_epoch"]),
                "after_authority": "domain",
                "after_epoch": int(item["rollback_epoch"]),
                "domain_revision": str(item["domain_revision"]),
                "result": "applied",
            }
            for item in journal["binding_plan"]
        ]
        connection = self._open_source()
        try:
            self._record_or_verify_rollback_transitions(
                connection,
                receipt,
                transitions,
                applied_at="",
                allow_insert=False,
            )
            transition_sha = _canonical_sha256(transitions)
            expected_payload = {
                "reverse_export_receipt_id": receipt["receipt_id"],
                "rollback_authority_epoch": receipt["rollback_authority_epoch"],
                "tree_target": journal["tree_target"],
                "database_target": journal["database_target"],
                "staged_tree_sha256": receipt["staged_tree_sha256"],
                "legacy_database_sha256": receipt["legacy_database_sha256"],
                "install_journal_sha256": authority.get(
                    "install_journal_sha256"
                ),
                "binding_transitions_sha256": transition_sha,
            }
            encoded = _canonical_json(expected_payload)
            expected_id = "tmig_" + _canonical_sha256(
                {
                    "cohort_id": receipt["cohort_id"],
                    "operation": "rollback_activate",
                    "payload": expected_payload,
                }
            )[:32]
            control = connection.execute(
                "SELECT cohort_id, operation, status, payload_sha256, payload_json "
                "FROM task_migration_receipts WHERE receipt_id=?",
                (authority.get("receipt_id"),),
            ).fetchone()
            if (
                authority.get("receipt_id") != expected_id
                or authority.get("binding_transitions_sha256") != transition_sha
                or control is None
                or str(control["cohort_id"]) != str(receipt["cohort_id"])
                or str(control["operation"]) != "rollback_activate"
                or str(control["status"]) != "completed"
                or str(control["payload_sha256"])
                != _bytes_sha256(encoded.encode("utf-8"))
                or str(control["payload_json"]) != encoded
            ):
                raise RollbackExportBlocked(
                    "The completed rollback control receipt was modified."
                )
        finally:
            connection.close()
        latch_path = activation_authority_latch_path(self.source_db_path).resolve()
        if latch_path.exists():
            raise RollbackExportBlocked(
                "The completed rollback still has a native-authority latch."
            )
        try:
            routed_epoch = authority_epoch(self.source_db_path)
        except TaskAuthorityUnavailable as exc:
            raise RollbackExportBlocked(
                "The completed rollback is not routable after latch release."
            ) from exc
        if routed_epoch != receipt["rollback_authority_epoch"]:
            raise RollbackExportBlocked(
                "The completed rollback router reports another authority epoch."
            )

    def _clear_native_authority_latch(
        self,
        receipt: Mapping[str, Any],
        journal: dict[str, Any],
    ) -> None:
        """Release native routing only after the rollback CAS is durable.

        A crash between the SQLite CAS and this exact-match unlink is safely
        unavailable because SQLite and the native latch disagree.  Replaying
        completion recovers the control receipt, repeats this idempotent unlink,
        and verifies the rollback router before returning.
        """

        path = activation_authority_latch_path(self.source_db_path).resolve()
        if path.exists():
            if not path.is_file() or path.is_symlink() or _is_link_like(path):
                raise RollbackExportBlocked(
                    "The native task authority latch became unsafe before release."
                )
            length, digest = _file_digest(path)
            source_latch = receipt["source_authority_latch"]
            if (
                length != int(source_latch["byte_length"])
                or digest != str(source_latch["file_sha256"])
            ):
                raise RollbackExportBlocked(
                    "The native task authority latch changed before release."
                )
        try:
            clear_pending_authority_latch(
                self.source_db_path,
                cohort_id=str(receipt["cohort_id"]),
                target_authority_epoch=str(receipt["source_authority_epoch"]),
            )
        except TaskAuthorityUnavailable as exc:
            raise RollbackExportBlocked(
                "The native task authority latch no longer matches this rollback."
            ) from exc
        if path.exists():
            raise RollbackExportBlocked(
                "The native task authority latch could not be released."
            )
        journal["authority_latch_cleared"] = {
            "path": str(path),
            "source_file_sha256": receipt["source_authority_latch"]["file_sha256"],
            "cleared": True,
        }
        self._save_install_journal(journal)

    def _recover_committed_authority(
        self,
        receipt: Mapping[str, Any],
        journal: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        connection = self._open_source()
        try:
            system = connection.execute(
                "SELECT authority_epoch, rollback_fence, process_generation, "
                "cutover_receipt_id, updated_at FROM task_system_state WHERE id=1"
            ).fetchone()
            cohort = connection.execute(
                "SELECT state, updated_at FROM task_migration_cohorts WHERE cohort_id=?",
                (receipt["cohort_id"],),
            ).fetchone()
            if system is None or cohort is None:
                return None
            if (
                str(system["authority_epoch"])
                != str(receipt["rollback_authority_epoch"])
                or bool(system["rollback_fence"])
                or int(system["process_generation"])
                != int(receipt["source_process_generation"]) + 1
                or str(cohort["state"]) != "rolled_back"
            ):
                return None
            control_receipt_id = str(system["cutover_receipt_id"] or "")
            control = connection.execute(
                "SELECT operation, status, payload_sha256, payload_json "
                "FROM task_migration_receipts "
                "WHERE receipt_id=? AND cohort_id=?",
                (control_receipt_id, receipt["cohort_id"]),
            ).fetchone()
            if (
                control is None
                or str(control["operation"]) != "rollback_activate"
                or str(control["status"]) != "completed"
            ):
                raise RollbackExportBlocked(
                    "Rollback authority committed without its control receipt."
                )
            encoded = str(control["payload_json"])
            try:
                payload = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise RollbackExportBlocked(
                    "The committed rollback control receipt is malformed."
                ) from exc
            transitions = [
                {
                    "binding_id": str(item["binding_id"]),
                    "before_authority": "co_work",
                    "before_epoch": int(item["expected_epoch"]),
                    "after_authority": "domain",
                    "after_epoch": int(item["rollback_epoch"]),
                    "domain_revision": str(item["domain_revision"]),
                    "result": "applied",
                }
                for item in journal["binding_plan"]
            ]
            expected_payload = {
                "reverse_export_receipt_id": receipt["receipt_id"],
                "rollback_authority_epoch": receipt["rollback_authority_epoch"],
                "tree_target": journal["tree_target"],
                "database_target": journal["database_target"],
                "staged_tree_sha256": receipt["staged_tree_sha256"],
                "legacy_database_sha256": receipt["legacy_database_sha256"],
                "install_journal_sha256": journal["journal_sha256"],
                "binding_transitions_sha256": _canonical_sha256(transitions),
            }
            expected_id = "tmig_" + _canonical_sha256(
                {
                    "cohort_id": receipt["cohort_id"],
                    "operation": "rollback_activate",
                    "payload": expected_payload,
                }
            )[:32]
            if (
                control_receipt_id != expected_id
                or payload != expected_payload
                or str(control["payload_sha256"])
                != _bytes_sha256(encoded.encode("utf-8"))
            ):
                raise RollbackExportBlocked(
                    "Rollback authority points at modified completion evidence."
                )
            self._record_or_verify_rollback_transitions(
                connection,
                receipt,
                transitions,
                applied_at="",
                allow_insert=False,
            )
            return {
                "receipt_id": control_receipt_id,
                "authority_epoch": receipt["rollback_authority_epoch"],
                "process_generation": int(system["process_generation"]),
                "committed_at": str(cohort["updated_at"]),
                "install_journal_sha256": expected_payload[
                    "install_journal_sha256"
                ],
                "binding_transitions_sha256": expected_payload[
                    "binding_transitions_sha256"
                ],
            }
        finally:
            connection.close()

    @staticmethod
    def _require_confirmation(confirmation: str) -> None:
        if confirmation != ROLLBACK_EXPORT_CONFIRMATION:
            raise RollbackExportBlocked(
                "The exact rollback-export confirmation phrase is required."
            )

    def _verify_maintenance_receipt(
        self,
        receipt_path: str | Path,
        *,
        cohort_id: str,
        expected_process_generation: int,
    ) -> dict[str, Any]:
        """Verify a hash-bound process-stop receipt and re-probe live state.

        Receipt bytes alone never prove that processes remain stopped.  The
        injected verifier is deliberately mandatory: production can bind this
        to ``NativeTaskProductionCutover._stop_receipt_evidence`` while tests
        and restore rehearsals can supply an equivalent current-state probe.
        """

        path = Path(receipt_path).expanduser().resolve()
        if not path.is_file() or path.is_symlink():
            raise RollbackExportBlocked(
                "A hash-bound process-stop receipt file is required.",
                details={"maintenance_receipt": str(path)},
            )
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RollbackExportBlocked(
                "The process-stop receipt is missing or malformed."
            ) from exc
        if not isinstance(receipt, dict) or receipt.get("schema") != STOP_RECEIPT_SCHEMA:
            raise RollbackExportBlocked("The process-stop receipt schema is unsupported.")
        expected_hash = str(receipt.get("payload_sha256") or "")
        unsigned = dict(receipt)
        unsigned.pop("payload_sha256", None)
        if (
            _SHA256_RE.fullmatch(expected_hash) is None
            or _canonical_sha256(unsigned) != expected_hash
        ):
            raise RollbackExportBlocked("The process-stop receipt was modified.")
        if str(receipt.get("cohort_id") or "") != str(cohort_id):
            raise RollbackExportBlocked(
                "The process-stop receipt belongs to another cohort."
            )
        evidence = receipt.get("evidence")
        if not isinstance(evidence, Mapping):
            raise RollbackExportBlocked("The process-stop evidence is malformed.")
        if int(evidence.get("process_generation", -1)) != int(
            expected_process_generation
        ):
            raise RollbackExportBlocked(
                "The process-stop receipt names another task process generation."
            )
        verifier = self.maintenance_verifier
        if verifier is None:
            raise RollbackExportBlocked(
                "A current process/job/retry-state verifier is required."
            )
        try:
            current = dict(
                verifier(
                    path,
                    receipt,
                    str(cohort_id),
                    int(expected_process_generation),
                )
            )
        except RollbackExportError:
            raise
        except Exception as exc:
            raise RollbackExportBlocked(
                "Current maintenance-state revalidation failed."
            ) from exc
        if not current.get("continuously_revalidated"):
            raise RollbackExportBlocked(
                "The process-stop receipt is stale because live state was not revalidated."
            )
        if int(current.get("process_generation", -1)) != int(
            expected_process_generation
        ):
            raise RollbackExportBlocked(
                "The current task process generation no longer matches the stop receipt."
            )
        returned_hash = str(
            current.get("stop_payload_sha256")
            or current.get("receipt_payload_sha256")
            or ""
        )
        if returned_hash != expected_hash:
            raise RollbackExportBlocked(
                "The current-state verifier did not validate this exact stop receipt."
            )
        length, file_sha = _file_digest(path)
        return {
            "schema": STOP_RECEIPT_SCHEMA,
            "path": str(path),
            "byte_length": length,
            "file_sha256": file_sha,
            "payload_sha256": expected_hash,
            "cohort_id": str(cohort_id),
            "process_generation": int(expected_process_generation),
            "captured_at": receipt.get("captured_at"),
            "evidence_sha256": _canonical_sha256(evidence),
            "continuously_revalidated": True,
        }

    def _revalidate_maintenance(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        stored = receipt.get("maintenance_stop_receipt")
        if not isinstance(stored, Mapping):
            raise RollbackExportBlocked(
                "The rollback export lacks verified maintenance evidence."
            )
        current = self._verify_maintenance_receipt(
            str(stored.get("path") or ""),
            cohort_id=str(receipt["cohort_id"]),
            expected_process_generation=int(receipt["source_process_generation"]),
        )
        if current != dict(stored):
            raise RollbackExportBlocked(
                "The process/job/retry stop evidence changed after export.",
                details={"expected": dict(stored), "observed": current},
            )
        return current

    def _open_source(self) -> sqlite3.Connection:
        if (
            not self.source_db_path.is_file()
            or self.source_db_path.is_symlink()
        ):
            raise RollbackExportBlocked(
                "The native task database must be a regular file."
            )
        connection = sqlite3.connect(
            self.source_db_path.as_uri() + "?mode=ro",
            uri=True,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _read_snapshot(
        self,
        *,
        cohort_id: str | None,
        require_active: bool,
    ) -> _SourceSnapshot:
        connection = self._open_source()
        try:
            connection.execute("BEGIN")
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version != TASK_MIGRATIONS.target_version:
                raise RollbackExportBlocked(
                    "The reverse exporter only accepts the task schema it was audited for.",
                    details={
                        "expected_schema_version": TASK_MIGRATIONS.target_version,
                        "actual_schema_version": schema_version,
                    },
                )
            integrity = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
            if integrity != ["ok"]:
                raise RollbackExportBlocked(
                    "The native task database failed quick_check.",
                    details={"integrity": integrity},
                )
            system_row = connection.execute(
                "SELECT * FROM task_system_state WHERE id=1"
            ).fetchone()
            collection_row = connection.execute(
                "SELECT * FROM task_collection_state WHERE id=1"
            ).fetchone()
            if system_row is None or collection_row is None:
                raise RollbackExportBlocked("Native task control state is incomplete.")

            if cohort_id is None:
                cohort_row = connection.execute(
                    "SELECT * FROM task_migration_cohorts "
                    "WHERE state IN ('active','rollback_prepared') "
                    "ORDER BY activated_at DESC, updated_at DESC LIMIT 1"
                ).fetchone()
            else:
                cohort_row = connection.execute(
                    "SELECT * FROM task_migration_cohorts WHERE cohort_id=?",
                    (cohort_id,),
                ).fetchone()
            if cohort_row is None:
                if require_active:
                    raise RollbackExportBlocked("The active migration cohort is unavailable.")
                cohort: Mapping[str, Any] = {}
            else:
                cohort = dict(cohort_row)
                if require_active and cohort.get("state") != "active":
                    raise RollbackExportBlocked(
                        "Only an active native migration cohort can be reverse-exported.",
                        details={"cohort_id": cohort.get("cohort_id"), "state": cohort.get("state")},
                    )

            database_snapshot_sha256 = _canonical_sha256(
                _sqlite_snapshot_payload(connection)
            )
            content_snapshot_sha256 = _canonical_sha256(
                _sqlite_snapshot_payload(
                    connection,
                    exclude_tables=frozenset(_CONTROL_TABLES),
                )
            )

            snapshot = _SourceSnapshot(
                schema_version=schema_version,
                authority_epoch=str(system_row["authority_epoch"]),
                collection_revision=int(collection_row["revision"]),
                process_generation=int(system_row["process_generation"]),
                rollback_fence=bool(system_row["rollback_fence"]),
                cowork_task_store_id=(
                    str(system_row["cowork_task_store_id"])
                    if system_row["cowork_task_store_id"]
                    else None
                ),
                cutover_receipt_id=(
                    str(system_row["cutover_receipt_id"])
                    if system_row["cutover_receipt_id"]
                    else None
                ),
                cohort=cohort,
                tasks=_select_all(connection, "task_metadata", order_by="task_id"),
                tags=_select_all(connection, "task_tags", order_by="task_id, tag"),
                history=_select_all(connection, "task_state_history", order_by="id"),
                sessions=_select_all(connection, "task_sessions", order_by="id"),
                actions=_select_all(connection, "task_action_items", order_by="id"),
                lww_meta=_select_all(connection, "lww_meta", order_by="id"),
                document_links=_select_all(
                    connection, "task_document_links", order_by="task_id"
                ),
                recovered_documents=_select_all(
                    connection, "recovered_task_documents", order_by="note_uuid"
                ),
                local_file_roots=_select_all(
                    connection, "task_local_file_roots", order_by="root_id"
                ),
                local_file_links=_select_all(
                    connection,
                    "task_local_file_links",
                    order_by="document_id, relative_path, link_id",
                ),
                mutation_receipts=_select_all(
                    connection, "task_mutation_receipts", order_by="created_at, receipt_id"
                ),
                event_outbox=_select_all(
                    connection, "task_event_outbox", order_by="collection_revision"
                ),
                sync_status=(
                    dict(row)
                    if (
                        row := connection.execute(
                            "SELECT * FROM task_sync_status WHERE id=1"
                        ).fetchone()
                    )
                    is not None
                    else None
                ),
                database_snapshot_sha256=database_snapshot_sha256,
                content_snapshot_sha256=content_snapshot_sha256,
            )
            connection.execute("COMMIT")
            return snapshot
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _native_authority_latch_evidence(
        self,
        snapshot: _SourceSnapshot,
    ) -> Mapping[str, Any]:
        """Verify the independent installation latch for this native snapshot."""

        path = activation_authority_latch_path(self.source_db_path).resolve()
        if not path.is_file() or path.is_symlink() or _is_link_like(path):
            raise RollbackExportBlocked(
                "The active native task store has no regular authority latch.",
                details={"authority_latch": str(path)},
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RollbackExportBlocked(
                "The native task authority latch is malformed."
            ) from exc
        if not isinstance(payload, Mapping):
            raise RollbackExportBlocked("The native task authority latch is malformed.")
        expected = {
            "schema": AUTHORITY_LATCH_SCHEMA,
            "cohort_id": str(snapshot.cohort.get("cohort_id") or ""),
            "target_authority_epoch": snapshot.authority_epoch,
            "cutover_receipt_id": str(snapshot.cutover_receipt_id or ""),
        }
        observed = {key: str(payload.get(key) or "") for key in expected}
        if observed != expected:
            raise RollbackExportBlocked(
                "The native task authority latch names another activation.",
                details={"expected": expected, "observed": observed},
            )
        try:
            routed_epoch = authority_epoch(self.source_db_path)
        except TaskAuthorityUnavailable as exc:
            raise RollbackExportBlocked(
                "The native task authority latch does not validate this database."
            ) from exc
        if routed_epoch != snapshot.authority_epoch:
            raise RollbackExportBlocked(
                "The native task router and rollback snapshot disagree."
            )
        length, digest = _file_digest(path)
        return {
            "schema": AUTHORITY_LATCH_SCHEMA,
            "path": str(path),
            "byte_length": length,
            "file_sha256": digest,
            "cohort_id": expected["cohort_id"],
            "target_authority_epoch": expected["target_authority_epoch"],
            "cutover_receipt_id": expected["cutover_receipt_id"],
        }

    @staticmethod
    def _validate_prepare_request(
        snapshot: _SourceSnapshot,
        *,
        rollback_authority_epoch: str,
        expected_process_generation: int,
    ) -> None:
        if _NATIVE_EPOCH_RE.fullmatch(snapshot.authority_epoch) is None:
            raise RollbackExportBlocked(
                "The native task epoch is not active.",
                details={"authority_epoch": snapshot.authority_epoch},
            )
        if _epoch_number(rollback_authority_epoch) <= _epoch_number(
            snapshot.authority_epoch
        ):
            raise RollbackExportBlocked(
                "Rollback must use a strictly newer task epoch."
            )
        if snapshot.process_generation != int(expected_process_generation):
            raise RollbackExportBlocked(
                "The task process generation does not match the maintenance receipt.",
                details={
                    "expected_process_generation": int(expected_process_generation),
                    "actual_process_generation": snapshot.process_generation,
                },
            )
        if str(snapshot.cohort.get("target_authority_epoch")) != snapshot.authority_epoch:
            raise RollbackExportBlocked(
                "The active cohort does not own the current task epoch."
            )
        if snapshot.cowork_task_store_id is None:
            raise RollbackExportBlocked("The active task Co-work store is not pinned.")

    @staticmethod
    def _validate_legacy_identity(snapshot: _SourceSnapshot) -> None:
        task_by_id = {str(row["task_id"]): row for row in snapshot.tasks}
        links_by_task = {str(row["task_id"]): row for row in snapshot.document_links}
        blockers: list[dict[str, Any]] = []
        for task_id, task in task_by_id.items():
            if _LEGACY_TASK_ID_RE.fullmatch(task_id) is None:
                blockers.append({"task_id": task_id, "reason": "legacy_task_id_unrepresentable"})
            description = str(task.get("description") or "").strip()
            if not description or _UNREPRESENTABLE_DESCRIPTION_RE.search(description):
                blockers.append(
                    {"task_id": task_id, "reason": "legacy_description_unrepresentable"}
                )
            note_uuid = task.get("note_uuid")
            link = links_by_task.get(task_id)
            if note_uuid:
                if _NOTE_UUID_RE.fullmatch(str(note_uuid)) is None:
                    blockers.append({"task_id": task_id, "reason": "legacy_note_uuid_invalid"})
                if link is None:
                    blockers.append({"task_id": task_id, "reason": "task_document_link_missing"})
                elif str(link["note_uuid"]).casefold() != str(note_uuid).casefold():
                    blockers.append({"task_id": task_id, "reason": "task_note_link_mismatch"})
            elif link is not None:
                blockers.append({"task_id": task_id, "reason": "task_note_uuid_missing"})
        for row in snapshot.document_links:
            if str(row["task_id"]) not in task_by_id:
                blockers.append(
                    {"task_id": row["task_id"], "reason": "orphan_document_link"}
                )
            if str(row["store_id"]) != snapshot.cowork_task_store_id:
                blockers.append(
                    {"task_id": row["task_id"], "reason": "document_store_mismatch"}
                )
        for row in snapshot.recovered_documents:
            if _NOTE_UUID_RE.fullmatch(str(row["note_uuid"])) is None:
                blockers.append(
                    {"note_uuid": row["note_uuid"], "reason": "recovered_note_uuid_invalid"}
                )
            if str(row["store_id"]) != snapshot.cowork_task_store_id:
                blockers.append(
                    {"note_uuid": row["note_uuid"], "reason": "recovered_store_mismatch"}
                )
        if blockers:
            raise RollbackExportBlocked(
                "Some native tasks cannot be represented by the legacy parser.",
                details={"blockers": blockers},
            )

    @staticmethod
    def _resolve_dates(
        tasks: Sequence[Mapping[str, Any]],
        resolutions: Mapping[str, DateConflictResolution],
    ) -> tuple[dict[str, str | None], list[dict[str, Any]]]:
        task_ids = {str(task["task_id"]) for task in tasks}
        unknown = sorted(set(resolutions) - task_ids)
        if unknown:
            raise RollbackExportBlocked(
                "Date resolutions name tasks outside the export cohort.",
                details={"unknown_task_ids": unknown},
            )
        mapped: dict[str, str | None] = {}
        downgrades: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        used_resolutions: set[str] = set()
        for task in tasks:
            task_id = str(task["task_id"])
            due = _legacy_date(task.get("due_date"), task_id=task_id, field="due_date")
            deadline = _legacy_date(
                task.get("deadline_date"), task_id=task_id, field="deadline_date"
            )
            if due and deadline and due != deadline:
                resolution = resolutions.get(task_id)
                if not isinstance(resolution, DateConflictResolution):
                    unresolved.append(
                        {"task_id": task_id, "due_date": due, "deadline_date": deadline}
                    )
                    continue
                selected = due if resolution.use == "due_date" else deadline
                used_resolutions.add(task_id)
                mapped[task_id] = selected
                downgrades.append(
                    {
                        "kind": "distinct_due_deadline_resolved",
                        "task_id": task_id,
                        "due_date": due,
                        "deadline_date": deadline,
                        "selected_field": resolution.use,
                        "legacy_date": selected,
                        "reason": resolution.reason.strip(),
                    }
                )
            elif due and deadline:
                mapped[task_id] = due
                downgrades.append(
                    {
                        "kind": "equal_due_deadline_collapsed",
                        "task_id": task_id,
                        "legacy_date": due,
                    }
                )
            else:
                mapped[task_id] = due or deadline
        if unresolved:
            raise RollbackExportBlocked(
                "Distinct due/deadline pairs require explicit per-task resolutions.",
                details={"date_conflicts": unresolved},
            )
        unused = sorted(set(resolutions) - used_resolutions)
        if unused:
            raise RollbackExportBlocked(
                "Date resolutions are accepted only for currently distinct pairs.",
                details={"unneeded_task_ids": unused},
            )
        return mapped, downgrades

    def _require_source_unchanged(
        self,
        snapshot: _SourceSnapshot,
        receipt: Mapping[str, Any],
    ) -> None:
        current = self._read_snapshot(
            cohort_id=str(snapshot.cohort["cohort_id"]),
            require_active=True,
        )
        if current.database_snapshot_sha256 != snapshot.database_snapshot_sha256:
            raise RollbackExportBlocked(
                "Native task state changed while the rollback export was being staged.",
                details={
                    "expected": snapshot.database_snapshot_sha256,
                    "observed": current.database_snapshot_sha256,
                },
            )
        self._require_receipt_source(current, receipt)

    def _require_receipt_source(
        self,
        snapshot: _SourceSnapshot,
        receipt: Mapping[str, Any],
    ) -> None:
        expected = {
            "authority_epoch": receipt["source_authority_epoch"],
            "collection_revision": int(receipt["source_collection_revision"]),
            "process_generation": int(receipt["source_process_generation"]),
            "database_snapshot_sha256": receipt["source_database_snapshot_sha256"],
            "content_snapshot_sha256": receipt["source_content_snapshot_sha256"],
        }
        observed = {
            "authority_epoch": snapshot.authority_epoch,
            "collection_revision": snapshot.collection_revision,
            "process_generation": snapshot.process_generation,
            "database_snapshot_sha256": snapshot.database_snapshot_sha256,
            "content_snapshot_sha256": snapshot.content_snapshot_sha256,
        }
        if observed != expected:
            raise RollbackExportBlocked(
                "The verified export no longer matches the native task snapshot.",
                details={"expected": expected, "observed": observed},
            )
        _content, documents = self._read_documents(self._document_rows(snapshot))
        local_files = self._local_file_catalog(snapshot, verify_assets=True)
        authority_latch = self._native_authority_latch_evidence(snapshot)
        source_snapshot_sha256 = _canonical_sha256(
            {
                "database_snapshot_sha256": snapshot.database_snapshot_sha256,
                "documents": list(documents),
                "local_file_catalog": local_files,
            }
        )
        immutable = {
            "documents": list(receipt.get("source_documents") or []),
            "document_heads_sha256": receipt.get("document_heads_sha256"),
            "local_file_catalog": receipt.get("source_local_file_catalog"),
            "local_file_catalog_sha256": receipt.get(
                "source_local_file_catalog_sha256"
            ),
            "source_snapshot_sha256": receipt.get("source_snapshot_sha256"),
            "authority_latch": receipt.get("source_authority_latch"),
            "authority_latch_sha256": receipt.get(
                "source_authority_latch_sha256"
            ),
        }
        current_immutable = {
            "documents": list(documents),
            "document_heads_sha256": _canonical_sha256(documents),
            "local_file_catalog": local_files,
            "local_file_catalog_sha256": _canonical_sha256(local_files),
            "source_snapshot_sha256": source_snapshot_sha256,
            "authority_latch": authority_latch,
            "authority_latch_sha256": _canonical_sha256(authority_latch),
        }
        if current_immutable != immutable:
            raise RollbackExportBlocked(
                "The verified export no longer matches Co-work heads or local files.",
                details={"expected": immutable, "observed": current_immutable},
            )

    def _write_staging(
        self,
        root: Path,
        *,
        snapshot: _SourceSnapshot,
        cohort_id: str,
        rollback_authority_epoch: str,
        maintenance_receipt: Mapping[str, Any],
        date_mappings: Mapping[str, str | None],
        downgrades: list[dict[str, Any]],
        resolutions: Mapping[str, DateConflictResolution],
    ) -> dict[str, Any]:
        tree = root / _LEGACY_TREE_DIR
        tree.mkdir(parents=True, exist_ok=False)
        document_rows = self._document_rows(snapshot)
        documents, document_manifest = self._read_documents(document_rows)
        asset_downgrades = self._stage_assets_and_rewrite_documents(
            tree,
            snapshot=snapshot,
            documents=documents,
        )
        downgrades.extend(asset_downgrades)

        tasks_by_id = {str(row["task_id"]): row for row in snapshot.tasks}
        tags_by_task: dict[str, list[Mapping[str, Any]]] = {
            task_id: [] for task_id in tasks_by_id
        }
        for tag in snapshot.tags:
            tags_by_task.setdefault(str(tag["task_id"]), []).append(tag)

        master: list[Mapping[str, Any]] = []
        archive: list[Mapping[str, Any]] = []
        for task in snapshot.tasks:
            if task.get("deleted_at") is not None:
                continue
            target = archive if task.get("archived_at") is not None else master
            target.append(task)
        master.sort(
            key=lambda row: (str(row.get("created_at") or ""), str(row["task_id"])),
            reverse=True,
        )
        archive.sort(
            key=lambda row: (
                str(row.get("archived_at") or ""),
                str(row.get("created_at") or ""),
                str(row["task_id"]),
            )
        )
        master_lines = [
            self._render_task_line(
                task,
                tags=tags_by_task.get(str(task["task_id"]), ()),
                legacy_date=date_mappings[str(task["task_id"])],
            )
            for task in master
        ]
        archive_lines = [
            self._render_task_line(
                task,
                tags=tags_by_task.get(str(task["task_id"]), ()),
                legacy_date=date_mappings[str(task["task_id"])],
            )
            for task in archive
        ]
        _write_bytes(
            tree / "master-task-list.md",
            ("# Master Task List\n\n" + "\n".join(master_lines) + "\n").encode("utf-8"),
        )
        _write_bytes(
            tree / "archive.md",
            ("# Archived Tasks\n\n" + "\n".join(archive_lines) + "\n").encode(
                "utf-8"
            ),
        )
        for note_uuid, document in sorted(documents.items()):
            _write_bytes(
                tree / "notes" / f"{note_uuid}.md",
                document.encode("utf-8"),
            )

        database_path = root / _LEGACY_DB_FILE
        self._write_legacy_database(
            database_path,
            snapshot=snapshot,
            date_mappings=date_mappings,
        )
        database_verification = self._verify_v11_database(database_path)

        history_enrichment = [
            {
                key: row.get(key)
                for key in (
                    "id",
                    "task_id",
                    "mutation",
                    "actor",
                    "session_id",
                    "receipt_id",
                    "task_revision",
                    "collection_revision",
                    "details_json",
                )
            }
            for row in snapshot.history
        ]
        native_task_fields = [
            {
                key: row.get(key)
                for key in (
                    "task_id",
                    "revision",
                    "due_date",
                    "deadline_date",
                    "snooze_resume_state",
                    "restored_at",
                    "legacy_import_receipt_id",
                    "summary_text",
                    "dependencies_json",
                )
            }
            for row in snapshot.tasks
        ]
        supplement = {
            "schema": SUPPLEMENT_SCHEMA,
            "cohort_id": cohort_id,
            "source_authority_epoch": snapshot.authority_epoch,
            "source_collection_revision": snapshot.collection_revision,
            "native_task_fields": native_task_fields,
            "history_enrichment": history_enrichment,
            "task_document_links": list(snapshot.document_links),
            "recovered_task_documents": list(snapshot.recovered_documents),
            "task_local_file_roots": list(snapshot.local_file_roots),
            "task_local_file_links": list(snapshot.local_file_links),
            "task_mutation_receipts": list(snapshot.mutation_receipts),
            "task_event_outbox": list(snapshot.event_outbox),
            "date_resolutions": {
                task_id: asdict(resolution)
                for task_id, resolution in sorted(resolutions.items())
            },
        }
        _write_json(root / _SUPPLEMENT_FILE, supplement)
        supplement_size, supplement_sha = _file_digest(root / _SUPPLEMENT_FILE)

        if any(task.get("state") in {"active", "waiting"} for task in snapshot.tasks):
            for task in snapshot.tasks:
                if task.get("state") in {"active", "waiting"}:
                    downgrades.append(
                        {
                            "kind": "native_attention_state_retained_in_v11",
                            "task_id": task["task_id"],
                            "state": task["state"],
                            "warning": "The historical UI may not offer this state for new edits.",
                        }
                    )
        if any(
            any(row.get(key) not in (None, "", "[]") for key in (
                "summary_text",
                "dependencies_json",
                "restored_at",
                "snooze_resume_state",
            ))
            for row in snapshot.tasks
        ):
            downgrades.append(
                {
                    "kind": "native_only_task_fields_preserved_in_supplement",
                    "supplement_file": _SUPPLEMENT_FILE,
                }
            )
        if snapshot.history:
            downgrades.append(
                {
                    "kind": "native_history_enrichment_preserved_in_supplement",
                    "rows": len(snapshot.history),
                    "supplement_file": _SUPPLEMENT_FILE,
                }
            )
        exception_report = {
            "schema": EXCEPTION_REPORT_SCHEMA,
            "cohort_id": cohort_id,
            "blocking_exceptions": [],
            "semantic_downgrades": sorted(
                downgrades,
                key=lambda item: (
                    str(item.get("kind", "")),
                    str(item.get("task_id", "")),
                    str(item.get("link_id", "")),
                ),
            ),
        }
        _write_json(root / _EXCEPTIONS_FILE, exception_report)
        exceptions_size, exceptions_sha = _file_digest(root / _EXCEPTIONS_FILE)

        tree_manifest = self._tree_manifest(tree)
        _write_json(root / _TREE_MANIFEST_FILE, tree_manifest)
        tree_manifest_size, tree_manifest_file_sha = _file_digest(
            root / _TREE_MANIFEST_FILE
        )
        database_size, database_sha = _file_digest(database_path)
        local_file_catalog = self._local_file_catalog(snapshot, verify_assets=True)
        authority_latch = self._native_authority_latch_evidence(snapshot)
        snapshot_digest = _canonical_sha256(
            {
                "database_snapshot_sha256": snapshot.database_snapshot_sha256,
                "documents": list(document_manifest),
                "local_file_catalog": local_file_catalog,
            }
        )
        counts = {
            **database_verification["counts"],
            "master_lines": len(master_lines),
            "archive_lines": len(archive_lines),
            "note_files": len(documents),
            "recovered_note_files": len(snapshot.recovered_documents),
            "tree_files": len(tree_manifest["files"]),
            "local_assets": len(
                {
                    str(row["relative_path"]) for row in snapshot.local_file_links
                }
            ),
        }
        receipt: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "cohort_id": cohort_id,
            "rollback_authority_epoch": rollback_authority_epoch,
            "maintenance_stop_receipt": dict(maintenance_receipt),
            "created_at": self.clock(),
            "source_schema_version": snapshot.schema_version,
            "source_authority_epoch": snapshot.authority_epoch,
            "source_collection_revision": snapshot.collection_revision,
            "source_process_generation": snapshot.process_generation,
            "source_inventory_sha256": snapshot.cohort.get("inventory_sha256"),
            "source_manifest_sha256": snapshot.cohort.get("manifest_sha256"),
            "source_root_fingerprint": snapshot.cohort.get(
                "source_root_fingerprint"
            ),
            "source_cowork_task_store_id": snapshot.cowork_task_store_id,
            "source_database_snapshot_sha256": snapshot.database_snapshot_sha256,
            "source_content_snapshot_sha256": snapshot.content_snapshot_sha256,
            "source_snapshot_sha256": snapshot_digest,
            "legacy_database_schema_version": LEGACY_SCHEMA_VERSION,
            "legacy_database_file": _LEGACY_DB_FILE,
            "legacy_database_byte_length": database_size,
            "legacy_database_sha256": database_sha,
            "legacy_database_semantic_sha256": database_verification[
                "semantic_sha256"
            ],
            "legacy_tree_directory": _LEGACY_TREE_DIR,
            "staged_tree_sha256": tree_manifest["tree_sha256"],
            "tree_manifest_file": _TREE_MANIFEST_FILE,
            "tree_manifest_byte_length": tree_manifest_size,
            "tree_manifest_sha256": tree_manifest_file_sha,
            "exception_report_file": _EXCEPTIONS_FILE,
            "exception_report_byte_length": exceptions_size,
            "exception_report_sha256": exceptions_sha,
            "native_supplement_file": _SUPPLEMENT_FILE,
            "native_supplement_byte_length": supplement_size,
            "native_supplement_sha256": supplement_sha,
            "source_documents": list(document_manifest),
            "document_heads_sha256": _canonical_sha256(document_manifest),
            "source_local_file_catalog": local_file_catalog,
            "source_local_file_catalog_sha256": _canonical_sha256(
                local_file_catalog
            ),
            "source_authority_latch": authority_latch,
            "source_authority_latch_sha256": _canonical_sha256(authority_latch),
            "counts": counts,
            "semantic_downgrade_count": len(exception_report["semantic_downgrades"]),
            "verified": True,
        }
        receipt["receipt_id"] = "trr_" + _canonical_sha256(receipt)[:32]
        _write_json(root / _RECEIPT_FILE, receipt)
        return receipt

    @staticmethod
    def _document_rows(snapshot: _SourceSnapshot) -> tuple[Mapping[str, Any], ...]:
        by_note: dict[str, Mapping[str, Any]] = {}
        for row in (*snapshot.document_links, *snapshot.recovered_documents):
            note_uuid = str(row["note_uuid"]).casefold()
            candidate = {
                "note_uuid": note_uuid,
                "store_id": str(row["store_id"]),
                "document_id": str(row["document_id"]),
                "task_id": row.get("task_id") or row.get("claimed_task_id"),
                "binding_id": row.get("binding_id"),
                "lifecycle": row.get("lifecycle"),
            }
            existing = by_note.get(note_uuid)
            if existing is not None and (
                existing["store_id"], existing["document_id"]
            ) != (candidate["store_id"], candidate["document_id"]):
                raise RollbackExportBlocked(
                    "Two native documents claim the same legacy note UUID.",
                    details={"note_uuid": note_uuid},
                )
            by_note[note_uuid] = candidate
        return tuple(by_note[key] for key in sorted(by_note))

    def _read_documents(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, str], tuple[Mapping[str, Any], ...]]:
        if not rows:
            return {}, ()

        def injected_state(row: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
            if self.document_reader is None or self.document_head_reader is None:
                raise RollbackExportBlocked(
                    "Injected document reads require an immutable structured-head reader."
                )
            content = self.document_reader(row)
            head = str(self.document_head_reader(row))
            return content, self._document_manifest_item(row, content=content, head=head)

        if self.document_reader is not None:
            first_states = [injected_state(row) for row in rows]
            second_states = [injected_state(row) for row in rows]
        else:
            stores = self.document_stores or TaskDocumentStoreManager()
            store = stores.open_existing()
            expected_store_ids = {str(row["store_id"]) for row in rows}
            if expected_store_ids != {str(store.store_id)}:
                raise RollbackExportBlocked(
                    "The opened Task Co-work store does not match the pinned document set.",
                    details={
                        "expected_store_ids": sorted(expected_store_ids),
                        "opened_store_id": str(store.store_id),
                    },
                )
            kernel = DocumentKernelClient()
            try:
                def native_state(
                    row: Mapping[str, Any],
                ) -> tuple[str, Mapping[str, Any]]:
                    document = truth_documents.get_document(
                        store, str(row["document_id"])
                    )
                    if document.ydoc_snapshot_sha256 is None:
                        raise RollbackExportBlocked(
                            "A task document has no immutable snapshot."
                        )
                    snapshot = ydoc_store.read_snapshot(
                        store,
                        snapshot_sha256=document.ydoc_snapshot_sha256,
                    )
                    updates, _cursor = ydoc_store.read_updates(
                        store,
                        document_id=document.id,
                    )
                    head = structured_head_sha256(snapshot, updates)
                    projected = kernel.request(
                        {
                            "kind": "project_markdown",
                            "snapshotBase64": snapshot,
                            "updatesBase64": updates,
                            "expectedBaseStructuredHeadSha256": head,
                        },
                        request_id=(
                            "task_rollback_"
                            + hashlib.sha256(
                                f"{document.id}:{head}".encode("utf-8")
                            ).hexdigest()[:24]
                        ),
                    )
                    if projected.projection is None:
                        raise RollbackExportBlocked(
                            "The document kernel returned no rollback projection."
                        )
                    content = projected.projection.decode("utf-8")
                    return content, self._document_manifest_item(
                        row,
                        content=content,
                        head=head,
                        snapshot_sha256=document.ydoc_snapshot_sha256,
                    )

                first_states = [native_state(row) for row in rows]
                second_states = [native_state(row) for row in rows]
            finally:
                kernel.close()
        first_manifest = tuple(item for _content, item in first_states)
        second_manifest = tuple(item for _content, item in second_states)
        if first_manifest != second_manifest:
            raise RollbackExportBlocked(
                "A Co-work task document changed while it was being exported.",
                details={"first": first_manifest, "second": second_manifest},
            )
        first = {
            str(row["note_uuid"]): state[0]
            for row, state in zip(rows, first_states, strict=True)
        }
        for note_uuid, content in first.items():
            if not isinstance(content, str):
                raise RollbackExportBlocked(
                    "The task document reader returned non-text content.",
                    details={"note_uuid": note_uuid},
                )
        return first, first_manifest

    @staticmethod
    def _document_manifest_item(
        row: Mapping[str, Any],
        *,
        content: str,
        head: str,
        snapshot_sha256: str | None = None,
    ) -> Mapping[str, Any]:
        if not isinstance(content, str):
            raise RollbackExportBlocked("The task document reader returned non-text content.")
        if _SHA256_RE.fullmatch(str(head)) is None:
            raise RollbackExportBlocked(
                "The task document reader returned an invalid structured-head hash."
            )
        return {
            "note_uuid": str(row["note_uuid"]),
            "store_id": str(row["store_id"]),
            "document_id": str(row["document_id"]),
            "task_id": row.get("task_id"),
            "binding_id": row.get("binding_id"),
            "lifecycle": row.get("lifecycle"),
            "ydoc_snapshot_sha256": snapshot_sha256,
            "structured_head_sha256": str(head),
            "projection_sha256": _bytes_sha256(content.encode("utf-8")),
            "projection_byte_length": len(content.encode("utf-8")),
        }

    def _stage_assets_and_rewrite_documents(
        self,
        tree: Path,
        *,
        snapshot: _SourceSnapshot,
        documents: dict[str, str],
    ) -> list[dict[str, Any]]:
        links = list(snapshot.local_file_links)
        all_tokens = {
            token
            for content in documents.values()
            for token in _LOCAL_FILE_TOKEN_RE.findall(content)
        }
        catalog_ids = {str(row["link_id"]) for row in links}
        unknown = sorted(all_tokens - catalog_ids)
        if unknown:
            raise RollbackExportBlocked(
                "A task document contains local-file handles absent from the catalog.",
                details={"link_ids": unknown},
            )
        if not links:
            return []
        by_document: dict[str, list[Mapping[str, Any]]] = {}
        target_hashes: dict[str, str] = {}
        downgrades: list[dict[str, Any]] = []
        reserved = {
            PurePosixPath("master-task-list.md"),
            PurePosixPath("archive.md"),
        } | {
            PurePosixPath("notes") / f"{note_uuid}.md" for note_uuid in documents
        }
        for link in links:
            relative, resolved, size, digest = self._verified_asset(link)
            relative_name = relative.as_posix()
            if any(_paths_collide(relative, generated) for generated in reserved):
                raise RollbackExportBlocked(
                    "A linked file collides with a generated legacy task artifact.",
                    details={"relative_path": relative_name},
                )
            prior_digest = target_hashes.get(relative_name.casefold())
            if prior_digest is not None and prior_digest != digest:
                raise RollbackExportBlocked(
                    "Two local-file links collide at one rollback path.",
                    details={"relative_path": relative_name},
                )
            if prior_digest is None:
                target_hashes[relative_name.casefold()] = digest
                _write_bytes(tree.joinpath(*relative.parts), resolved.read_bytes())
            by_document.setdefault(str(link["document_id"]), []).append(link)

        note_by_document = {
            str(row["document_id"]): str(row["note_uuid"])
            for row in self._document_rows(snapshot)
        }
        for document_id, document_links in by_document.items():
            note_uuid = note_by_document.get(document_id)
            if note_uuid is None or note_uuid not in documents:
                raise RollbackExportBlocked(
                    "A cataloged local file has no exported task document.",
                    details={"document_id": document_id},
                )
            content = documents[note_uuid]
            for link in document_links:
                link_id = str(link["link_id"])
                token = f"wb-local-file:{link_id}"
                relative_path = _safe_relative_path(
                    str(link["relative_path"]), label="A local-file catalog path"
                )
                note_relative = _relative_posix_path(
                    relative_path,
                    from_directory=PurePosixPath("notes"),
                )
                encoded = quote(note_relative, safe="/._-~")
                relative = relative_path.as_posix()
                display = str(link.get("display_name") or relative_path.name)
                rich_pattern = re.compile(
                    rf"Local file \(([^)\r\n]+)\):\s*{re.escape(token)}"
                )
                if rich_pattern.search(content):
                    if str(link["allowed_action"]) == "open":
                        content = rich_pattern.sub(
                            lambda match: f"[{match.group(1)}]({encoded})", content
                        )
                    else:
                        content = rich_pattern.sub(
                            lambda match: f"Local file ({match.group(1)}): {note_relative}",
                            content,
                        )
                elif token in content:
                    content = content.replace(
                        token,
                        encoded
                        if str(link["allowed_action"]) == "open"
                        else note_relative,
                    )
                else:
                    downgrades.append(
                        {
                            "kind": "cataloged_local_file_not_referenced_by_document",
                            "link_id": link_id,
                            "document_id": document_id,
                        }
                    )
                downgrades.append(
                    {
                        "kind": "local_file_rehydrated_from_frozen_root",
                        "link_id": link_id,
                        "task_id": link.get("task_id"),
                        "display_name": display,
                        "allowed_action": link["allowed_action"],
                        "relative_path": relative,
                        "sha256": link["sha256"],
                    }
                )
            documents[note_uuid] = content
        unresolved = {
            token
            for content in documents.values()
            for token in _LOCAL_FILE_TOKEN_RE.findall(content)
        }
        if unresolved:
            raise RollbackExportBlocked(
                "Opaque local-file handles remain after asset rehydration.",
                details={"link_ids": sorted(unresolved)},
            )
        return downgrades

    def _verified_asset(
        self,
        link: Mapping[str, Any],
    ) -> tuple[PurePosixPath, Path, int, str]:
        if (
            self.local_asset_root is None
            or not self.local_asset_root.is_dir()
            or _is_link_like(self.local_asset_root)
        ):
            raise RollbackExportBlocked(
                "A verified frozen asset root is required to stage linked files."
            )
        frozen_root = self.local_asset_root
        relative = _safe_relative_path(
            str(link["relative_path"]), label="A local-file catalog path"
        )
        source = frozen_root.joinpath(*relative.parts)
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(frozen_root)
        except (OSError, ValueError) as exc:
            raise RollbackExportBlocked(
                "A linked file escapes or is missing from the frozen root.",
                details={"link_id": link["link_id"], "relative_path": relative.as_posix()},
            ) from exc
        cursor = source
        while True:
            if _is_link_like(cursor):
                raise RollbackExportBlocked(
                    "Linked or reparse-point frozen assets cannot enter a rollback tree.",
                    details={"relative_path": relative.as_posix()},
                )
            if cursor == frozen_root:
                break
            cursor = cursor.parent
        if not resolved.is_file():
            raise RollbackExportBlocked(
                "A cataloged rollback asset is not a regular file.",
                details={"relative_path": relative.as_posix()},
            )
        size, digest = _file_digest(resolved)
        if size != int(link["byte_length"]) or digest != str(link["sha256"]):
            raise RollbackExportBlocked(
                "A frozen asset no longer matches its native catalog.",
                details={
                    "link_id": link["link_id"],
                    "relative_path": relative.as_posix(),
                    "expected_byte_length": int(link["byte_length"]),
                    "actual_byte_length": size,
                    "expected_sha256": link["sha256"],
                    "actual_sha256": digest,
                },
            )
        return relative, resolved, size, digest

    def _local_file_catalog(
        self,
        snapshot: _SourceSnapshot,
        *,
        verify_assets: bool,
    ) -> Mapping[str, Any]:
        assets: list[Mapping[str, Any]] = []
        if verify_assets:
            for link in snapshot.local_file_links:
                relative, _resolved, size, digest = self._verified_asset(link)
                assets.append(
                    {
                        "link_id": str(link["link_id"]),
                        "relative_path": relative.as_posix(),
                        "byte_length": size,
                        "sha256": digest,
                    }
                )
        return {
            "roots": [dict(row) for row in snapshot.local_file_roots],
            "links": [dict(row) for row in snapshot.local_file_links],
            "verified_assets": assets,
        }

    @staticmethod
    def _render_task_line(
        task: Mapping[str, Any],
        *,
        tags: Sequence[Mapping[str, Any]],
        legacy_date: str | None,
    ) -> str:
        task_id = str(task["task_id"])
        checked = str(task.get("state")) == "done"
        parts = [f"- [{'x' if checked else ' '}] #todo {str(task['description']).strip()}"]
        if task.get("note_uuid"):
            parts.append(f"[[{str(task['note_uuid']).casefold()}|📓]]")
        for tag in sorted({str(row["tag"]).strip().lstrip("#") for row in tags}):
            if tag and tag.casefold() != "todo":
                parts.append(f"#{tag}")
        parts.append(f"🆔 {task_id}")
        if legacy_date:
            parts.append(f"📅 {legacy_date}")
        completed = _legacy_date(
            task.get("completed_at"), task_id=task_id, field="completed_at"
        )
        if completed:
            parts.append(f"✅ {completed}")
        priority = {"high": "⏫", "medium": "🔼", "low": "🔽"}.get(
            str(task.get("urgency") or "medium")
        )
        if priority:
            parts.append(priority)
        return " ".join(parts)

    @staticmethod
    def _insert_rows(
        connection: sqlite3.Connection,
        table: str,
        columns: Sequence[str],
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        if not rows:
            return
        placeholders = ",".join("?" for _ in columns)
        connection.executemany(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            [tuple(row.get(column) for column in columns) for row in rows],
        )

    def _write_legacy_database(
        self,
        path: Path,
        *,
        snapshot: _SourceSnapshot,
        date_mappings: Mapping[str, str | None],
    ) -> None:
        if path.exists():
            raise RollbackExportBlocked("The staged legacy database already exists.")
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            _m001_bootstrap_v11(connection)
            connection.execute(f"PRAGMA user_version = {LEGACY_SCHEMA_VERSION}")
            legacy_tasks: list[dict[str, Any]] = []
            for native in snapshot.tasks:
                task_id = str(native["task_id"])
                row = {column: native.get(column) for column in _LEGACY_TASK_COLUMNS}
                row["deadline_date"] = date_mappings[task_id]
                row["has_deadline"] = 1 if date_mappings[task_id] else 0
                legacy_tasks.append(row)
            self._insert_rows(
                connection, "task_metadata", _LEGACY_TASK_COLUMNS, legacy_tasks
            )
            self._insert_rows(
                connection,
                "task_state_history",
                _LEGACY_HISTORY_COLUMNS,
                snapshot.history,
            )
            self._insert_rows(
                connection, "task_sessions", _LEGACY_SESSION_COLUMNS, snapshot.sessions
            )
            self._insert_rows(connection, "task_tags", _LEGACY_TAG_COLUMNS, snapshot.tags)
            self._insert_rows(
                connection,
                "task_action_items",
                _LEGACY_ACTION_COLUMNS,
                snapshot.actions,
            )
            self._insert_rows(
                connection, "lww_meta", _LEGACY_LWW_COLUMNS, snapshot.lww_meta
            )
            if snapshot.sync_status is not None:
                self._insert_rows(
                    connection,
                    "task_sync_status",
                    (
                        "id",
                        "last_full_sync_at",
                        "last_sync_created",
                        "last_sync_updated",
                        "last_sync_deleted",
                        "updated_at",
                    ),
                    (snapshot.sync_status,),
                )
            else:
                connection.execute(
                    "INSERT INTO task_sync_status "
                    "(id,last_full_sync_at,last_sync_created,last_sync_updated,"
                    "last_sync_deleted,updated_at) VALUES (1,NULL,0,0,0,?)",
                    (self.clock(),),
                )
            violations = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
            if violations:
                raise RollbackExportBlocked(
                    "The staged v11 database has foreign-key violations.",
                    details={"violations": violations},
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
        return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))

    @classmethod
    def _verify_v11_database(cls, path: Path) -> dict[str, Any]:
        if not path.is_file() or path.is_symlink():
            raise RollbackExportVerificationError(
                "The staged legacy database is missing or linked."
            )
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != LEGACY_SCHEMA_VERSION:
                raise RollbackExportVerificationError(
                    "The staged database is not schema v11.",
                    details={"actual_schema_version": version},
                )
            expected_columns = {
                "task_metadata": _LEGACY_TASK_COLUMNS,
                "task_state_history": _LEGACY_HISTORY_COLUMNS,
                "task_sessions": _LEGACY_SESSION_COLUMNS,
                "task_tags": _LEGACY_TAG_COLUMNS,
                "task_action_items": _LEGACY_ACTION_COLUMNS,
                "lww_meta": _LEGACY_LWW_COLUMNS,
                "task_sync_status": (
                    "id",
                    "last_full_sync_at",
                    "last_sync_created",
                    "last_sync_updated",
                    "last_sync_deleted",
                    "updated_at",
                ),
            }
            actual_columns = {
                table: cls._table_columns(connection, table)
                for table in expected_columns
            }
            if actual_columns != expected_columns:
                raise RollbackExportVerificationError(
                    "The staged database schema differs from the v11 contract.",
                    details={"expected": expected_columns, "actual": actual_columns},
                )
            forbidden_tables = {
                "task_system_state",
                "task_collection_state",
                "task_mutation_receipts",
                "task_event_outbox",
                "task_document_links",
            }
            present_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if present_tables & forbidden_tables:
                raise RollbackExportVerificationError(
                    "Native-only tables leaked into the v11 database.",
                    details={"tables": sorted(present_tables & forbidden_tables)},
                )
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise RollbackExportVerificationError(
                    "The staged v11 database failed integrity_check.",
                    details={"integrity": integrity},
                )
            foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
            if foreign_keys:
                raise RollbackExportVerificationError(
                    "The staged v11 database failed foreign_key_check.",
                    details={"violations": foreign_keys},
                )
            counts = {
                "task_rows": int(
                    connection.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0]
                ),
                "live_task_rows": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_metadata WHERE deleted_at IS NULL"
                    ).fetchone()[0]
                ),
                "tombstone_rows": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_metadata WHERE deleted_at IS NOT NULL"
                    ).fetchone()[0]
                ),
                "archived_rows": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_metadata WHERE archived_at IS NOT NULL"
                    ).fetchone()[0]
                ),
                "tag_rows": int(
                    connection.execute("SELECT COUNT(*) FROM task_tags").fetchone()[0]
                ),
                "history_rows": int(
                    connection.execute("SELECT COUNT(*) FROM task_state_history").fetchone()[0]
                ),
                "session_rows": int(
                    connection.execute("SELECT COUNT(*) FROM task_sessions").fetchone()[0]
                ),
                "action_item_rows": int(
                    connection.execute("SELECT COUNT(*) FROM task_action_items").fetchone()[0]
                ),
                "lww_rows": int(
                    connection.execute("SELECT COUNT(*) FROM lww_meta").fetchone()[0]
                ),
            }
            semantic = {
                "tasks": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM task_metadata ORDER BY task_id"
                    )
                ],
                "tags": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM task_tags ORDER BY task_id, tag"
                    )
                ],
                "history": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM task_state_history ORDER BY id"
                    )
                ],
                "sessions": [
                    dict(row)
                    for row in connection.execute("SELECT * FROM task_sessions ORDER BY id")
                ],
                "actions": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM task_action_items ORDER BY id"
                    )
                ],
            }
            return {
                "schema_version": version,
                "counts": counts,
                "semantic_sha256": _canonical_sha256(semantic),
            }
        finally:
            connection.close()

    @staticmethod
    def _tree_manifest(tree: Path) -> dict[str, Any]:
        if not tree.is_dir() or _is_link_like(tree):
            raise RollbackExportVerificationError(
                "The staged legacy tree is missing or linked."
            )
        files: list[dict[str, Any]] = []
        for path in sorted(tree.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if _is_link_like(path):
                raise RollbackExportVerificationError(
                    "The staged legacy tree contains a symlink.",
                    details={"path": path.relative_to(tree).as_posix()},
                )
            if not path.is_file():
                continue
            relative = path.relative_to(tree).as_posix()
            length, digest = _file_digest(path)
            files.append(
                {"relative_path": relative, "byte_length": length, "sha256": digest}
            )
        return {
            "schema": TREE_MANIFEST_SCHEMA,
            "files": files,
            "tree_sha256": _canonical_sha256(files),
        }

    def _verify_staging_at(
        self,
        root: Path,
        *,
        expected_receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not root.is_dir() or _is_link_like(root):
            raise RollbackExportVerificationError(
                "The rollback staging directory is missing or linked."
            )
        receipt_path = root / _RECEIPT_FILE
        if expected_receipt is None:
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RollbackExportVerificationError(
                    "The rollback export receipt is missing or malformed."
                ) from exc
        else:
            receipt = dict(expected_receipt)
        if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
            raise RollbackExportVerificationError("The rollback receipt schema is unsupported.")
        claimed_id = receipt.get("receipt_id")
        unsigned = dict(receipt)
        unsigned.pop("receipt_id", None)
        actual_id = "trr_" + _canonical_sha256(unsigned)[:32]
        if claimed_id != actual_id:
            raise RollbackExportVerificationError(
                "The rollback receipt ID does not match its payload."
            )
        source_documents = receipt.get("source_documents")
        local_catalog = receipt.get("source_local_file_catalog")
        authority_latch = receipt.get("source_authority_latch")
        if (
            not isinstance(source_documents, list)
            or not isinstance(local_catalog, dict)
            or not isinstance(authority_latch, dict)
        ):
            raise RollbackExportVerificationError(
                "The rollback receipt lacks immutable native source manifests."
            )
        document_ids = [str(item.get("document_id") or "") for item in source_documents]
        if (
            any(not identifier for identifier in document_ids)
            or len(document_ids) != len(set(document_ids))
            or receipt.get("document_heads_sha256")
            != _canonical_sha256(source_documents)
            or receipt.get("source_local_file_catalog_sha256")
            != _canonical_sha256(local_catalog)
            or authority_latch.get("schema") != AUTHORITY_LATCH_SCHEMA
            or receipt.get("source_authority_latch_sha256")
            != _canonical_sha256(authority_latch)
        ):
            raise RollbackExportVerificationError(
                "The rollback receipt source manifest hashes are invalid."
            )
        for item in source_documents:
            if (
                _SHA256_RE.fullmatch(str(item.get("structured_head_sha256") or ""))
                is None
                or _SHA256_RE.fullmatch(str(item.get("projection_sha256") or ""))
                is None
            ):
                raise RollbackExportVerificationError(
                    "A rollback receipt document head is invalid."
                )
        composite = _canonical_sha256(
            {
                "database_snapshot_sha256": receipt.get(
                    "source_database_snapshot_sha256"
                ),
                "documents": source_documents,
                "local_file_catalog": local_catalog,
            }
        )
        if receipt.get("source_snapshot_sha256") != composite:
            raise RollbackExportVerificationError(
                "The rollback receipt native snapshot digest is invalid."
            )

        artifacts = (
            ("tree_manifest", _TREE_MANIFEST_FILE),
            ("exception_report", _EXCEPTIONS_FILE),
            ("native_supplement", _SUPPLEMENT_FILE),
        )
        for prefix, file_name in artifacts:
            path = root / file_name
            length, digest = _file_digest(path)
            if (
                receipt.get(f"{prefix}_byte_length") != length
                or receipt.get(f"{prefix}_sha256") != digest
            ):
                raise RollbackExportVerificationError(
                    f"The staged {file_name} no longer matches its receipt."
                )
        try:
            stored_manifest = json.loads(
                (root / _TREE_MANIFEST_FILE).read_text(encoding="utf-8")
            )
        except json.JSONDecodeError as exc:
            raise RollbackExportVerificationError(
                "The legacy tree manifest is malformed."
            ) from exc
        actual_manifest = self._tree_manifest(root / _LEGACY_TREE_DIR)
        if stored_manifest != actual_manifest:
            raise RollbackExportVerificationError(
                "The staged legacy tree changed after export."
            )
        if receipt.get("staged_tree_sha256") != actual_manifest["tree_sha256"]:
            raise RollbackExportVerificationError(
                "The staged legacy tree hash differs from the receipt."
            )
        database_path = root / _LEGACY_DB_FILE
        database_length, database_sha = _file_digest(database_path)
        if (
            receipt.get("legacy_database_byte_length") != database_length
            or receipt.get("legacy_database_sha256") != database_sha
            or receipt.get("legacy_database_schema_version") != LEGACY_SCHEMA_VERSION
        ):
            raise RollbackExportVerificationError(
                "The staged legacy database no longer matches its receipt."
            )
        database = self._verify_v11_database(database_path)
        if receipt.get("legacy_database_semantic_sha256") != database["semantic_sha256"]:
            raise RollbackExportVerificationError(
                "The staged v11 task data changed after export."
            )
        expected_counts = dict(receipt.get("counts") or {})
        for key, value in database["counts"].items():
            if expected_counts.get(key) != value:
                raise RollbackExportVerificationError(
                    "The staged v11 semantic counts differ from the receipt.",
                    details={"count": key, "expected": expected_counts.get(key), "actual": value},
                )
        task_files = {item["relative_path"] for item in actual_manifest["files"]}
        master_lines = sum(
            1
            for line in (root / _LEGACY_TREE_DIR / "master-task-list.md")
            .read_text(encoding="utf-8")
            .splitlines()
            if re.match(r"^-\s*\[[ xX]\]", line.strip())
        )
        archive_lines = sum(
            1
            for line in (root / _LEGACY_TREE_DIR / "archive.md")
            .read_text(encoding="utf-8")
            .splitlines()
            if re.match(r"^-\s*\[[ xX]\]", line.strip())
        )
        note_files = sum(
            1
            for item in task_files
            if PurePosixPath(item).parent == PurePosixPath("notes")
            and _NOTE_UUID_RE.fullmatch(PurePosixPath(item).stem)
            and PurePosixPath(item).suffix.casefold() == ".md"
        )
        observed_tree_counts = {
            "master_lines": master_lines,
            "archive_lines": archive_lines,
            "note_files": note_files,
            "tree_files": len(actual_manifest["files"]),
        }
        for key, value in observed_tree_counts.items():
            if expected_counts.get(key) != value:
                raise RollbackExportVerificationError(
                    "The staged legacy tree counts differ from the receipt.",
                    details={"count": key, "expected": expected_counts.get(key), "actual": value},
                )
        if expected_counts.get("live_task_rows") != master_lines + archive_lines:
            raise RollbackExportVerificationError(
                "The staged Markdown task lines do not cover every live v11 task row."
            )
        return receipt


__all__ = [
    "DateConflictResolution",
    "RECEIPT_SCHEMA",
    "ROLLBACK_EXPORT_CONFIRMATION",
    "ReverseLegacyTaskExportOperator",
    "RollbackExportBlocked",
    "RollbackExportError",
    "RollbackExportVerificationError",
]
