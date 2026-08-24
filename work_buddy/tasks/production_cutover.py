"""Production-only, fail-closed native Task cutover surface.

The lower-level migration ledger deliberately exposes small, composable
primitives.  That is useful in tests, but it is not a safe production
operator by itself: :meth:`TaskMigrationLedger.record_gate` accepts evidence
that a caller has already verified.  This module is the production wrapper.
It derives every mandatory gate from the frozen tree, the migration staging
tables, live process state, the retry queue, backup artifacts, a restored
fixture, and (on Windows) the effective NTFS ACL before it calls the existing
prepare/apply/activate protocol.

Nothing here runs during application startup.  ``status`` is read-only and is
the default CLI action.  Mutating actions are explicit, resumable, and retain
append-only external receipts in addition to the cohort ledger.
"""

from __future__ import annotations

import argparse
import base64
import csv
from contextlib import ExitStack
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from work_buddy import paths
from work_buddy.cowork.local_files import LocalFileLinkRegistry
from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.markdown_db.storage_helpers import atomic_write_text, file_lock
from work_buddy.sources import ActorRef, SourceStore
from work_buddy.tasks.documents import TaskDocumentStoreManager
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.export import import_store
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.utils.process import is_process_alive

from .import_legacy import (
    ACTIVATION_CONFIRMATION,
    LegacyTaskCutoverOperator,
    LegacyTaskDocumentImporter,
)
from .migration import (
    REQUIRED_ACTIVATION_GATES,
    CutoverPreconditionError,
    InventoryItem,
    LegacyInventory,
    LegacyManifestEntry,
    ParsedLegacyTaskLine,
    canonical_sha256,
)
from .runtime import TASK_MUTATION_CAPABILITIES, is_native_authority_epoch
from .store import TaskStore


STOP_RECEIPT_SCHEMA = "wb.native-task-process-stop/v1"
RESTORE_RECEIPT_SCHEMA = "wb.native-task-restore-rehearsal/v1"
ROLLBACK_REHEARSAL_SCHEMA = "wb.native-task-rollback-rehearsal/v1"
OPERATOR_RECEIPT_SCHEMA = "wb.native-task-cutover-operator/v1"
RETRY_CANCELLATION_RECEIPT_SCHEMA = "wb.native-task-retry-cancellation/v1"
CANCEL_RETRIES_CONFIRMATION = "CANCEL QUEUED LEGACY TASK MUTATIONS"

_IMPORTABLE_DOCUMENT_CLASSES = frozenset(
    {
        "task_note_live",
        "task_note_live_db_only",
        "task_note_idless",
        "task_note_deleted",
        "recovered_task_document",
    }
)
_LOCAL_FILE_CLASSES = frozenset({"local_file_pdf", "local_file_sensitive"})
_RETRY_WRAPPERS = frozenset({"retry", "obsidian_retry"})
_PORTABLE_REHEARSAL_KINDS = frozenset(
    {"cowork_task_store", "task_causality"}
)
_PREPARE_GATES = REQUIRED_ACTIVATION_GATES - frozenset(
    {"legacy_mutation_fenced", "binding_cohort_verified"}
)
PRODUCTION_ACTIVATION_GATES = frozenset(
    {*REQUIRED_ACTIVATION_GATES, "rollback_rehearsal_verified"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NOTE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_LEGACY_V11_COLUMNS = {
    "task_metadata": (
        "task_id", "state", "urgency", "complexity", "contract", "note_uuid",
        "snooze_until", "created_at", "updated_at", "completed_at", "archived_at",
        "task_kind", "density", "outcome_text", "next_action_text",
        "definition_of_done", "creation_effort", "user_involvement",
        "creation_provenance", "has_deadline", "deadline_date", "has_dependency",
        "dependency_hint", "description", "risk_profile_json",
        "automation_tier_achievable", "last_actor", "agent_required_contexts",
        "user_required_contexts", "required_contexts_source", "current_action_item_id",
        "deleted_at", "created_by_session",
    ),
    "task_state_history": ("id", "task_id", "old_state", "new_state", "changed_at", "reason"),
    "task_sessions": ("id", "task_id", "session_id", "assigned_at"),
    "task_tags": ("task_id", "tag", "is_namespace"),
    "task_action_items": (
        "id", "task_id", "sequence", "description", "state", "risk_profile_json",
        "agent_required_contexts", "user_required_contexts", "definition_of_done",
        "authorship", "completed_at", "handoff_package_path", "created_at",
        "updated_at", "deleted_at",
    ),
    "lww_meta": (
        "id", "table_name", "row_pk", "field", "ts", "actor", "process",
        "from_surface", "to_surface",
    ),
    "task_sync_status": (
        "id", "last_full_sync_at", "last_sync_created", "last_sync_updated",
        "last_sync_deleted", "updated_at",
    ),
}
_SNAPSHOT_ID = re.compile(
    r"^snap-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)(?:-|$)"
)


def load_accepted_inventory(
    task_db_path: str | Path,
    *,
    cohort_id: str,
) -> LegacyInventory:
    """Rehydrate the immutable inventory accepted by shadow import.

    Rebuilding against the live SQLite file after shadow import is incorrect:
    the migration ledger is stored in that same file, so its file hash has
    necessarily changed.  The cohort stores the accepted source-DB digest and
    every inventory item (including exact task-line bytes).  Rehydrate those
    immutable inputs and recompute their original inventory digest instead.
    """

    target = Path(task_db_path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        cohort = conn.execute(
            "SELECT * FROM task_migration_cohorts WHERE cohort_id=?", (cohort_id,)
        ).fetchone()
        if cohort is None:
            raise CutoverPreconditionError("The accepted shadow cohort does not exist.")
        rows = conn.execute(
            "SELECT * FROM task_migration_inventory WHERE cohort_id=? ORDER BY item_key",
            (cohort_id,),
        ).fetchall()
        if not rows:
            raise CutoverPreconditionError("The accepted cohort inventory is empty.")
        stage_fields: dict[str, Mapping[str, Any]] = {}
        for table in ("task_migration_idless_stage", "task_migration_existing_task_stage"):
            for row in conn.execute(
                f"SELECT source_key, fields_json FROM {table} WHERE cohort_id=?",
                (cohort_id,),
            ):
                value = json.loads(str(row["fields_json"]))
                if not isinstance(value, Mapping):
                    raise CutoverPreconditionError("A staged task field receipt is invalid.")
                stage_fields[str(row["source_key"])] = value
    finally:
        conn.close()

    items = tuple(
        InventoryItem(
            item_key=str(row["item_key"]),
            item_kind=str(row["item_kind"]),
            classification=str(row["classification"]),
            reason=str(row["reason"]),
            relative_path=row["relative_path"],
            line_number=row["line_number"],
            task_id=row["task_id"],
            note_uuid=row["note_uuid"],
            content_sha256=row["content_sha256"],
            byte_length=row["byte_length"],
            source_bytes=(None if row["source_bytes"] is None else bytes(row["source_bytes"])),
            metadata=json.loads(str(row["metadata_json"])),
        )
        for row in rows
    )
    task_lines: list[ParsedLegacyTaskLine] = []
    for item in items:
        if item.item_kind != "task_line":
            continue
        if item.source_bytes is None or item.relative_path is None or item.line_number is None:
            raise CutoverPreconditionError("An accepted task-line receipt is incomplete.")
        fields = stage_fields.get(item.item_key)
        if fields is None:
            raise CutoverPreconditionError("An accepted task line has no staging receipt.")
        metadata = dict(item.metadata)
        is_idless = item.classification == "idless_task_stage"
        task_lines.append(
            ParsedLegacyTaskLine(
                source_key=item.item_key,
                relative_path=item.relative_path,
                line_number=int(item.line_number),
                exact_bytes=item.source_bytes,
                line_sha256=str(item.content_sha256),
                task_id=None if is_idless else str(item.task_id),
                imported_task_id=str(item.task_id),
                description=str(fields.get("description") or ""),
                state=str(metadata.get("state") or "inbox"),
                urgency=str(metadata.get("urgency") or "medium"),
                due_date=metadata.get("due_date"),
                completed_at=metadata.get("completed_at"),
                archived=bool(metadata.get("archived")),
                note_uuid=item.note_uuid,
                tags=tuple(str(tag) for tag in metadata.get("tags") or ()),
                checked=str(metadata.get("state") or "") == "done",
                date_ambiguity=bool(metadata.get("date_ambiguity")),
            )
        )
    counts = json.loads(str(cohort["counts_json"]))
    inventory = LegacyInventory(
        cohort_id=str(cohort["cohort_id"]),
        manifest_sha256=str(cohort["manifest_sha256"]),
        inventory_sha256=str(cohort["inventory_sha256"]),
        source_root_fingerprint=str(cohort["source_root_fingerprint"]),
        source_db_sha256=str(cohort["source_db_sha256"]),
        source_db_integrity=str(cohort["source_db_integrity"]),
        source_db_schema_version=int(cohort["source_db_schema_version"]),
        source_file_count=int(cohort["source_file_count"]),
        source_tree_bytes=int(cohort["source_tree_bytes"]),
        items=items,
        task_lines=tuple(sorted(task_lines, key=lambda line: line.source_key)),
        errors=(),
        counts=counts,
    )
    recomputed = canonical_sha256(
        {
            "schema": "wb.legacy-task-inventory/v1",
            "cohort_id": inventory.cohort_id,
            "manifest_sha256": inventory.manifest_sha256,
            "source_db_sha256": inventory.source_db_sha256,
            "source_db_integrity": inventory.source_db_integrity,
            "source_db_schema_version": inventory.source_db_schema_version,
            "items": [item.digest_dict() for item in inventory.items],
        }
    )
    if recomputed != inventory.inventory_sha256:
        raise CutoverPreconditionError("The accepted inventory ledger digest does not verify.")
    return inventory.require_valid()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, *, label: str) -> Path:
    supplied = path.expanduser()
    try:
        supplied_info = os.lstat(supplied)
    except OSError as exc:
        raise CutoverPreconditionError(f"{label} is unavailable: {supplied}") from exc
    supplied_reparse = bool(
        getattr(supplied_info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )
    if supplied.is_symlink() or supplied_reparse:
        raise CutoverPreconditionError(f"{label} must not be a filesystem link.")
    candidate = supplied.resolve(strict=True)
    info = os.lstat(candidate)
    reparse = bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )
    if candidate.is_symlink() or reparse or not candidate.is_file():
        raise CutoverPreconditionError(f"{label} must be a regular, non-linked file.")
    return candidate


def _safe_relative(value: str) -> str:
    normalized = str(value).replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or normalized != pure.as_posix()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise CutoverPreconditionError(f"Unsafe artifact path: {value!r}")
    return normalized


def _json_value(path: Path, *, expected: type) -> Any:
    source = _regular_file(path, label="Receipt")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverPreconditionError(f"Receipt is not valid UTF-8 JSON: {source}") from exc
    if not isinstance(value, expected):
        raise CutoverPreconditionError(
            f"Receipt {source} must contain a JSON {expected.__name__}."
        )
    return value


def _row_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    return value


def _rows_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    normalized = [
        {key: _row_value(row[key]) for key in sorted(row)}
        for row in rows
    ]
    return canonical_sha256(normalized)


def _atomic_receipt(directory: Path, *, cohort_id: str, action: str, payload: Mapping[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stable_payload = dict(payload)
    payload_sha256 = canonical_sha256(stable_payload)
    receipt = {
        "schema": OPERATOR_RECEIPT_SCHEMA,
        "cohort_id": cohort_id,
        "action": action,
        "payload_sha256": payload_sha256,
        "payload": stable_payload,
    }
    target = directory / f"{cohort_id}.{action}.{payload_sha256[:16]}.json"
    encoded = (json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if target.exists():
        if target.read_bytes() != encoded:
            raise CutoverPreconditionError("An operator receipt identity was reused with other bytes.")
        return target
    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
    return target


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    passed: bool
    evidence: Mapping[str, Any]
    problems: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "evidence": dict(self.evidence),
            "problems": list(self.problems),
        }


@dataclass(frozen=True, slots=True)
class CutoverPaths:
    manifest: Path
    frozen_tree: Path
    legacy_tree: Path
    backup_receipts: Path
    restore_rehearsal: Path
    rollback_rehearsal: Path
    process_stop_receipt: Path
    receipts: Path
    root_bindings: Path
    operations: Path
    sidecar_state: Path
    sidecar_pid: Path
    tray_pid: Path
    job_roots: tuple[Path, ...]


class ProductionTaskCutover:
    """Validate and drive one production cohort without trusting caller gates."""

    def __init__(
        self,
        *,
        inventory: LegacyInventory,
        task_db_path: str | Path,
        cutover_paths: CutoverPaths,
        operator: LegacyTaskCutoverOperator | None = None,
        clock: Callable[[], datetime] = _utc_now,
        process_alive: Callable[[int], bool] = is_process_alive,
        process_lister: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        acl_probe: Callable[[Path], Mapping[str, Any]] | None = None,
        backup_freshness: timedelta = timedelta(hours=24),
        stop_receipt_freshness: timedelta = timedelta(minutes=15),
    ) -> None:
        self.inventory = inventory
        self.task_db_path = Path(task_db_path).expanduser().resolve()
        self.paths = cutover_paths
        self.operator = operator
        self.clock = clock
        self.process_alive = process_alive
        self.process_lister = process_lister or self._list_processes
        self.acl_probe = acl_probe or self._probe_windows_acl
        self.backup_freshness = backup_freshness
        self.stop_receipt_freshness = stop_receipt_freshness
        self.manifest = LegacyManifestEntry.from_csv(self.paths.manifest)
        self._portable_restore_cache: dict[tuple[str, str, str], Mapping[str, Any]] = {}

    def _connect_ro(self, path: Path | None = None) -> sqlite3.Connection:
        target = (path or self.task_db_path).expanduser().resolve(strict=True)
        uri = f"file:{target.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _cohort(self, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        owned = conn is None
        connection = conn or self._connect_ro()
        try:
            row = connection.execute(
                "SELECT * FROM task_migration_cohorts WHERE cohort_id=?",
                (self.inventory.cohort_id,),
            ).fetchone()
            if row is None:
                raise CutoverPreconditionError("The shadow migration cohort does not exist.")
            return dict(row)
        finally:
            if owned:
                connection.close()

    @staticmethod
    def _guard(name: str, callback: Callable[[], Mapping[str, Any]]) -> GateCheck:
        try:
            return GateCheck(name, True, callback())
        except Exception as exc:
            details = getattr(exc, "details", None)
            evidence = {"error_type": type(exc).__name__}
            if isinstance(details, Mapping) and details:
                evidence["details"] = dict(details)
            return GateCheck(name, False, evidence, (str(exc),))

    def _manifest_digest(self) -> str:
        by_path = {entry.relative_path: entry for entry in self.manifest}
        if len(by_path) != len(self.manifest):
            raise CutoverPreconditionError("The legacy manifest contains duplicate paths.")
        return canonical_sha256(
            [asdict(by_path[path]) for path in sorted(by_path)]
        )

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        info = os.lstat(path)
        return bool(
            path.is_symlink()
            or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
        )

    def _tree_evidence(self) -> Mapping[str, Any]:
        supplied_root = self.paths.frozen_tree.expanduser()
        if self._is_reparse(supplied_root):
            raise CutoverPreconditionError("The frozen task root must not be a filesystem link.")
        root = supplied_root.resolve(strict=True)
        legacy = self.paths.legacy_tree.expanduser().resolve(strict=False)
        legacy_fingerprint = hashlib.sha256(
            os.path.normcase(str(legacy)).encode("utf-8")
        ).hexdigest()
        if legacy_fingerprint != self.inventory.source_root_fingerprint:
            raise CutoverPreconditionError(
                "The supplied legacy tree path is not the accepted inventory source path."
            )
        if not root.is_dir() or self._is_reparse(root):
            raise CutoverPreconditionError("The frozen task tree must be a real directory.")
        wrapper = root.parent
        if (
            root.name.casefold() != "tasks"
            or wrapper.parent.name.casefold() != "_frozen"
            or not wrapper.name.casefold().startswith("work-buddy-task-tree-")
            or self._is_reparse(wrapper)
        ):
            raise CutoverPreconditionError(
                "The sealed target must use _frozen/work-buddy-task-tree-<id>/tasks/."
            )
        wrapper_entries = list(wrapper.iterdir())
        if len(wrapper_entries) != 1 or wrapper_entries[0].resolve() != root:
            raise CutoverPreconditionError(
                "The dedicated frozen wrapper must contain only the exact tasks tree; "
                "warning markers belong beside the sealed wrapper."
            )
        if legacy.exists():
            raise CutoverPreconditionError(
                "The original legacy task path still exists; the same-volume freeze is incomplete."
            )
        if root == legacy:
            raise CutoverPreconditionError("Frozen and live legacy roots cannot be the same path.")
        if root.anchor.casefold() != legacy.anchor.casefold():
            raise CutoverPreconditionError("The frozen tree is not on the legacy tree's volume.")

        expected = {entry.relative_path: entry for entry in self.manifest}
        discovered: dict[str, Path] = {}
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if self._is_reparse(path):
                raise CutoverPreconditionError(f"Frozen tree contains a linked entry: {relative}")
            if path.is_file():
                discovered[relative] = path
        missing = sorted(set(expected) - set(discovered))
        extra = sorted(set(discovered) - set(expected))
        if missing or extra:
            raise CutoverPreconditionError(
                "The frozen tree membership differs from the exact manifest.",
                details={"missing": missing, "extra": extra},
            )
        rows: list[dict[str, Any]] = []
        for relative in sorted(expected):
            entry = expected[relative]
            path = discovered[relative]
            size = path.stat().st_size
            digest = _sha256_file(path)
            if size != entry.byte_length or digest != entry.sha256:
                raise CutoverPreconditionError(f"Frozen tree bytes changed: {relative}")
            rows.append({"relative_path": relative, "bytes": size, "sha256": digest})
        manifest_sha256 = self._manifest_digest()
        if manifest_sha256 != self.inventory.manifest_sha256:
            raise CutoverPreconditionError("The supplied manifest is not the accepted cohort manifest.")
        return {
            "schema": "wb.native-task-frozen-tree-verification/v1",
            "frozen_tree": str(root),
            "acl_wrapper": str(wrapper),
            "legacy_tree_absent": str(legacy),
            "legacy_tree_path_fingerprint": legacy_fingerprint,
            "manifest_sha256": manifest_sha256,
            "tree_sha256": canonical_sha256(rows),
            "file_count": len(rows),
            "tree_bytes": sum(row["bytes"] for row in rows),
        }

    def _inventory_evidence(self) -> Mapping[str, Any]:
        self.inventory.require_valid()
        tree = self._tree_evidence()
        cohort = self._cohort()
        expected = {
            "inventory_sha256": self.inventory.inventory_sha256,
            "manifest_sha256": self.inventory.manifest_sha256,
            "source_file_count": self.inventory.source_file_count,
            "source_tree_bytes": self.inventory.source_tree_bytes,
        }
        for key, value in expected.items():
            observed = int(cohort[key]) if isinstance(value, int) else str(cohort[key])
            if observed != value:
                raise CutoverPreconditionError(f"Cohort {key} no longer matches the rebuilt inventory.")
        return {
            "schema": "wb.native-task-inventory-parity/v1",
            **expected,
            "inventory_counts_sha256": canonical_sha256(dict(self.inventory.counts)),
            "tree_sha256": tree["tree_sha256"],
        }

    def _shadow_task_evidence(self) -> Mapping[str, Any]:
        expected_existing = {
            (line.source_key, line.imported_task_id)
            for line in self.inventory.task_lines
            if not line.is_idless
        }
        expected_idless = {
            (line.source_key, line.imported_task_id, line.line_sha256)
            for line in self.inventory.task_lines
            if line.is_idless
        }
        with self._connect_ro() as conn:
            cohort = self._cohort(conn)
            active = str(cohort["state"]) == "active"
            existing_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM task_migration_existing_task_stage WHERE cohort_id=? "
                    "ORDER BY source_key",
                    (self.inventory.cohort_id,),
                )
            ]
            idless_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM task_migration_idless_stage WHERE cohort_id=? ORDER BY source_key",
                    (self.inventory.cohort_id,),
                )
            ]
            for staged in existing_rows:
                current = conn.execute(
                    "SELECT * FROM task_metadata WHERE task_id=?", (staged["task_id"],)
                ).fetchone()
                if current is None:
                    raise CutoverPreconditionError(
                        f"An identified task disappeared: {staged['task_id']}"
                    )
                current_tags = [
                    {"tag": str(row["tag"]), "is_namespace": bool(row["is_namespace"])}
                    for row in conn.execute(
                        "SELECT tag, is_namespace FROM task_tags WHERE task_id=? ORDER BY tag",
                        (staged["task_id"],),
                    )
                ]
                if not active:
                    if canonical_sha256({"row": dict(current), "tags": current_tags}) != str(
                        staged["expected_row_sha256"]
                    ):
                        raise CutoverPreconditionError(
                            f"An identified task changed after shadow parity: {staged['task_id']}"
                        )
                else:
                    fields = json.loads(str(staged["fields_json"]))
                    tags = [
                        {
                            "tag": str(tag),
                            "is_namespace": str(tag).startswith("projects/") or "/" in str(tag),
                        }
                        for tag in json.loads(str(staged["tags_json"]))
                    ]
                    if any(
                        current[key] != fields.get(key)
                        for key in (
                            "description",
                            "note_uuid",
                            "due_date",
                            "legacy_import_receipt_id",
                        )
                    ) or current_tags != tags:
                        raise CutoverPreconditionError(
                            f"An activated task differs from its staged receipt: {staged['task_id']}"
                        )
            existing_idless = {
                str(row[0])
                for row in conn.execute(
                    "SELECT task_id FROM task_metadata WHERE task_id IN "
                    "(SELECT task_id FROM task_migration_idless_stage WHERE cohort_id=?)",
                    (self.inventory.cohort_id,),
                )
            }
            if active:
                for staged in idless_rows:
                    current = conn.execute(
                        "SELECT * FROM task_metadata WHERE task_id=?", (staged["task_id"],)
                    ).fetchone()
                    fields = json.loads(str(staged["fields_json"]))
                    current_tags = [
                        {"tag": str(row["tag"]), "is_namespace": bool(row["is_namespace"])}
                        for row in conn.execute(
                            "SELECT tag, is_namespace FROM task_tags WHERE task_id=? ORDER BY tag",
                            (staged["task_id"],),
                        )
                    ]
                    expected_tags = sorted(
                        (
                            {
                                "tag": str(tag),
                                "is_namespace": str(tag).startswith("projects/")
                                or "/" in str(tag),
                            }
                            for tag in json.loads(str(staged["tags_json"]))
                        ),
                        key=lambda item: item["tag"],
                    )
                    compared = {
                        "state": fields.get("state"),
                        "urgency": fields.get("urgency"),
                        "note_uuid": fields.get("note_uuid"),
                        "completed_at": fields.get("completed_at"),
                        "archived_at": fields.get("archived_at"),
                        "description": fields.get("description"),
                        "due_date": fields.get("due_date"),
                        "creation_provenance": fields.get("creation_provenance"),
                        "legacy_import_receipt_id": fields.get("legacy_import_receipt_id"),
                    }
                    if current is None or any(current[key] != value for key, value in compared.items()):
                        raise CutoverPreconditionError(
                            f"An activated ID-less task differs from staging: {staged['task_id']}"
                        )
                    if current_tags != expected_tags:
                        raise CutoverPreconditionError(
                            f"An activated ID-less task has different tags: {staged['task_id']}"
                        )
            task_count = int(conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0])
        actual_existing = {(str(row["source_key"]), str(row["task_id"])) for row in existing_rows}
        actual_idless = {
            (str(row["source_key"]), str(row["task_id"]), str(row["line_sha256"]))
            for row in idless_rows
        }
        if actual_existing != expected_existing or actual_idless != expected_idless:
            raise CutoverPreconditionError("The staged task cohort is missing or has extra task rows.")
        expected_active_idless = {str(row["task_id"]) for row in idless_rows} if active else set()
        if existing_idless != expected_active_idless:
            raise CutoverPreconditionError("ID-less staged tasks leaked before, or vanished after, activation.")
        expected_task_count = int(self.inventory.counts.get("database_tasks", 0)) + (
            len(idless_rows) if active else 0
        )
        if task_count != expected_task_count:
            raise CutoverPreconditionError("The live task-row count no longer matches the cohort.")
        if any((row["activated_at"] is not None) != active for row in existing_rows + idless_rows):
            raise CutoverPreconditionError("A staged task row has an impossible activation state.")
        return {
            "schema": "wb.native-task-shadow-task-parity/v1",
            "identified_rows": len(existing_rows),
            "idless_rows": len(idless_rows),
            "stage_sha256": _rows_digest([*existing_rows, *idless_rows]),
        }

    def _shadow_document_evidence(self) -> Mapping[str, Any]:
        expected = {
            str(item.note_uuid): item
            for item in self.inventory.items
            if item.classification in _IMPORTABLE_DOCUMENT_CLASSES and item.note_uuid
        }
        with self._connect_ro() as conn:
            cohort = self._cohort(conn)
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM task_migration_document_stage WHERE cohort_id=? ORDER BY note_uuid",
                    (self.inventory.cohort_id,),
                )
            ]
        actual = {str(row["note_uuid"]): row for row in rows}
        if len(actual) != len(rows) or set(actual) != set(expected):
            raise CutoverPreconditionError("The staged document cohort is missing or has extra notes.")
        active = str(cohort["state"]) == "active"
        for note_uuid, item in expected.items():
            row = actual[note_uuid]
            if (
                str(row["classification"]) != item.classification
                or (row["task_id"] or None) != item.task_id
                or str(row["source_content_sha256"]) != item.content_sha256
                or not bool(row["byte_parity"])
                or not bool(row["normalized_parity"])
                or not str(row.get("source_receipt_id") or "")
                # Recovery-only documents deliberately have no task binding,
                # so activation never marks their staging row as activated.
                or (row["activated_at"] is not None)
                != (active and item.task_id is not None)
            ):
                raise CutoverPreconditionError(f"Staged document parity changed: {note_uuid}")
        return {
            "schema": "wb.native-task-shadow-document-parity/v1",
            "document_count": len(rows),
            "current_bindings": sum(
                row["lifecycle"] == "current" and row["binding_id"] is not None for row in rows
            ),
            "retired_bindings": sum(row["lifecycle"] == "retired" for row in rows),
            "recovery_documents": sum(row["task_id"] is None for row in rows),
            "stage_sha256": _rows_digest(rows),
        }

    @staticmethod
    def _root_id(manifest_sha256: str) -> str:
        return "root_" + hashlib.sha256(
            f"work-buddy-frozen-task-root/v1\0{manifest_sha256}".encode("utf-8")
        ).hexdigest()[:32]

    @staticmethod
    def _link_id(root_id: str, document_id: str, relative_path: str, sha256: str) -> str:
        return "lf_" + hashlib.sha256(
            f"work-buddy-local-link/v2\0{root_id}\0{document_id}\0{relative_path}\0{sha256}".encode(
                "utf-8"
            )
        ).hexdigest()[:32]

    def _attachment_evidence(self) -> Mapping[str, Any]:
        notes_by_path = {
            str(item.relative_path): item
            for item in self.inventory.items
            if item.classification in _IMPORTABLE_DOCUMENT_CLASSES
            and item.relative_path
            and item.note_uuid
        }
        expected: dict[tuple[str, str], Any] = {}
        for item in self.inventory.items:
            if item.classification not in _LOCAL_FILE_CLASSES or not item.relative_path:
                continue
            for referrer in item.metadata.get("referenced_by", []):
                note = notes_by_path.get(str(referrer))
                if note is not None:
                    expected[(str(note.note_uuid), item.relative_path)] = item
        with self._connect_ro() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM task_migration_local_link_stage WHERE cohort_id=? "
                    "ORDER BY note_uuid, relative_path",
                    (self.inventory.cohort_id,),
                )
            ]
            documents = {
                str(row["note_uuid"]): dict(row)
                for row in conn.execute(
                    "SELECT note_uuid, document_id, rewrite_manifest_json "
                    "FROM task_migration_document_stage WHERE cohort_id=?",
                    (self.inventory.cohort_id,),
                )
            }
        actual = {(str(row["note_uuid"]), str(row["relative_path"])): row for row in rows}
        if len(actual) != len(rows) or set(actual) != set(expected):
            raise CutoverPreconditionError("The staged linked-file cohort is not exact.")
        root_id = self._root_id(self.inventory.manifest_sha256)
        for identity, item in expected.items():
            row = actual[identity]
            note_uuid, relative = identity
            document = documents.get(note_uuid)
            if document is None:
                raise CutoverPreconditionError(f"Linked file has no staged document: {note_uuid}")
            expected_link_id = self._link_id(
                root_id,
                str(document["document_id"]),
                relative,
                str(item.content_sha256),
            )
            expected_action = "reveal" if PurePosixPath(relative).suffix.casefold() == ".ppk" else "open"
            rewrites = json.loads(str(document["rewrite_manifest_json"]))
            rewrite_ids = {str(rewrite.get("link_id")) for rewrite in rewrites}
            if (
                str(row["root_id"]) != root_id
                or str(row["link_id"]) != expected_link_id
                or str(row["sha256"]) != item.content_sha256
                or int(row["byte_length"]) != int(item.byte_length or 0)
                or str(row["allowed_action"]) != expected_action
                or expected_link_id not in rewrite_ids
            ):
                raise CutoverPreconditionError(f"Linked-file parity changed: {relative}")
        return {
            "schema": "wb.native-task-attachment-parity/v1",
            "root_id": root_id,
            "linked_files": len(rows),
            "stage_sha256": _rows_digest(rows),
        }

    def _receipt_time(self, receipt: Mapping[str, Any], path: Path) -> datetime:
        for key in ("completed_at", "created_at", "verified_at", "snapshot_ts"):
            if receipt.get(key):
                return _parse_time(receipt[key])
        snapshot_id = str(receipt.get("snapshot_id") or "")
        match = _SNAPSHOT_ID.match(snapshot_id)
        if match:
            return datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%SZ").replace(
                tzinfo=timezone.utc
            )
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    def _verify_tree_zip(self, archive: Path) -> Mapping[str, Any]:
        expected = {entry.relative_path: entry for entry in self.manifest}
        observed: dict[str, tuple[int, str]] = {}
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                if info.is_dir():
                    continue
                name = _safe_relative(info.filename)
                if name.startswith("tasks/"):
                    name = name.removeprefix("tasks/")
                name = _safe_relative(name)
                if name in observed or info.flag_bits & 0x1:
                    raise CutoverPreconditionError("The legacy ZIP is duplicate-bearing or encrypted.")
                digest = hashlib.sha256()
                length = 0
                with bundle.open(info) as stream:
                    while chunk := stream.read(1024 * 1024):
                        length += len(chunk)
                        digest.update(chunk)
                observed[name] = (length, digest.hexdigest())
        if set(observed) != set(expected):
            raise CutoverPreconditionError("The legacy ZIP membership differs from the manifest.")
        for relative, entry in expected.items():
            if observed[relative] != (entry.byte_length, entry.sha256):
                raise CutoverPreconditionError(f"The legacy ZIP entry changed: {relative}")
        return {"entry_count": len(observed), "entry_manifest_sha256": self._manifest_digest()}

    def _verify_snapshot_tar(self, archive: Path) -> Mapping[str, Any]:
        with tarfile.open(archive, mode="r:*") as bundle:
            members = bundle.getmembers()
            names: set[str] = set()
            for member in members:
                name = _safe_relative(member.name)
                if name in names or member.issym() or member.islnk():
                    raise CutoverPreconditionError("The data snapshot contains links or duplicates.")
                names.add(name)
            if "task_metadata.db" not in names or "MANIFEST.json" not in names:
                raise CutoverPreconditionError("The data snapshot omits Task DB or MANIFEST.json.")
            manifest_stream = bundle.extractfile("MANIFEST.json")
            if manifest_stream is None:
                raise CutoverPreconditionError("The data snapshot manifest is unreadable.")
            snapshot_manifest = json.loads(manifest_stream.read().decode("utf-8"))
            task_db_stream = bundle.extractfile("task_metadata.db")
            if task_db_stream is None:
                raise CutoverPreconditionError("The snapshot Task DB member is unreadable.")
            task_db_digest = hashlib.sha256()
            task_db_bytes = 0
            while chunk := task_db_stream.read(1024 * 1024):
                task_db_digest.update(chunk)
                task_db_bytes += len(chunk)
        expected_tasks = int(self.inventory.counts.get("database_tasks", 0))
        observed_tasks = int(
            snapshot_manifest.get("row_counts", {}).get("tasks", {}).get("task_metadata", -1)
        )
        if observed_tasks != expected_tasks:
            raise CutoverPreconditionError("The fresh snapshot has the wrong pre-cutover task count.")
        return {
            "member_count": len(names),
            "task_rows": observed_tasks,
            "snapshot_ts": snapshot_manifest.get("snapshot_ts"),
            "task_db_member_sha256": task_db_digest.hexdigest(),
            "task_db_member_bytes": task_db_bytes,
        }

    def _backup_evidence(self) -> Mapping[str, Any]:
        receipts = _json_value(self.paths.backup_receipts, expected=list)
        required = {
            "work_buddy_data_snapshot",
            "exact_legacy_task_tree",
            "legacy_task_tree_manifest",
        }
        verified: dict[str, dict[str, Any]] = {}
        now = self.clock().astimezone(timezone.utc)
        for raw in receipts:
            if not isinstance(raw, Mapping):
                raise CutoverPreconditionError("Every backup receipt must be an object.")
            kind = str(raw.get("kind") or "")
            if kind not in required:
                continue
            if kind in verified:
                raise CutoverPreconditionError(f"Duplicate required backup receipt: {kind}")
            artifact = _regular_file(Path(str(raw.get("path") or "")), label=kind)
            digest = _sha256_file(artifact)
            if not _SHA256.fullmatch(str(raw.get("sha256") or "")) or digest != raw["sha256"]:
                raise CutoverPreconditionError(f"Backup hash mismatch: {kind}")
            if raw.get("size_bytes") is not None and artifact.stat().st_size != int(raw["size_bytes"]):
                raise CutoverPreconditionError(f"Backup size mismatch: {kind}")
            detail: Mapping[str, Any] = {}
            if kind == "work_buddy_data_snapshot":
                completed = self._receipt_time(raw, artifact)
                age = now - completed
                if age < timedelta(minutes=-5) or age > self.backup_freshness:
                    raise CutoverPreconditionError("The Work Buddy data backup is not fresh enough.")
                detail = {**self._verify_snapshot_tar(artifact), "completed_at": _iso(completed)}
            elif kind == "exact_legacy_task_tree":
                detail = self._verify_tree_zip(artifact)
                if raw.get("entry_count") is not None and int(raw["entry_count"]) != detail["entry_count"]:
                    raise CutoverPreconditionError("The legacy ZIP entry receipt is stale.")
            else:
                manifest_path = _regular_file(self.paths.manifest, label="legacy manifest")
                try:
                    same = os.path.samefile(artifact, manifest_path)
                except OSError:
                    same = False
                if not same or self._manifest_digest() != self.inventory.manifest_sha256:
                    raise CutoverPreconditionError("The backup manifest is not the cohort manifest.")
                if raw.get("row_count") is not None and int(raw["row_count"]) != len(self.manifest):
                    raise CutoverPreconditionError("The manifest row-count receipt is stale.")
                detail = {"row_count": len(self.manifest)}
            verified[kind] = {
                "path": str(artifact),
                "sha256": digest,
                "bytes": artifact.stat().st_size,
                **dict(detail),
            }
        missing = sorted(required - set(verified))
        if missing:
            raise CutoverPreconditionError(
                "Required backup artifacts are missing.", details={"missing": missing}
            )
        return {
            "schema": "wb.native-task-backup-verification/v1",
            "receipt_file_sha256": _sha256_file(self.paths.backup_receipts),
            "artifacts": verified,
        }

    @staticmethod
    def _table_digest(
        conn: sqlite3.Connection,
        table: str,
        cohort_id: str | None = None,
        *,
        ignored_columns: Iterable[str] = (),
    ) -> str:
        if cohort_id is None:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE cohort_id=? ORDER BY rowid", (cohort_id,)
            ).fetchall()
        ignored = frozenset(ignored_columns)
        return _rows_digest(
            [
                {key: value for key, value in dict(row).items() if key not in ignored}
                for row in rows
            ]
        )

    @staticmethod
    def _extract_snapshot_task_db(snapshot: Path, destination: Path) -> str:
        with tarfile.open(snapshot, mode="r:*") as bundle:
            members = [member for member in bundle.getmembers() if member.name == "task_metadata.db"]
            if len(members) != 1 or not members[0].isfile():
                raise CutoverPreconditionError(
                    "The verified snapshot does not contain one regular Task DB member."
                )
            stream = bundle.extractfile(members[0])
            if stream is None:
                raise CutoverPreconditionError("The snapshot Task DB member is unreadable.")
            digest = hashlib.sha256()
            with destination.open("xb") as target:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        return digest.hexdigest()

    def _verify_portable_restore(
        self,
        *,
        cowork_export: Path,
        causality_export: Path,
        target: Path,
    ) -> Mapping[str, Any]:
        with self._connect_ro() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM task_migration_document_stage WHERE cohort_id=? "
                    "ORDER BY note_uuid",
                    (self.inventory.cohort_id,),
                )
            ]
            transition_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM task_migration_binding_transitions "
                    "WHERE cohort_id=? ORDER BY binding_id, direction",
                    (self.inventory.cohort_id,),
                )
            ]
        stage_sha256 = canonical_sha256(
            {
                "document_stage": rows,
                "binding_transitions": transition_rows,
            }
        )
        # Binding application mutates the transition ledger. Include its exact
        # digest so pre-binding restore evidence can never be replayed for a
        # post-binding activation on the same operator instance.
        cache_key = (
            _sha256_file(cowork_export),
            _sha256_file(causality_export),
            stage_sha256,
        )
        cached = self._portable_restore_cache.get(cache_key)
        if cached is not None:
            return dict(cached)
        target.mkdir()
        registry = TruthStoreRegistry(target.parent / "isolated-truth-registry.db")
        try:
            imported = import_store(
                cowork_export,
                target,
                registry=registry,
                causality_source=causality_export,
                causality_sha256=_sha256_file(causality_export),
            )
        except Exception as exc:
            raise CutoverPreconditionError(
                "The portable task Co-work/causality artifacts cannot be restored."
            ) from exc
        restored = imported.store
        store_ids = {str(row["store_id"]) for row in rows}
        if store_ids != {restored.store_id}:
            raise CutoverPreconditionError(
                "The restored portable store identity differs from the staged cohort."
            )
        restored_conn = restored.connect()
        try:
            restored_ids = {
                str(row[0]) for row in restored_conn.execute("SELECT id FROM documents")
            }
        finally:
            restored_conn.close()
        expected_ids = {str(row["document_id"]) for row in rows}
        if restored_ids != expected_ids:
            raise CutoverPreconditionError(
                "The restored portable store has missing or extra task documents."
            )
        causality = DocumentCausalityStore(restored.paths.sidecar)
        transitions = {
            str(row["binding_id"]): row
            for row in transition_rows
            if str(row.get("direction") or "") == "to_cowork"
        }
        expected_current_binding_ids = {
            str(row["binding_id"])
            for row in rows
            if row["lifecycle"] != "retired" and row.get("binding_id")
        }
        if (
            len(transitions) != len(transition_rows)
            or not set(transitions).issubset(expected_current_binding_ids)
            or any(
                str(row.get("after_authority") or "") != "co_work"
                for row in transitions.values()
            )
        ):
            raise CutoverPreconditionError(
                "The staged binding transition cohort is not activation-safe."
            )
        staged_by_binding = {
            str(row["binding_id"]): row for row in rows if row.get("binding_id")
        }
        replayed_transitions: list[dict[str, Any]] = []
        for binding_id, transition in sorted(transitions.items()):
            staged = staged_by_binding[binding_id]
            binding = causality.get_binding(binding_id)
            try:
                before_epoch = int(transition["before_epoch"])
                after_epoch = int(transition["after_epoch"])
            except (TypeError, ValueError) as exc:
                raise CutoverPreconditionError(
                    f"Binding transition epochs are invalid: {binding_id}"
                ) from exc
            if (
                binding is None
                or binding.lifecycle != "current"
                or binding.content_authority != str(transition["before_authority"])
                or binding.content_authority_epoch != before_epoch
                or str(transition["domain_revision"])
                != str(staged["source_content_sha256"])
            ):
                raise CutoverPreconditionError(
                    f"The restored portable binding cannot replay its transition: {binding_id}"
                )
            updated = causality.cutover_to_cowork(
                binding_id,
                domain_revision=str(transition["domain_revision"]),
            )
            if (
                updated.content_authority != str(transition["after_authority"])
                or updated.content_authority_epoch != after_epoch
                or updated.domain_revision != str(transition["domain_revision"])
            ):
                raise CutoverPreconditionError(
                    f"The isolated binding replay differs from its receipt: {binding_id}"
                )
            replayed_transitions.append(
                {
                    "binding_id": binding_id,
                    "before_authority": str(transition["before_authority"]),
                    "before_epoch": before_epoch,
                    "after_authority": updated.content_authority,
                    "after_epoch": updated.content_authority_epoch,
                    "domain_revision": updated.domain_revision,
                }
            )
        expected_binding_ids: set[str] = set()
        verified_rows: list[dict[str, Any]] = []
        for row in rows:
            document = documents.get_document(restored, str(row["document_id"]))
            if (
                document.ydoc_snapshot_sha256 is None
                or document.content_sha256 != str(row["document_content_sha256"])
            ):
                raise CutoverPreconditionError(
                    f"Restored portable task document differs: {row['note_uuid']}"
                )
            head = ydoc_store.current_structured_head(
                restored,
                document_id=document.id,
                snapshot_sha256=document.ydoc_snapshot_sha256,
            )
            lifecycle = documents.current_lifecycle(restored, document.id)
            expected_lifecycle = "retired" if row["lifecycle"] == "retired" else "active"
            if head != str(row["document_head_sha256"]) or lifecycle != expected_lifecycle:
                raise CutoverPreconditionError(
                    f"Restored portable task document head/lifecycle differs: {row['note_uuid']}"
                )
            binding_id = str(row.get("binding_id") or "")
            if binding_id:
                binding = causality.get_binding(binding_id)
                transition = transitions.get(binding_id)
                expected_authority = (
                    str(transition["after_authority"])
                    if transition is not None
                    else "domain"
                )
                expected_epoch = (
                    int(transition["after_epoch"])
                    if transition is not None
                    else None
                )
                if (
                    binding is None
                    or binding.store_id != restored.store_id
                    or binding.document_id != document.id
                    or binding.domain_namespace != "tasks"
                    or binding.domain_kind != "task_knowledge"
                    or binding.role != "task_knowledge"
                    or binding.domain_revision != str(row["source_content_sha256"])
                    or binding.projection_mode != "none"
                    or binding.projection_path is not None
                    or binding.migration_origin != "legacy-task-cohort/v1"
                    or (
                        row["lifecycle"] == "retired"
                        and (
                            binding.lifecycle != "retired"
                            or binding.content_authority != "domain"
                            or transition is not None
                        )
                    )
                    or (
                        row["lifecycle"] != "retired"
                        and (
                            binding.lifecycle != "current"
                            or binding.content_authority != expected_authority
                            or (
                                expected_epoch is not None
                                and binding.content_authority_epoch != expected_epoch
                            )
                        )
                    )
                ):
                    raise CutoverPreconditionError(
                        f"Restored portable task binding differs: {binding_id}"
                    )
                expected_binding_ids.add(binding_id)
            verified_rows.append(
                {
                    "note_uuid": row["note_uuid"],
                    "document_id": document.id,
                    "content_sha256": document.content_sha256,
                    "head_sha256": head,
                    "lifecycle": lifecycle,
                    "binding_id": binding_id or None,
                }
            )
        with causality.connection() as conn:
            actual_binding_ids = {
                str(row[0])
                for row in conn.execute(
                    "SELECT binding_id FROM domain_document_bindings "
                    "WHERE domain_namespace='tasks' AND domain_kind='task_knowledge' "
                    "AND role='task_knowledge' AND migration_origin='legacy-task-cohort/v1'"
                )
            }
        if actual_binding_ids != expected_binding_ids:
            raise CutoverPreconditionError(
                "The restored portable task binding cohort is not exact."
            )
        evidence = {
            "store_id": restored.store_id,
            "document_count": len(rows),
            "binding_count": len(expected_binding_ids),
            "binding_transition_replay_count": len(replayed_transitions),
            "binding_transition_replays_sha256": canonical_sha256(replayed_transitions),
            "stage_sha256": stage_sha256,
            "restored_rows_sha256": canonical_sha256(verified_rows),
            "export_record_count": imported.record_count,
            "export_blob_count": imported.blob_count,
        }
        self._portable_restore_cache[cache_key] = evidence
        return evidence

    def _restore_evidence(self, backup: Mapping[str, Any]) -> Mapping[str, Any]:
        receipt = _json_value(self.paths.restore_rehearsal, expected=dict)
        if receipt.get("schema") != RESTORE_RECEIPT_SCHEMA:
            raise CutoverPreconditionError("The restore rehearsal receipt schema is unsupported.")
        if str(receipt.get("cohort_id") or "") != self.inventory.cohort_id:
            raise CutoverPreconditionError("The restore rehearsal belongs to another cohort.")
        if (
            str(receipt.get("inventory_sha256") or "") != self.inventory.inventory_sha256
            or str(receipt.get("manifest_sha256") or "") != self.inventory.manifest_sha256
        ):
            raise CutoverPreconditionError("The restore rehearsal digests are stale.")
        completed = _parse_time(receipt.get("completed_at"))
        age = self.clock().astimezone(timezone.utc) - completed
        if age < timedelta(minutes=-5) or age > self.backup_freshness:
            raise CutoverPreconditionError("The restore rehearsal is not fresh enough.")

        snapshot = backup.get("artifacts", {}).get("work_buddy_data_snapshot")
        if not isinstance(snapshot, Mapping):
            raise CutoverPreconditionError("The verified data snapshot evidence is missing.")
        snapshot_path = _regular_file(Path(str(snapshot.get("path") or "")), label="snapshot")
        if str(receipt.get("restored_from_sha256") or "") != str(snapshot.get("sha256") or ""):
            raise CutoverPreconditionError("The rehearsal is not tied to the verified fresh backup.")
        expected_db_sha = str(receipt.get("restored_task_db_sha256") or "")
        if (
            not _SHA256.fullmatch(expected_db_sha)
            or expected_db_sha != str(snapshot.get("task_db_member_sha256") or "")
        ):
            raise CutoverPreconditionError(
                "The restore receipt does not match the Task DB embedded in the verified snapshot."
            )

        portable: dict[str, dict[str, Any]] = {}
        portable_paths: dict[str, Path] = {}
        for item in receipt.get("portable_artifacts") or []:
            if not isinstance(item, Mapping):
                raise CutoverPreconditionError("Portable rehearsal artifact must be an object.")
            kind = str(item.get("kind") or "")
            if kind in portable:
                raise CutoverPreconditionError(f"Duplicate portable rehearsal artifact: {kind}")
            artifact = _regular_file(Path(str(item.get("path") or "")), label=kind)
            digest = _sha256_file(artifact)
            if digest != str(item.get("sha256") or ""):
                raise CutoverPreconditionError(f"Portable rehearsal artifact hash mismatch: {kind}")
            portable[kind] = {"sha256": digest, "bytes": artifact.stat().st_size}
            portable_paths[kind] = artifact
        missing = sorted(_PORTABLE_REHEARSAL_KINDS - set(portable))
        if missing:
            raise CutoverPreconditionError(
                "The restore rehearsal lacks portable Co-work/causality artifacts.",
                details={"missing": missing},
            )

        cohort_tables = (
            "task_migration_inventory",
            "task_migration_idless_stage",
            "task_migration_existing_task_stage",
            "task_migration_document_stage",
            "task_migration_local_link_stage",
        )
        global_tables = ("task_metadata", "task_tags", "task_state_history")
        digests: dict[str, Any] = {}
        with tempfile.TemporaryDirectory(prefix="wb-task-restore-rehearsal-") as temporary:
            isolated = Path(temporary)
            restored_db = isolated / "task_metadata.db"
            extracted_sha = self._extract_snapshot_task_db(snapshot_path, restored_db)
            if extracted_sha != expected_db_sha:
                raise CutoverPreconditionError(
                    "The extracted snapshot Task DB hash changed during rehearsal."
                )
            live = self._connect_ro()
            restored = self._connect_ro(restored_db)
            try:
                if str(restored.execute("PRAGMA integrity_check").fetchone()[0]).casefold() != "ok":
                    raise CutoverPreconditionError(
                        "The restored Task DB failed SQLite integrity_check."
                    )
                restored_cohort = restored.execute(
                    "SELECT inventory_sha256, manifest_sha256 FROM task_migration_cohorts "
                    "WHERE cohort_id=?",
                    (self.inventory.cohort_id,),
                ).fetchone()
                if restored_cohort is None or tuple(restored_cohort) != (
                    self.inventory.inventory_sha256,
                    self.inventory.manifest_sha256,
                ):
                    raise CutoverPreconditionError(
                        "The restored DB does not contain the accepted cohort."
                    )
                live_cohort = self._cohort(live)
                active = str(live_cohort["state"]) == "active"
                for table in cohort_tables:
                    ignored = (
                        ("activated_at",)
                        if table != "task_migration_inventory"
                        else ()
                    )
                    live_digest = self._table_digest(
                        live,
                        table,
                        self.inventory.cohort_id,
                        ignored_columns=ignored,
                    )
                    restored_digest = self._table_digest(
                        restored,
                        table,
                        self.inventory.cohort_id,
                        ignored_columns=ignored,
                    )
                    if live_digest != restored_digest:
                        raise CutoverPreconditionError(
                            f"Restored staging table differs: {table}"
                        )
                    digests[table] = live_digest
                for table in global_tables:
                    live_digest = self._table_digest(live, table)
                    restored_digest = self._table_digest(restored, table)
                    if not active and live_digest != restored_digest:
                        raise CutoverPreconditionError(
                            f"Restored task table differs: {table}"
                        )
                    digests[table] = {
                        "live": live_digest,
                        "restored": restored_digest,
                        "comparison": (
                            "active-task-parity-separate" if active else "exact"
                        ),
                    }
            finally:
                restored.close()
                live.close()
            portable_restore = self._verify_portable_restore(
                cowork_export=portable_paths["cowork_task_store"],
                causality_export=portable_paths["task_causality"],
                target=isolated / "cowork-restore",
            )
        return {
            "schema": RESTORE_RECEIPT_SCHEMA,
            "completed_at": _iso(completed),
            "restored_task_db_sha256": expected_db_sha,
            "table_digests": digests,
            "portable_artifacts": portable,
            "portable_restore": portable_restore,
            "receipt_file_sha256": _sha256_file(self.paths.restore_rehearsal),
        }

    @staticmethod
    def _read_pid(path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CutoverPreconditionError(f"PID file is unreadable: {path}") from exc

    @staticmethod
    def _list_processes() -> Sequence[Mapping[str, Any]]:
        if sys.platform == "win32":
            script = (
                "Get-CimInstance Win32_Process | "
                "Select-Object Name,ProcessId,ParentProcessId,CommandLine,ExecutablePath | "
                "ConvertTo-Json -Compress -Depth 3"
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            decoded = json.loads(completed.stdout or "[]")
            values = decoded if isinstance(decoded, list) else [decoded]
            rows: list[dict[str, Any]] = []
            for row in values:
                if not isinstance(row, Mapping):
                    continue
                try:
                    rows.append(
                        {
                            "name": str(row.get("Name") or ""),
                            "pid": int(row.get("ProcessId") or 0),
                            "parent_pid": int(row.get("ParentProcessId") or 0),
                            "command_line": str(row.get("CommandLine") or ""),
                            "executable_path": str(row.get("ExecutablePath") or ""),
                        }
                    )
                except (TypeError, ValueError):
                    continue
            return rows
        proc = Path("/proc")
        rows = []
        if proc.is_dir():
            for child in proc.iterdir():
                if not child.name.isdigit():
                    continue
                try:
                    status_lines = (child / "status").read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    parent_pid = next(
                        (
                            int(line.split(":", 1)[1].strip())
                            for line in status_lines
                            if line.startswith("PPid:")
                        ),
                        0,
                    )
                    rows.append(
                        {
                            "pid": int(child.name),
                            "parent_pid": parent_pid,
                            "name": (child / "comm").read_text(encoding="utf-8").strip(),
                            "command_line": (child / "cmdline")
                            .read_bytes()
                            .replace(b"\0", b" ")
                            .decode("utf-8", errors="replace"),
                            "executable_path": str((child / "exe").resolve(strict=True)),
                        }
                    )
                except (OSError, ValueError):
                    continue
        return rows

    @staticmethod
    def _frontmatter(path: Path) -> Mapping[str, Any]:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return {}
        parts = text.split("---", 2)
        if len(parts) != 3:
            return {}
        value = yaml.safe_load(parts[1]) or {}
        return value if isinstance(value, Mapping) else {}

    def _producer_jobs(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for root in self.paths.job_roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.md")):
                metadata = self._frontmatter(path)
                capability = str(metadata.get("capability") or "")
                # task-note-index is authority-aware: after activation it
                # indexes Co-work heads, and before activation the stopped
                # process set prevents it from running during the flip. It is
                # therefore a valid native reader, not a legacy producer.
                known_legacy_file = path.stem.casefold() == "task-sync"
                if capability not in TASK_MUTATION_CAPABILITIES and not known_legacy_file:
                    continue
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    relative = path.name
                rows.append(
                    {
                        "root": root.name,
                        "path": relative,
                        "capability": capability,
                        "enabled": bool(metadata.get("enabled", True)),
                        "sha256": _sha256_file(path),
                    }
                )
        return rows

    def _operation_record(self, operation_id: str) -> Mapping[str, Any] | None:
        if not re.fullmatch(r"op_[A-Za-z0-9_-]+", operation_id):
            return None
        path = self.paths.operations / f"{operation_id}.json"
        try:
            source = _regular_file(path, label=f"operation {operation_id}")
            value = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CutoverPreconditionError(
                f"Unreadable operation record: {path.name}"
            ) from exc
        return value if isinstance(value, Mapping) else None

    def _effective_retry(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        current = record
        seen: set[str] = set()
        while str(current.get("name") or "") in _RETRY_WRAPPERS:
            params = current.get("params") if isinstance(current.get("params"), Mapping) else {}
            inner_id = str(params.get("operation_id") or "")
            if not re.fullmatch(r"op_[A-Za-z0-9_-]+", inner_id):
                raise CutoverPreconditionError(
                    "A queued retry wrapper has an invalid inner operation ID."
                )
            if inner_id in seen:
                raise CutoverPreconditionError("A queued retry wrapper contains a cycle.")
            seen.add(inner_id)
            inner = self._operation_record(inner_id)
            if inner is None:
                raise CutoverPreconditionError(
                    f"A queued retry wrapper references a missing operation: {inner_id}"
                )
            current = inner
        return current

    @staticmethod
    def _queued(record: Mapping[str, Any]) -> bool:
        return bool(record.get("queued") or record.get("queued_for_retry"))

    def _retry_target_snapshots(self) -> list[dict[str, Any]]:
        """Return exact carrier bytes for queued, non-native task mutations."""

        rows: list[dict[str, Any]] = []
        if not self.paths.operations.is_dir():
            return rows
        root = self.paths.operations.resolve(strict=True)
        for supplied in sorted(root.glob("op_*.json")):
            try:
                path = _regular_file(supplied, label="operation record")
                if path.parent != root:
                    raise CutoverPreconditionError(
                        f"Operation record escaped its queue root: {supplied.name}"
                    )
                exact = path.read_bytes()
                record = json.loads(exact.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CutoverPreconditionError(
                    f"Unreadable operation record: {supplied.name}"
                ) from exc
            if not isinstance(record, Mapping):
                raise CutoverPreconditionError(f"Invalid operation record: {supplied.name}")
            if not self._queued(record):
                continue
            carrier_id = str(record.get("operation_id") or path.stem)
            if carrier_id != path.stem or not re.fullmatch(r"op_[A-Za-z0-9_-]+", carrier_id):
                raise CutoverPreconditionError(
                    f"Queued operation identity does not match its file: {path.name}"
                )
            effective = self._effective_retry(record)
            name = str(effective.get("name") or "")
            epoch = str(effective.get("task_authority_epoch") or "legacy")
            if name not in TASK_MUTATION_CAPABILITIES or is_native_authority_epoch(epoch):
                continue
            rows.append(
                {
                    "path": path,
                    "record": dict(record),
                    "exact_bytes": exact,
                    "operation_id": carrier_id,
                    "effective_operation_id": str(
                        effective.get("operation_id") or carrier_id
                    ),
                    "name": name,
                    "task_authority_epoch": epoch,
                }
            )
        return rows

    def _pending_legacy_retries(self) -> list[dict[str, Any]]:
        return [
            {
                "operation_id": row["operation_id"],
                "effective_operation_id": row["effective_operation_id"],
                "name": row["name"],
                "task_authority_epoch": row["task_authority_epoch"],
                "record_sha256": hashlib.sha256(row["exact_bytes"]).hexdigest(),
            }
            for row in self._retry_target_snapshots()
        ]

    @staticmethod
    def _retry_cancellation_record(
        source: Mapping[str, Any],
        *,
        cohort_id: str,
        cancelled_at: str,
        plan_id: str,
        original_sha256: str,
    ) -> dict[str, Any]:
        reason = "native_task_cutover_cancelled_queued_legacy_task_mutation"
        replacement = dict(source)
        replacement.update(
            {
                "status": "cancelled",
                "queued": False,
                "queued_for_retry": False,
                "retry_at": None,
                "locked_until": None,
                "lease_token": None,
                "completed_at": cancelled_at,
                "cancelled_at": cancelled_at,
                "cancelled_reason": reason,
                "cancelled_for_cohort_id": cohort_id,
                "task_cutover_cancellation": {
                    "schema": RETRY_CANCELLATION_RECEIPT_SCHEMA,
                    "cohort_id": cohort_id,
                    "plan_id": plan_id,
                    "cancelled_at": cancelled_at,
                    "reason": reason,
                    "original_sha256": original_sha256,
                },
            }
        )
        return replacement

    def _retry_plan_path(self, plan_id: str) -> Path:
        cohort_key = hashlib.sha256(self.inventory.cohort_id.encode("utf-8")).hexdigest()[:12]
        return self.paths.receipts / f"retry-cancel.{cohort_key}.{plan_id}.json"

    @staticmethod
    def _retry_plan_payload_sha256(receipt: Mapping[str, Any]) -> str:
        return canonical_sha256(
            {key: value for key, value in receipt.items() if key != "payload_sha256"}
        )

    def _write_retry_plan(self, receipt: Mapping[str, Any]) -> Path:
        target = self._retry_plan_path(str(receipt["plan_id"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            receipt, indent=2, ensure_ascii=False, sort_keys=True
        ) + "\n"
        with file_lock(target, timeout=2.0):
            if target.exists():
                if target.read_text(encoding="utf-8") != encoded:
                    raise CutoverPreconditionError(
                        "A retry-cancellation plan identity was reused with other bytes."
                    )
            else:
                atomic_write_text(target, encoded)
        return target

    def _read_retry_plan(self, path: Path) -> Mapping[str, Any]:
        receipt = _json_value(path, expected=dict)
        if receipt.get("schema") != RETRY_CANCELLATION_RECEIPT_SCHEMA:
            raise CutoverPreconditionError(
                f"Retry-cancellation receipt schema is unsupported: {path.name}"
            )
        if str(receipt.get("cohort_id") or "") != self.inventory.cohort_id:
            raise CutoverPreconditionError(
                f"Retry-cancellation receipt belongs to another cohort: {path.name}"
            )
        supplied_hash = str(receipt.get("payload_sha256") or "")
        if (
            not _SHA256.fullmatch(supplied_hash)
            or self._retry_plan_payload_sha256(receipt) != supplied_hash
        ):
            raise CutoverPreconditionError(
                f"Retry-cancellation receipt was modified: {path.name}"
            )
        records = receipt.get("records")
        if not isinstance(records, list) or not records:
            raise CutoverPreconditionError(
                f"Retry-cancellation receipt has no records: {path.name}"
            )
        for record in records:
            if not isinstance(record, Mapping):
                raise CutoverPreconditionError(
                    f"Retry-cancellation receipt has an invalid record: {path.name}"
                )
            operation_id = str(record.get("operation_id") or "")
            if not re.fullmatch(r"op_[A-Za-z0-9_-]+", operation_id):
                raise CutoverPreconditionError(
                    f"Retry-cancellation receipt has an invalid operation ID: {path.name}"
                )
            try:
                original = base64.b64decode(
                    str(record.get("original_bytes_base64") or ""), validate=True
                )
                replacement = base64.b64decode(
                    str(record.get("replacement_bytes_base64") or ""), validate=True
                )
            except (ValueError, TypeError) as exc:
                raise CutoverPreconditionError(
                    f"Retry-cancellation receipt bytes are invalid: {path.name}"
                ) from exc
            if (
                hashlib.sha256(original).hexdigest()
                != str(record.get("original_sha256") or "")
                or hashlib.sha256(replacement).hexdigest()
                != str(record.get("replacement_sha256") or "")
            ):
                raise CutoverPreconditionError(
                    f"Retry-cancellation receipt byte hashes do not verify: {path.name}"
                )
        return receipt

    def _existing_retry_plans(self) -> list[tuple[Path, Mapping[str, Any]]]:
        if not self.paths.receipts.is_dir():
            return []
        cohort_key = hashlib.sha256(self.inventory.cohort_id.encode("utf-8")).hexdigest()[:12]
        plans: list[tuple[Path, Mapping[str, Any]]] = []
        for path in sorted(self.paths.receipts.glob(f"retry-cancel.{cohort_key}.*.json")):
            plans.append((path, self._read_retry_plan(path)))
        return plans

    def _apply_retry_plan(self, receipt: Mapping[str, Any]) -> int:
        """CAS every carrier under queue locks; either old or planned bytes are valid."""

        root = self.paths.operations.resolve(strict=True)
        entries: list[tuple[Path, bytes, bytes]] = []
        for item in receipt["records"]:
            path = root / f"{item['operation_id']}.json"
            source = _regular_file(path, label="retry-cancellation target")
            if source.parent != root:
                raise CutoverPreconditionError(
                    f"Retry-cancellation target escaped its queue root: {path.name}"
                )
            entries.append(
                (
                    source,
                    base64.b64decode(str(item["original_bytes_base64"]), validate=True),
                    base64.b64decode(str(item["replacement_bytes_base64"]), validate=True),
                )
            )
        changed = 0
        with ExitStack() as locks:
            for path, _original, _replacement in sorted(entries, key=lambda row: str(row[0])):
                locks.enter_context(file_lock(path, timeout=2.0))
            current: dict[Path, bytes] = {}
            for path, original, replacement in entries:
                observed = path.read_bytes()
                if observed not in {original, replacement}:
                    raise CutoverPreconditionError(
                        f"Retry operation changed after cancellation planning: {path.name}"
                    )
                current[path] = observed
            for path, original, replacement in entries:
                if current[path] == original:
                    atomic_write_text(path, replacement.decode("utf-8"))
                    changed += 1
            for path, _original, replacement in entries:
                if path.read_bytes() != replacement:
                    raise CutoverPreconditionError(
                        f"Retry cancellation did not verify atomically: {path.name}"
                    )
        return changed

    def cancel_legacy_retries(self, *, confirmation: str) -> Mapping[str, Any]:
        """Terminalize only queued legacy task mutations while all producers are stopped."""

        if confirmation != CANCEL_RETRIES_CONFIRMATION:
            raise CutoverPreconditionError(
                "Retry cancellation needs the exact operator confirmation token.",
                details={"required_confirmation": CANCEL_RETRIES_CONFIRMATION},
            )
        if not self.paths.operations.is_dir():
            raise CutoverPreconditionError("The operation queue directory is unavailable.")
        lock_target = self.paths.operations / ".native-task-cutover-cancel"
        with file_lock(lock_target, timeout=2.0):
            stopped = self._stopped_process_evidence()
            receipts: list[str] = []
            changed = 0
            # An immutable plan is written before its first queue mutation.
            # Replaying the action repairs any original bytes left by a crash.
            for path, plan in self._existing_retry_plans():
                changed += self._apply_retry_plan(plan)
                receipts.append(str(path))

            targets = self._retry_target_snapshots()
            if targets:
                cancelled_at = _iso(self.clock())
                identities = [
                    {
                        "operation_id": row["operation_id"],
                        "original_sha256": hashlib.sha256(row["exact_bytes"]).hexdigest(),
                    }
                    for row in targets
                ]
                plan_id = "cancel_" + canonical_sha256(
                    {
                        "schema": RETRY_CANCELLATION_RECEIPT_SCHEMA,
                        "cohort_id": self.inventory.cohort_id,
                        "records": identities,
                    }
                )[:32]
                planned_records: list[dict[str, Any]] = []
                for row, identity in zip(targets, identities, strict=True):
                    replacement_record = self._retry_cancellation_record(
                        row["record"],
                        cohort_id=self.inventory.cohort_id,
                        cancelled_at=cancelled_at,
                        plan_id=plan_id,
                        original_sha256=identity["original_sha256"],
                    )
                    replacement = (
                        json.dumps(
                            replacement_record,
                            indent=2,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode("utf-8")
                    planned_records.append(
                        {
                            **identity,
                            "effective_operation_id": row["effective_operation_id"],
                            "name": row["name"],
                            "task_authority_epoch": row["task_authority_epoch"],
                            "original_bytes_base64": base64.b64encode(
                                row["exact_bytes"]
                            ).decode("ascii"),
                            "replacement_sha256": hashlib.sha256(replacement).hexdigest(),
                            "replacement_bytes_base64": base64.b64encode(replacement).decode(
                                "ascii"
                            ),
                        }
                    )
                plan: dict[str, Any] = {
                    "schema": RETRY_CANCELLATION_RECEIPT_SCHEMA,
                    "cohort_id": self.inventory.cohort_id,
                    "plan_id": plan_id,
                    "prepared_at": cancelled_at,
                    "cancelled_reason": (
                        "native_task_cutover_cancelled_queued_legacy_task_mutation"
                    ),
                    "process_stop_evidence_sha256": canonical_sha256(stopped),
                    "records": planned_records,
                }
                plan["payload_sha256"] = self._retry_plan_payload_sha256(plan)
                receipt_path = self._write_retry_plan(plan)
                changed += self._apply_retry_plan(plan)
                receipts.append(str(receipt_path))

            pending = self._pending_legacy_retries()
            if pending:
                raise CutoverPreconditionError(
                    "Legacy task retry cancellation left queued mutations.",
                    details={"operations": pending},
                )
            return {
                "schema": RETRY_CANCELLATION_RECEIPT_SCHEMA,
                "cohort_id": self.inventory.cohort_id,
                "cancelled": changed,
                "replayed": changed == 0,
                "receipts": sorted(set(receipts)),
                "pending_legacy_task_retries": [],
                "process_stop_evidence": stopped,
            }

    def _stopped_process_evidence(self) -> Mapping[str, Any]:
        """Verify only the live process stop, for guarded queue maintenance."""

        state_path = _regular_file(self.paths.sidecar_state, label="sidecar state")
        state = _json_value(state_path, expected=dict)
        tracked: dict[int, set[str]] = {}

        def add(pid: object, role: str) -> None:
            if pid is None:
                return
            try:
                parsed = int(pid)
            except (TypeError, ValueError):
                raise CutoverPreconditionError(f"Invalid tracked PID for {role}.")
            if parsed > 0:
                tracked.setdefault(parsed, set()).add(role)

        add(state.get("pid"), "sidecar-state")
        services = state.get("services") if isinstance(state.get("services"), Mapping) else {}
        for name, service in services.items():
            if isinstance(service, Mapping):
                add(service.get("pid"), f"service:{name}")
        add(self._read_pid(self.paths.sidecar_pid), "sidecar-pidfile")
        add(self._read_pid(self.paths.tray_pid), "tray-pidfile")
        alive = sorted(pid for pid in tracked if self.process_alive(pid))
        if alive:
            raise CutoverPreconditionError(
                "Tracked Work Buddy process generations are still running.",
                details={"alive_pids": alive},
            )
        processes = [dict(item) for item in self.process_lister()]
        obsidian = sorted(
            {
                int(item["pid"])
                for item in processes
                if str(item.get("name") or "").casefold() in {"obsidian", "obsidian.exe"}
                and int(item.get("pid") or 0) > 0
            }
        )
        if obsidian:
            raise CutoverPreconditionError(
                "Obsidian is still running.", details={"obsidian_pids": obsidian}
            )
        repo_markers = {
            os.path.normcase(str(paths.repo_root())).replace("\\", "/").casefold(),
            os.path.normcase(str(self.task_db_path.parent)).replace("\\", "/").casefold(),
        }
        dedicated_names = {
            "wbuddy",
            "wbuddy.exe",
            "work-buddy",
            "work-buddy.exe",
            "wb-tray",
            "wb-tray.exe",
        }
        generic_names = {
            "python",
            "python.exe",
            "pythonw",
            "pythonw.exe",
            "node",
            "node.exe",
            "uv",
            "uv.exe",
        }
        untracked: list[dict[str, Any]] = []
        by_pid = {int(item.get("pid") or 0): item for item in processes}
        operator_ancestry = {os.getpid()}
        ancestor = os.getppid()
        while ancestor > 0 and ancestor not in operator_ancestry:
            operator_ancestry.add(ancestor)
            parent = by_pid.get(ancestor)
            if parent is None:
                break
            ancestor = int(parent.get("parent_pid") or 0)
        ancestry_records = []
        for pid in sorted(operator_ancestry):
            item = by_pid.get(pid)
            if item is None:
                ancestry_records.append(
                    {"pid": pid, "parent_pid": None, "name": "unobserved"}
                )
                continue
            command = str(item.get("command_line") or "")
            executable = str(item.get("executable_path") or "")
            ancestry_records.append(
                {
                    "pid": pid,
                    "parent_pid": int(item.get("parent_pid") or 0),
                    "name": str(item.get("name") or "").casefold(),
                    "command_line_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                    "executable_path_sha256": hashlib.sha256(
                        executable.encode("utf-8")
                    ).hexdigest(),
                }
            )
        for item in processes:
            pid = int(item.get("pid") or 0)
            if pid <= 0 or pid in operator_ancestry:
                continue
            name = str(item.get("name") or "").casefold()
            command = str(item.get("command_line") or "")
            executable = str(item.get("executable_path") or "")
            searchable = os.path.normcase(f"{command} {executable}").replace("\\", "/").casefold()
            work_buddy_marker = any(marker and marker in searchable for marker in repo_markers) or any(
                token in searchable
                for token in (
                    "work_buddy",
                    "work-buddy",
                    "wbuddy",
                    "sidecar_jobs",
                    "task-sync",
                )
            )
            if name in dedicated_names or (name in generic_names and work_buddy_marker):
                untracked.append(
                    {
                        "pid": pid,
                        "name": name,
                        "command_line_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                        "executable_path_sha256": hashlib.sha256(
                            executable.encode("utf-8")
                        ).hexdigest(),
                    }
                )
        if untracked:
            raise CutoverPreconditionError(
                "Untracked Work Buddy-capable Python/Node processes are still running.",
                details={"processes": untracked},
            )
        with self._connect_ro() as conn:
            system = conn.execute(
                "SELECT process_generation FROM task_system_state WHERE id=1"
            ).fetchone()
            if system is None:
                raise CutoverPreconditionError("Task system state is missing.")
            generation = int(system[0])
        tracked_rows = [
            {"pid": pid, "roles": sorted(roles)} for pid, roles in sorted(tracked.items())
        ]
        return {
            "schema": "wb.native-task-process-stop-only/v1",
            "process_generation": generation,
            "sidecar_state_sha256": _sha256_file(state_path),
            "tracked_processes": tracked_rows,
            "obsidian_pids": [],
            "untracked_work_buddy_processes": [],
            "untracked_process_scan_sha256": canonical_sha256([]),
            "operator_ancestor_chain": ancestry_records,
            "operator_ancestor_chain_sha256": canonical_sha256(ancestry_records),
        }

    def _process_evidence(self) -> Mapping[str, Any]:
        stopped = self._stopped_process_evidence()
        producer_jobs = self._producer_jobs()
        enabled = [row for row in producer_jobs if row["enabled"]]
        if enabled:
            raise CutoverPreconditionError(
                "Legacy task producers are still enabled.",
                details={"enabled_jobs": enabled},
            )
        retries = self._pending_legacy_retries()
        if retries:
            raise CutoverPreconditionError(
                "Queued legacy task mutations can still replay.",
                details={"operations": retries},
            )
        return {
            **stopped,
            "schema": "wb.native-task-live-stop-verification/v1",
            "producer_jobs": producer_jobs,
            "producer_jobs_sha256": canonical_sha256(producer_jobs),
            "pending_legacy_task_retries": [],
            "retry_queue_sha256": canonical_sha256([]),
        }

    def capture_process_stop_receipt(self) -> Mapping[str, Any]:
        target = self.paths.process_stop_receipt
        target.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(target, timeout=10.0):
            connection = sqlite3.connect(self.task_db_path, timeout=10.0)
            connection.row_factory = sqlite3.Row
            try:
                # Serialize against arm_mutation_fence's SQLite transaction so
                # a capture cannot observe "unbound", overwrite the fixed
                # receipt, and race a fence commit that names the old digest.
                connection.execute("BEGIN IMMEDIATE")
                cohort = self._cohort(connection)
                state = str(cohort.get("state") or "")
                bound_fence = str(cohort.get("fence_receipt_id") or "")
                if bound_fence:
                    verified = self._stop_receipt_evidence()
                    payload_sha256 = str(verified["stop_payload_sha256"])
                    derived_fence = "fence_" + payload_sha256[:32]
                    if derived_fence != bound_fence:
                        raise CutoverPreconditionError(
                            "The bound process-stop receipt no longer matches the mutation fence."
                        )
                    connection.commit()
                    return {
                        "receipt": str(target),
                        "payload_sha256": payload_sha256,
                        "replayed": True,
                        **verified,
                    }
                if state != "shadow":
                    raise CutoverPreconditionError(
                        "A non-shadow cutover cohort has no bound process-stop fence; "
                        "receipt replacement is forbidden."
                    )

                evidence = self._process_evidence()
                payload = {
                    "schema": STOP_RECEIPT_SCHEMA,
                    "cohort_id": self.inventory.cohort_id,
                    "captured_at": _iso(self.clock()),
                    "evidence": dict(evidence),
                }
                payload["payload_sha256"] = canonical_sha256(payload)
                encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
                    "utf-8"
                )
                temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    try:
                        os.remove(temporary)
                    except FileNotFoundError:
                        pass
                connection.commit()
                return {
                    "receipt": str(target),
                    "payload_sha256": payload["payload_sha256"],
                    "replayed": False,
                    **evidence,
                }
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _stop_receipt_evidence(self) -> Mapping[str, Any]:
        receipt = _json_value(self.paths.process_stop_receipt, expected=dict)
        if receipt.get("schema") != STOP_RECEIPT_SCHEMA:
            raise CutoverPreconditionError("The process-stop receipt schema is unsupported.")
        expected_hash = str(receipt.pop("payload_sha256", ""))
        if not _SHA256.fullmatch(expected_hash) or canonical_sha256(receipt) != expected_hash:
            raise CutoverPreconditionError("The process-stop receipt was modified.")
        if str(receipt.get("cohort_id") or "") != self.inventory.cohort_id:
            raise CutoverPreconditionError("The process-stop receipt belongs to another cohort.")
        captured = _parse_time(receipt.get("captured_at"))
        age = self.clock().astimezone(timezone.utc) - captured
        if age < timedelta(minutes=-5):
            raise CutoverPreconditionError("The process-stop receipt is from the future.")
        # The receipt is a hash-bound identity for the fenced process
        # generation, not a lease. Every use below re-probes all PIDs,
        # Obsidian, producer jobs, and relevant queue records. Expiring it by
        # wall time made a long binding pass impossible to resume after the
        # fence had been tied to its payload without adding any safety.
        current = self._process_evidence()
        stored = receipt.get("evidence")
        if not isinstance(stored, Mapping):
            raise CutoverPreconditionError("The process-stop evidence is malformed.")
        stored_generation = int(stored.get("process_generation", -1))
        observed_generation = int(current.get("process_generation", -1))
        invocation_fields = {
            "process_generation",
            "operator_ancestor_chain",
            "operator_ancestor_chain_sha256",
        }
        stored_stable = {
            key: value for key, value in stored.items() if key not in invocation_fields
        }
        current_stable = {
            key: value for key, value in current.items() if key not in invocation_fields
        }
        if canonical_sha256(stored_stable) != canonical_sha256(current_stable):
            raise CutoverPreconditionError("Process, job, or retry state changed after the stop receipt.")
        generation_advanced_by_activation = False
        generation_advanced_by_rollback = False
        if observed_generation != stored_generation:
            with self._connect_ro() as conn:
                cohort = self._cohort(conn)
                system = conn.execute(
                    "SELECT authority_epoch, process_generation FROM task_system_state WHERE id=1"
                ).fetchone()
            generation_advanced_by_activation = bool(
                system is not None
                and str(cohort["state"]) == "active"
                and cohort.get("expected_process_generation") is not None
                and int(cohort["expected_process_generation"]) == stored_generation
                and int(system["process_generation"]) == stored_generation + 1
                and str(system["authority_epoch"]) == str(cohort["target_authority_epoch"])
            )
            generation_advanced_by_rollback = bool(
                system is not None
                and str(cohort["state"]) == "rolled_back"
                and cohort.get("expected_process_generation") is not None
                and int(cohort["expected_process_generation"]) == stored_generation
                and int(system["process_generation"]) == stored_generation + 1
                and str(cohort.get("rollback_authority_epoch") or "")
                and str(system["authority_epoch"])
                == str(cohort["rollback_authority_epoch"])
            )
            if not (
                generation_advanced_by_activation or generation_advanced_by_rollback
            ):
                raise CutoverPreconditionError(
                    "The Task process generation changed after the stop receipt."
                )
        return {
            **current,
            "process_generation": stored_generation,
            "observed_process_generation": observed_generation,
            "generation_advanced_by_activation": generation_advanced_by_activation,
            "generation_advanced_by_rollback": generation_advanced_by_rollback,
            "stop_receipt_sha256": _sha256_file(self.paths.process_stop_receipt),
            "stop_payload_sha256": expected_hash,
            "captured_at": _iso(captured),
            "continuously_revalidated": True,
        }

    @staticmethod
    def _probe_windows_acl(root: Path) -> Mapping[str, Any]:
        if sys.platform != "win32":
            raise CutoverPreconditionError("Production frozen-tree sealing requires Windows NTFS.")
        # Required blocked rights: create/write/append, extended attributes,
        # delete children, write attributes, delete/rename, DACL changes, and
        # ownership takeover. SYSTEM/Administrators can retain recovery access;
        # the interactive token must be explicitly denied every one of these.
        blocked_rights = 2 | 4 | 16 | 64 | 256 | 65536 | 262144 | 524288
        parent_blocked_rights = 64 | 262144 | 524288
        owner_blocked_rights = 262144 | 524288
        script = r"""
$ErrorActionPreference = 'Stop'
$rootPath = [Environment]::GetEnvironmentVariable('WB_CUTOVER_ACL_ROOT')
$blocked = [int64][Environment]::GetEnvironmentVariable('WB_CUTOVER_ACL_MASK')
$parentBlocked = [int64][Environment]::GetEnvironmentVariable('WB_CUTOVER_PARENT_ACL_MASK')
$ownerBlocked = [int64][Environment]::GetEnvironmentVariable('WB_CUTOVER_OWNER_ACL_MASK')
$root = Get-Item -LiteralPath $rootPath -Force
$rootFull = [IO.Path]::GetFullPath($root.FullName).TrimEnd('\')
$rootPrefix = $rootFull + '\'
$frozenParent = Get-Item -LiteralPath $root.Parent.FullName -Force
$frozenParentFull = [IO.Path]::GetFullPath($frozenParent.FullName).TrimEnd('\')
if ($frozenParent.Name -ine '_frozen' -or
    ([int]$frozenParent.Attributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0) {
  throw 'The dedicated wrapper parent must be a real _frozen directory.'
}
$volume = New-Object System.IO.DriveInfo -ArgumentList $root.PSDrive.Root
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentSid = $identity.User.Value
$ownerRightsSid = 'S-1-3-4'

function Get-ExplicitDenyMasks {
  param([Security.AccessControl.FileSystemSecurity]$Acl)
  $userDenied = [int64]0
  $ownerRightsDenied = [int64]0
  $rules = $Acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
  foreach ($rule in $rules) {
    $inheritOnly = ([int]$rule.PropagationFlags -band [int][Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0
    if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Deny -or
        $inheritOnly -or $rule.IsInherited) {
      continue
    }
    if ($rule.IdentityReference.Value -eq $currentSid) {
      $userDenied = $userDenied -bor [int64]$rule.FileSystemRights
    }
    if ($rule.IdentityReference.Value -eq $ownerRightsSid) {
      $ownerRightsDenied = $ownerRightsDenied -bor [int64]$rule.FileSystemRights
    }
  }
  return @{ user_denied=$userDenied; owner_rights_denied=$ownerRightsDenied }
}

$items = @($root) + @(Get-ChildItem -LiteralPath $rootPath -Force -Recurse)
$issues = @()
$records = @()
$rootAcl = Get-Acl -LiteralPath $rootPath
foreach ($item in $items) {
  $acl = Get-Acl -LiteralPath $item.FullName
  $masks = Get-ExplicitDenyMasks $acl
  $missing = $blocked -band (-bnot [int64]$masks.user_denied)
  $ownerFenceMissing = $ownerBlocked -band (-bnot [int64]$masks.owner_rights_denied)
  $itemOwnerSid = (New-Object Security.Principal.NTAccount($acl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value
  $itemFull = [IO.Path]::GetFullPath($item.FullName)
  if ([String]::Equals($itemFull, $rootFull, [StringComparison]::OrdinalIgnoreCase)) {
    $relative = '.'
  } elseif ($itemFull.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    $relative = $itemFull.Substring($rootPrefix.Length).Replace('\','/')
  } else {
    throw "ACL entry escaped the frozen root: $itemFull"
  }
  if ($missing -ne 0 -or $ownerFenceMissing -ne 0 -or -not $acl.AreAccessRulesCanonical) {
    $issues += @{
      path=$relative
      missing_rights=[int64]$missing
      owner_sid=$itemOwnerSid
      owner_rights_missing=[int64]$ownerFenceMissing
      acl_canonical=[bool]$acl.AreAccessRulesCanonical
    }
  }
  $records += "$relative`0$($acl.Sddl)"
}
$parentAcl = Get-Acl -LiteralPath $frozenParentFull
$parentMasks = Get-ExplicitDenyMasks $parentAcl
$parentMissing = $parentBlocked -band (-bnot [int64]$parentMasks.user_denied)
$parentOwnerMissing = $ownerBlocked -band (-bnot [int64]$parentMasks.owner_rights_denied)
$parentOwnerSid = (New-Object Security.Principal.NTAccount($parentAcl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value
$parentIssues = @()
if ($parentMissing -ne 0 -or $parentOwnerMissing -ne 0 -or -not $parentAcl.AreAccessRulesCanonical) {
  $parentIssues += @{
    path=$frozenParentFull
    missing_rights=[int64]$parentMissing
    owner_sid=$parentOwnerSid
    owner_rights_missing=[int64]$parentOwnerMissing
    acl_canonical=[bool]$parentAcl.AreAccessRulesCanonical
  }
}
$ownerSid = (New-Object Security.Principal.NTAccount($rootAcl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value
@{
  filesystem_type = $volume.DriveFormat
  current_sid = $currentSid
  owner_sid = $ownerSid
  root_acl_protected = $rootAcl.AreAccessRulesProtected
  root_sddl = $rootAcl.Sddl
  entry_count = $items.Count
  issues = $issues
  acl_records = $records
  frozen_parent = $frozenParentFull
  frozen_parent_owner_sid = $parentOwnerSid
  frozen_parent_acl_protected = $parentAcl.AreAccessRulesProtected
  frozen_parent_acl_canonical = $parentAcl.AreAccessRulesCanonical
  frozen_parent_sddl = $parentAcl.Sddl
  frozen_parent_current_user_denied_mask = [int64]$parentMasks.user_denied
  frozen_parent_owner_rights_denied_mask = [int64]$parentMasks.owner_rights_denied
  parent_issues = $parentIssues
} | ConvertTo-Json -Compress -Depth 6
"""
        environment = os.environ.copy()
        environment["WB_CUTOVER_ACL_ROOT"] = str(root)
        environment["WB_CUTOVER_ACL_MASK"] = str(blocked_rights)
        environment["WB_CUTOVER_PARENT_ACL_MASK"] = str(parent_blocked_rights)
        environment["WB_CUTOVER_OWNER_ACL_MASK"] = str(owner_blocked_rights)
        # ``uv run`` can inherit a PowerShell 7 module directory before the
        # Windows PowerShell 5.1 modules.  powershell.exe then discovers the
        # incompatible PS7 Security manifest first and even Get-Acl fails to
        # autoload.  Bind this production probe to the system WinPS modules.
        system_root = Path(environment.get("SystemRoot") or environment.get("WINDIR") or r"C:\Windows")
        winps_modules = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"
        inherited_modules = environment.get("PSModulePath", "")
        environment["PSModulePath"] = os.pathsep.join(
            item for item in (str(winps_modules), inherited_modules) if item
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
            )
            value = json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise CutoverPreconditionError("Could not verify the frozen-tree NTFS ACL.") from exc
        if str(value.get("filesystem_type") or "").casefold() != "ntfs":
            raise CutoverPreconditionError("The frozen task tree is not on NTFS.")
        if not bool(value.get("root_acl_protected")):
            raise CutoverPreconditionError("The frozen root DACL still inherits mutable parent rules.")
        issues = value.get("issues") or []
        parent_issues = value.get("parent_issues") or []
        if issues or parent_issues:
            raise CutoverPreconditionError(
                "The NTFS deny fence is incomplete.",
                details={
                    "acl_issues": issues[:50],
                    "frozen_parent_acl_issues": parent_issues[:10],
                },
            )
        expected_parent = root.expanduser().resolve(strict=False).parent
        observed_parent = Path(str(value.get("frozen_parent") or "")).expanduser().resolve(
            strict=False
        )
        if os.path.normcase(str(observed_parent)) != os.path.normcase(str(expected_parent)):
            raise CutoverPreconditionError("The ACL evidence names another _frozen parent.")
        parent_sddl = str(value.get("frozen_parent_sddl") or "")
        if not parent_sddl:
            raise CutoverPreconditionError("The _frozen parent SDDL evidence is missing.")
        if (
            int(value.get("frozen_parent_current_user_denied_mask") or 0)
            & parent_blocked_rights
            != parent_blocked_rights
            or int(value.get("frozen_parent_owner_rights_denied_mask") or 0)
            & owner_blocked_rights
            != owner_blocked_rights
        ):
            raise CutoverPreconditionError("The _frozen parent deny masks are incomplete.")
        records = sorted(str(item) for item in value.pop("acl_records", []))
        value["acl_tree_sha256"] = canonical_sha256(records)
        value["frozen_parent_sddl_sha256"] = hashlib.sha256(
            parent_sddl.encode("utf-8")
        ).hexdigest()
        value["acl_scope_sha256"] = canonical_sha256(
            {
                "wrapper_and_descendants": records,
                "frozen_parent": str(observed_parent),
                "frozen_parent_sddl": parent_sddl,
            }
        )
        value["blocked_rights_mask"] = blocked_rights
        value["frozen_parent_blocked_rights_mask"] = parent_blocked_rights
        value["owner_rights_blocked_mask"] = owner_blocked_rights
        return value

    def _frozen_evidence(self) -> Mapping[str, Any]:
        tree = self._tree_evidence()
        acl = dict(self.acl_probe(Path(str(tree["acl_wrapper"]))))
        if not acl.get("verified", True):
            raise CutoverPreconditionError("Frozen-tree ACL verifier did not pass.")
        return {
            "schema": "wb.native-task-frozen-seal/v1",
            "tree": dict(tree),
            "acl": acl,
            "acl_scope": "dedicated_wrapper_and_descendants",
            "retention_policy": "until_explicit_user_approval",
        }

    def _restore_gate_evidence(self) -> Mapping[str, Any]:
        backup = self._backup_evidence()
        restore = self._restore_evidence(backup)
        return {
            "schema": "wb.native-task-backup-restore-gate/v1",
            "backup": backup,
            "restore": restore,
        }

    def _rollback_stage_snapshot(self) -> Mapping[str, Any]:
        """Return state that is invariant across the final activation transaction."""

        stage_tables = (
            "task_migration_inventory",
            "task_migration_idless_stage",
            "task_migration_existing_task_stage",
            "task_migration_document_stage",
            "task_migration_local_link_stage",
            "task_migration_binding_transitions",
        )
        with self._connect_ro() as conn:
            cohort = self._cohort(conn)
            system = conn.execute(
                "SELECT process_generation FROM task_system_state WHERE id=1"
            ).fetchone()
            documents_stage = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM task_migration_document_stage WHERE cohort_id=? "
                    "ORDER BY note_uuid",
                    (self.inventory.cohort_id,),
                )
            ]
            link_stage = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM task_migration_local_link_stage WHERE cohort_id=? "
                    "ORDER BY link_id",
                    (self.inventory.cohort_id,),
                )
            ]
            idless_ids = {
                str(row[0])
                for row in conn.execute(
                    "SELECT task_id FROM task_migration_idless_stage WHERE cohort_id=?",
                    (self.inventory.cohort_id,),
                )
            }
            task_ids = {
                str(row[0]) for row in conn.execute("SELECT task_id FROM task_metadata")
            } | idless_ids
            root_ids = sorted({str(row["root_id"]) for row in link_stage})
            roots = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM task_local_file_roots WHERE root_id IN ("
                    + ",".join("?" for _ in root_ids)
                    + ") ORDER BY root_id",
                    root_ids,
                )
            ] if root_ids else []
            table_digests: dict[str, str] = {}
            for table in stage_tables:
                ignored_columns = ("activated_at",)
                if table == "task_migration_binding_transitions":
                    # The isolated clone performs the same semantic transition
                    # at another wall-clock instant. Keep authority, epochs,
                    # revision, and result exact while excluding only time.
                    ignored_columns += ("applied_at",)
                table_digests[table] = self._table_digest(
                    conn,
                    table,
                    self.inventory.cohort_id,
                    ignored_columns=ignored_columns,
                )
        if system is None:
            raise CutoverPreconditionError("Task system state is missing.")
        normalized_documents = [
            {
                "note_uuid": str(row["note_uuid"]),
                "store_id": str(row["store_id"]),
                "document_id": str(row["document_id"]),
                "task_id": row["task_id"],
                "binding_id": row["binding_id"],
                "lifecycle": str(row["lifecycle"]),
                "structured_head_sha256": str(row["document_head_sha256"]),
            }
            for row in documents_stage
        ]
        root_identity_fields = (
            "root_id", "label", "manifest_sha256", "policy_revision", "status",
        )
        normalized_roots = [
            {
                **{field: row[field] for field in root_identity_fields},
                "status": "active",
            }
            for row in roots
        ]
        link_identity_fields = (
            "link_id", "task_id", "store_id", "document_id", "root_id",
            "relative_path", "display_name", "suffix", "media_type", "byte_length",
            "sha256", "sensitivity", "allowed_action", "policy_revision",
            "source_receipt_id",
        )
        normalized_links = [
            {field: row[field] for field in link_identity_fields} for row in link_stage
        ]
        snapshot = {
            "schema": "wb.native-task-rollback-stage/v1",
            "cohort_id": self.inventory.cohort_id,
            "inventory_sha256": self.inventory.inventory_sha256,
            "manifest_sha256": self.inventory.manifest_sha256,
            "source_root_fingerprint": self.inventory.source_root_fingerprint,
            "target_authority_epoch": str(cohort["target_authority_epoch"]),
            "target_process_generation": int(cohort["expected_process_generation"]) + 1,
            "cowork_task_store_id": str(cohort["cowork_task_store_id"]),
            "task_ids": sorted(task_ids),
            "documents": normalized_documents,
            "local_file_roots": normalized_roots,
            "local_file_links": normalized_links,
            "table_digests": table_digests,
        }
        return {**snapshot, "stage_sha256": canonical_sha256(snapshot)}

    @staticmethod
    def _verify_v11_rollback_database(path: Path) -> Mapping[str, Any]:
        _regular_file(path, label="staged rollback v11 database")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 11:
                raise CutoverPreconditionError("The rollback database is not schema v11.")
            actual_columns = {
                table: tuple(
                    str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
                )
                for table in _LEGACY_V11_COLUMNS
            }
            if actual_columns != _LEGACY_V11_COLUMNS:
                raise CutoverPreconditionError("The rollback database schema is not exact v11.")
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            forbidden = {
                "task_system_state", "task_collection_state", "task_mutation_receipts",
                "task_event_outbox", "task_document_links",
            }
            if present & forbidden:
                raise CutoverPreconditionError("Native-only tables leaked into rollback v11.")
            if [str(row[0]) for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
                raise CutoverPreconditionError("The rollback v11 database failed integrity_check.")
            if list(connection.execute("PRAGMA foreign_key_check")):
                raise CutoverPreconditionError("The rollback v11 database has foreign-key errors.")
            counts = {
                "task_rows": int(connection.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0]),
                "live_task_rows": int(connection.execute(
                    "SELECT COUNT(*) FROM task_metadata WHERE deleted_at IS NULL"
                ).fetchone()[0]),
                "tombstone_rows": int(connection.execute(
                    "SELECT COUNT(*) FROM task_metadata WHERE deleted_at IS NOT NULL"
                ).fetchone()[0]),
                "archived_rows": int(connection.execute(
                    "SELECT COUNT(*) FROM task_metadata WHERE archived_at IS NOT NULL"
                ).fetchone()[0]),
                "tag_rows": int(connection.execute("SELECT COUNT(*) FROM task_tags").fetchone()[0]),
                "history_rows": int(connection.execute(
                    "SELECT COUNT(*) FROM task_state_history"
                ).fetchone()[0]),
                "session_rows": int(connection.execute(
                    "SELECT COUNT(*) FROM task_sessions"
                ).fetchone()[0]),
                "action_item_rows": int(connection.execute(
                    "SELECT COUNT(*) FROM task_action_items"
                ).fetchone()[0]),
                "lww_rows": int(connection.execute("SELECT COUNT(*) FROM lww_meta").fetchone()[0]),
            }
            semantic = {
                "tasks": [dict(row) for row in connection.execute(
                    "SELECT * FROM task_metadata ORDER BY task_id"
                )],
                "tags": [dict(row) for row in connection.execute(
                    "SELECT * FROM task_tags ORDER BY task_id, tag"
                )],
                "history": [dict(row) for row in connection.execute(
                    "SELECT * FROM task_state_history ORDER BY id"
                )],
                "sessions": [dict(row) for row in connection.execute(
                    "SELECT * FROM task_sessions ORDER BY id"
                )],
                "actions": [dict(row) for row in connection.execute(
                    "SELECT * FROM task_action_items ORDER BY id"
                )],
            }
            task_ids = sorted(str(row["task_id"]) for row in semantic["tasks"])
        finally:
            connection.close()
        return {
            "schema_version": 11,
            "counts": counts,
            "semantic_sha256": canonical_sha256(semantic),
            "task_ids": task_ids,
        }

    def _rollback_rehearsal_evidence(self) -> Mapping[str, Any]:
        companion = _json_value(self.paths.rollback_rehearsal, expected=dict)
        supplied_hash = str(companion.pop("payload_sha256", ""))
        if (
            companion.get("schema") != ROLLBACK_REHEARSAL_SCHEMA
            or not _SHA256.fullmatch(supplied_hash)
            or canonical_sha256(companion) != supplied_hash
        ):
            raise CutoverPreconditionError("The rollback rehearsal receipt is invalid.")
        completed = _parse_time(companion.get("completed_at"))
        age = self.clock().astimezone(timezone.utc) - completed
        if age < timedelta(minutes=-5) or age > self.backup_freshness:
            raise CutoverPreconditionError("The rollback rehearsal is not fresh enough.")
        stage = self._rollback_stage_snapshot()
        backup = self._backup_evidence()
        backup_snapshot = backup.get("artifacts", {}).get("work_buddy_data_snapshot")
        if not isinstance(backup_snapshot, Mapping):
            raise CutoverPreconditionError("The rollback rehearsal has no fresh backup binding.")
        expected_companion = {
            "cohort_id": self.inventory.cohort_id,
            "inventory_sha256": self.inventory.inventory_sha256,
            "manifest_sha256": self.inventory.manifest_sha256,
            "production_stage_sha256": stage["stage_sha256"],
            "backup_snapshot_sha256": str(backup_snapshot["sha256"]),
            "restore_receipt_sha256": _sha256_file(self.paths.restore_rehearsal),
        }
        for key, expected in expected_companion.items():
            if str(companion.get(key) or "") != str(expected):
                raise CutoverPreconditionError(
                    f"The rollback rehearsal is stale for production field: {key}"
                )
        compact = companion.get("export_evidence")
        if not isinstance(compact, Mapping) or compact.get("schema") != (
            "wb.task-rollback-rehearsal-evidence/v1"
        ):
            raise CutoverPreconditionError("Rollback exporter rehearsal evidence is missing.")
        receipt_path = _regular_file(
            Path(str(compact.get("receipt_file") or "")), label="rollback export receipt"
        )
        if (
            receipt_path.name != "rollback-export-receipt.json"
            or int(compact.get("receipt_byte_length", -1)) != receipt_path.stat().st_size
            or str(compact.get("receipt_sha256") or "") != _sha256_file(receipt_path)
        ):
            raise CutoverPreconditionError("The rollback export receipt file changed.")
        receipt = _json_value(receipt_path, expected=dict)
        if receipt.get("schema") != "wb.task-rollback-export/v1":
            raise CutoverPreconditionError("The rollback export receipt schema is unsupported.")
        claimed_id = str(receipt.get("receipt_id") or "")
        unsigned = dict(receipt)
        unsigned.pop("receipt_id", None)
        if claimed_id != "trr_" + canonical_sha256(unsigned)[:32]:
            raise CutoverPreconditionError("The rollback export receipt identity is invalid.")
        if str(compact.get("receipt_id") or "") != claimed_id:
            raise CutoverPreconditionError("Rollback compact evidence names another receipt.")
        receipt_created = _parse_time(receipt.get("created_at"))
        receipt_age = self.clock().astimezone(timezone.utc) - receipt_created
        if receipt_age < timedelta(minutes=-5) or receipt_age > self.backup_freshness:
            raise CutoverPreconditionError("The rollback export itself is not fresh enough.")
        receipt_identity = {
            "cohort_id": stage["cohort_id"],
            "source_inventory_sha256": stage["inventory_sha256"],
            "source_manifest_sha256": stage["manifest_sha256"],
            "source_root_fingerprint": stage["source_root_fingerprint"],
            "source_cowork_task_store_id": stage["cowork_task_store_id"],
            "source_authority_epoch": stage["target_authority_epoch"],
            "source_process_generation": stage["target_process_generation"],
        }
        for key, expected in receipt_identity.items():
            if str(receipt.get(key) or "") != str(expected):
                raise CutoverPreconditionError(
                    f"The rollback clone receipt differs from the live cohort: {key}"
                )
        compact_keys = (
            "cohort_id", "source_inventory_sha256", "source_manifest_sha256",
            "source_cowork_task_store_id", "source_snapshot_sha256",
            "source_database_snapshot_sha256", "document_heads_sha256",
            "source_local_file_catalog_sha256", "staged_tree_sha256",
            "legacy_database_sha256", "legacy_database_semantic_sha256", "counts",
        )
        if any(compact.get(key) != receipt.get(key) for key in compact_keys):
            raise CutoverPreconditionError("Rollback compact evidence differs from its full receipt.")

        source_documents = list(receipt.get("source_documents") or [])
        normalized_source_documents = [
            {
                "note_uuid": str(row.get("note_uuid") or ""),
                "store_id": str(row.get("store_id") or ""),
                "document_id": str(row.get("document_id") or ""),
                "task_id": row.get("task_id"),
                "binding_id": row.get("binding_id"),
                "lifecycle": str(row.get("lifecycle") or ""),
                "structured_head_sha256": str(row.get("structured_head_sha256") or ""),
            }
            for row in source_documents
        ]
        if normalized_source_documents != stage["documents"] or any(
            not _SHA256.fullmatch(str(row.get("ydoc_snapshot_sha256") or ""))
            or not _SHA256.fullmatch(str(row.get("projection_sha256") or ""))
            or int(row.get("projection_byte_length", -1)) < 0
            for row in source_documents
        ):
            raise CutoverPreconditionError("Rollback documents differ from the staged cohort.")
        if str(receipt.get("document_heads_sha256") or "") != canonical_sha256(
            source_documents
        ):
            raise CutoverPreconditionError("Rollback document-head receipt is invalid.")

        catalog = receipt.get("source_local_file_catalog")
        if not isinstance(catalog, Mapping):
            raise CutoverPreconditionError("Rollback local-file catalog is missing.")
        local_link_fields = (
            "link_id", "task_id", "store_id", "document_id", "root_id",
            "relative_path", "display_name", "suffix", "media_type", "byte_length",
            "sha256", "sensitivity", "allowed_action", "policy_revision",
            "source_receipt_id",
        )
        catalog_links = sorted(
            (
                {key: row.get(key) for key in local_link_fields}
                for row in list(catalog.get("links") or [])
            ),
            key=lambda row: str(row["link_id"]),
        )
        catalog_roots = sorted(
            (
                {
                    key: ("active" if key == "status" else row.get(key))
                    for key in (
                        "root_id", "label", "manifest_sha256", "policy_revision", "status",
                    )
                }
                for row in list(catalog.get("roots") or [])
            ),
            key=lambda row: str(row["root_id"]),
        )
        if (
            catalog_links != stage["local_file_links"]
            or catalog_roots != stage["local_file_roots"]
            or str(receipt.get("source_local_file_catalog_sha256") or "")
            != canonical_sha256(catalog)
        ):
            raise CutoverPreconditionError("Rollback local-file catalog differs from staging.")
        expected_assets = [
            {
                "link_id": row["link_id"],
                "relative_path": row["relative_path"],
                "byte_length": row["byte_length"],
                "sha256": row["sha256"],
            }
            for row in stage["local_file_links"]
        ]
        actual_assets = sorted(
            list(catalog.get("verified_assets") or []),
            key=lambda row: str(row.get("link_id") or ""),
        )
        if actual_assets != expected_assets:
            raise CutoverPreconditionError("Rollback verified assets differ from staging.")

        staging_root = receipt_path.parent
        artifact_names = {
            "tree_manifest": "legacy-tree-manifest.json",
            "exception_report": "rollback-exceptions.json",
            "native_supplement": "native-supplement.json",
        }
        for prefix, expected_name in artifact_names.items():
            if str(receipt.get(f"{prefix}_file") or "") != expected_name:
                raise CutoverPreconditionError("Rollback artifact path contract changed.")
            artifact = _regular_file(staging_root / expected_name, label=expected_name)
            if (
                int(receipt.get(f"{prefix}_byte_length", -1)) != artifact.stat().st_size
                or str(receipt.get(f"{prefix}_sha256") or "") != _sha256_file(artifact)
            ):
                raise CutoverPreconditionError(f"Rollback artifact changed: {expected_name}")
        tree = staging_root / "legacy-tree"
        if not tree.is_dir() or tree.is_symlink():
            raise CutoverPreconditionError("The staged rollback tree is missing or linked.")
        tree_files: list[dict[str, Any]] = []
        for path in sorted(tree.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if self._is_reparse(path):
                raise CutoverPreconditionError("The staged rollback tree contains a link.")
            if path.is_file():
                tree_files.append(
                    {
                        "relative_path": path.relative_to(tree).as_posix(),
                        "byte_length": path.stat().st_size,
                        "sha256": _sha256_file(path),
                    }
                )
        actual_manifest = {
            "schema": "wb.task-rollback-tree-manifest/v1",
            "files": tree_files,
            "tree_sha256": canonical_sha256(tree_files),
        }
        stored_manifest = _json_value(staging_root / "legacy-tree-manifest.json", expected=dict)
        if actual_manifest != stored_manifest or receipt.get("staged_tree_sha256") != (
            actual_manifest["tree_sha256"]
        ):
            raise CutoverPreconditionError("The staged rollback tree changed after rehearsal.")
        note_paths = sorted(
            str(row["relative_path"]).casefold()
            for row in tree_files
            if Path(row["relative_path"]).suffix.casefold() == ".md"
            and _NOTE_UUID.fullmatch(Path(row["relative_path"]).stem)
        )
        expected_note_paths = sorted(
            f"notes/{row['note_uuid']}.md".casefold() for row in stage["documents"]
        )
        if note_paths != expected_note_paths:
            raise CutoverPreconditionError(
                "Rollback note files do not exactly cover notes/<uuid>.md for the document cohort."
            )
        for asset in expected_assets:
            staged_asset = _regular_file(
                tree / str(asset["relative_path"]), label="staged rollback local asset"
            )
            if (
                staged_asset.stat().st_size != int(asset["byte_length"])
                or _sha256_file(staged_asset) != str(asset["sha256"])
            ):
                raise CutoverPreconditionError("A staged rollback local asset changed.")

        database_name = str(receipt.get("legacy_database_file") or "")
        if database_name != "task_metadata.v11.db":
            raise CutoverPreconditionError("Rollback v11 database path contract changed.")
        database_path = _regular_file(staging_root / database_name, label=database_name)
        if (
            int(receipt.get("legacy_database_schema_version", -1)) != 11
            or int(receipt.get("legacy_database_byte_length", -1)) != database_path.stat().st_size
            or str(receipt.get("legacy_database_sha256") or "") != _sha256_file(database_path)
        ):
            raise CutoverPreconditionError("The staged rollback v11 database changed.")
        database = self._verify_v11_rollback_database(database_path)
        counts = dict(receipt.get("counts") or {})
        if (
            database["task_ids"] != stage["task_ids"]
            or any(counts.get(key) != value for key, value in database["counts"].items())
            or str(receipt.get("legacy_database_semantic_sha256") or "")
            != database["semantic_sha256"]
        ):
            raise CutoverPreconditionError("Rollback v11 rows/counts differ from the cohort.")
        master_lines = sum(
            1
            for line in (tree / "master-task-list.md").read_text(encoding="utf-8").splitlines()
            if re.match(r"^-\s*\[[ xX]\]", line.strip())
        )
        archive_lines = sum(
            1
            for line in (tree / "archive.md").read_text(encoding="utf-8").splitlines()
            if re.match(r"^-\s*\[[ xX]\]", line.strip())
        )
        observed_tree_counts = {
            "master_lines": master_lines,
            "archive_lines": archive_lines,
            "note_files": len(note_paths),
            "tree_files": len(tree_files),
        }
        if any(counts.get(key) != value for key, value in observed_tree_counts.items()):
            raise CutoverPreconditionError("Rollback tree counts differ from the receipt.")
        if counts.get("live_task_rows") != master_lines + archive_lines:
            raise CutoverPreconditionError("Rollback Markdown lines do not cover live v11 tasks.")
        return {
            "schema": ROLLBACK_REHEARSAL_SCHEMA,
            "completed_at": _iso(completed),
            "production_stage_sha256": stage["stage_sha256"],
            "backup_snapshot_sha256": str(backup_snapshot["sha256"]),
            "export_receipt_id": claimed_id,
            "export_receipt_sha256": _sha256_file(receipt_path),
            "source_snapshot_sha256": str(receipt["source_snapshot_sha256"]),
            "staged_tree_sha256": str(receipt["staged_tree_sha256"]),
            "legacy_database_sha256": str(receipt["legacy_database_sha256"]),
            "counts_sha256": canonical_sha256(counts),
        }

    def _fence_evidence(self) -> Mapping[str, Any]:
        stop = self._stop_receipt_evidence()
        expected_receipt = "fence_" + str(stop["stop_payload_sha256"])[:32]
        with self._connect_ro() as conn:
            cohort = self._cohort(conn)
            system = conn.execute(
                "SELECT authority_epoch, rollback_fence, process_generation "
                "FROM task_system_state WHERE id=1"
            ).fetchone()
        if system is None:
            raise CutoverPreconditionError("Task system state is missing.")
        active = str(cohort["state"]) == "active"
        common_matches = bool(
            str(cohort.get("fence_receipt_id") or "") == expected_receipt
            and int(cohort.get("expected_process_generation")) == int(stop["process_generation"])
        )
        if active:
            committed = bool(
                common_matches
                and not bool(system["rollback_fence"])
                and int(system["process_generation"]) == int(stop["process_generation"]) + 1
                and str(system["authority_epoch"]) == str(cohort["target_authority_epoch"])
                and str(cohort.get("cutover_receipt_id") or "")
            )
            if not committed:
                raise CutoverPreconditionError(
                    "The committed cutover no longer matches its stopped generation."
                )
        elif (
            not common_matches
            or not bool(system["rollback_fence"])
            or int(system["process_generation"]) != int(stop["process_generation"])
        ):
            raise CutoverPreconditionError("The mutation fence does not match the fresh stop receipt.")
        return {
            "schema": "wb.native-task-mutation-fence/v1",
            "phase": "committed" if active else "armed",
            "fence_receipt_id": expected_receipt,
            "process_generation": int(system["process_generation"]),
            "authority_epoch": str(system["authority_epoch"]),
            "stop_receipt_sha256": stop["stop_receipt_sha256"],
        }

    def _binding_evidence(self) -> Mapping[str, Any]:
        with self._connect_ro() as conn:
            cohort = self._cohort(conn)
            gate = conn.execute(
                "SELECT passed, evidence_sha256, checked_at FROM task_migration_gates "
                "WHERE cohort_id=? AND gate_name='binding_cohort_verified'",
                (self.inventory.cohort_id,),
            ).fetchone()
            expected = int(
                conn.execute(
                    "SELECT COUNT(*) FROM task_migration_document_stage "
                    "WHERE cohort_id=? AND lifecycle='current' AND binding_id IS NOT NULL",
                    (self.inventory.cohort_id,),
                ).fetchone()[0]
            )
            transitions = int(
                conn.execute(
                    "SELECT COUNT(*) FROM task_migration_binding_transitions "
                    "WHERE cohort_id=? AND direction='to_cowork' AND after_authority='co_work'",
                    (self.inventory.cohort_id,),
                ).fetchone()[0]
            )
        if str(cohort["state"]) not in {"bindings_verified", "active"}:
            raise CutoverPreconditionError("The document binding cohort has not been verified.")
        if gate is None or not bool(gate["passed"]) or transitions != expected:
            raise CutoverPreconditionError("The binding transition receipt set is incomplete.")
        return {
            "schema": "wb.native-task-binding-gate/v1",
            "bindings": expected,
            "transition_receipts": transitions,
            "evidence_sha256": str(gate["evidence_sha256"]),
            "checked_at": str(gate["checked_at"]),
        }

    def checks(self) -> dict[str, GateCheck]:
        return {
            "inventory_parity": self._guard("inventory_parity", self._inventory_evidence),
            "task_parity": self._guard("task_parity", self._shadow_task_evidence),
            "document_parity": self._guard("document_parity", self._shadow_document_evidence),
            "attachment_parity": self._guard("attachment_parity", self._attachment_evidence),
            "backup_restore_rehearsal": self._guard(
                "backup_restore_rehearsal", self._restore_gate_evidence
            ),
            "rollback_rehearsal_verified": self._guard(
                "rollback_rehearsal_verified", self._rollback_rehearsal_evidence
            ),
            "legacy_mutation_fenced": self._guard(
                "legacy_mutation_fenced", self._fence_evidence
            ),
            "process_generations_stopped": self._guard(
                "process_generations_stopped", self._stop_receipt_evidence
            ),
            "frozen_tree_sealed": self._guard("frozen_tree_sealed", self._frozen_evidence),
            "binding_cohort_verified": self._guard(
                "binding_cohort_verified", self._binding_evidence
            ),
        }

    def status(self) -> Mapping[str, Any]:
        checks = self.checks()
        cohort: Mapping[str, Any]
        system: Mapping[str, Any]
        try:
            with self._connect_ro() as conn:
                cohort = self._cohort(conn)
                system_row = conn.execute("SELECT * FROM task_system_state WHERE id=1").fetchone()
                system = dict(system_row) if system_row is not None else {}
        except Exception as exc:
            cohort = {"error": str(exc)}
            system = {}
        return {
            "schema": "wb.native-task-cutover-status/v1",
            "read_only": True,
            "cohort_id": self.inventory.cohort_id,
            "cohort_state": cohort.get("state"),
            "authority_epoch": system.get("authority_epoch"),
            "rollback_fence": bool(system.get("rollback_fence", False)),
            "checks": {name: check.to_dict() for name, check in checks.items()},
            "ready_for_prepare": all(checks[name].passed for name in _PREPARE_GATES),
            "ready_for_activate": all(check.passed for check in checks.values()),
        }

    @staticmethod
    def _require(checks: Mapping[str, GateCheck], names: Iterable[str]) -> None:
        failed = {
            name: list(checks[name].problems)
            for name in names
            if name not in checks or not checks[name].passed
        }
        if failed:
            raise CutoverPreconditionError(
                "Production cutover preflight failed.", details={"failed_gates": failed}
            )

    def _require_operator(self) -> LegacyTaskCutoverOperator:
        if self.operator is None:
            raise CutoverPreconditionError("This action requires the mutating operator dependencies.")
        if self.operator.inventory.inventory_sha256 != self.inventory.inventory_sha256:
            raise CutoverPreconditionError("The wrapped operator has a different inventory.")
        return self.operator

    def _register_frozen_root(self, attachment: Mapping[str, Any]) -> Mapping[str, Any]:
        root_id = str(attachment["root_id"])
        with self._connect_ro() as conn:
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM task_migration_local_link_stage WHERE cohort_id=?",
                    (self.inventory.cohort_id,),
                ).fetchone()[0]
            )
        if count == 0:
            return {"root_id": root_id, "registered": False, "reason": "no linked files"}
        registry = LocalFileLinkRegistry(self.task_db_path, self.paths.root_bindings)
        binding = registry.register_root(
            root_id=root_id,
            root=self.paths.frozen_tree,
            manifest_sha256=self.inventory.manifest_sha256,
            label="Frozen legacy task tree",
            status="sealed",
        )
        return {
            "root_id": binding.root_id,
            "registered": True,
            "policy_revision": binding.policy_revision,
            "root": str(binding.root),
        }

    def _write_action_receipt(self, action: str, payload: Mapping[str, Any]) -> Path:
        return _atomic_receipt(
            self.paths.receipts,
            cohort_id=self.inventory.cohort_id,
            action=action,
            payload={"completed_at": _iso(self.clock()), **dict(payload)},
        )

    def prepare(self, *, target_authority_epoch: str) -> Mapping[str, Any]:
        operator = self._require_operator()
        checks = self.checks()
        self._require(checks, _PREPARE_GATES)
        existing = self._cohort()
        if str(existing["state"]) in {
            "bindings_applying",
            "bindings_verified",
            "active",
        }:
            if str(existing.get("target_authority_epoch") or "") != target_authority_epoch:
                raise CutoverPreconditionError(
                    "The resumed cohort has a different target authority epoch."
                )
            receipt = self._write_action_receipt(
                "prepare",
                {
                    "target_authority_epoch": target_authority_epoch,
                    "cohort_state": existing["state"],
                    "replayed": True,
                },
            )
            return {"cohort": existing, "operator_receipt": str(receipt), "replayed": True}
        # capture-stop uses this same file lock before it reads the cohort and
        # replaces the fixed receipt path. Hold it from the exact receipt
        # re-read through root registration and the SQLite fence CAS, so the
        # fence can never commit a digest whose receipt was concurrently
        # replaced while the cohort still said shadow.
        with file_lock(self.paths.process_stop_receipt, timeout=30.0):
            stop = self._stop_receipt_evidence()
            checks["process_generations_stopped"] = GateCheck(
                name="process_generations_stopped",
                passed=True,
                evidence=stop,
            )
            root_binding = self._register_frozen_root(
                checks["attachment_parity"].evidence
            )
            fence_receipt_id = "fence_" + str(stop["stop_payload_sha256"])[:32]
            operator.ledger.arm_mutation_fence(
                self.inventory.cohort_id,
                fence_receipt_id=fence_receipt_id,
                expected_process_generation=int(stop["process_generation"]),
                actor=operator.actor,
                session_id=operator.session_id,
            )
        for name in _PREPARE_GATES:
            operator.ledger.record_gate(
                self.inventory.cohort_id,
                name,
                passed=True,
                evidence=checks[name].evidence,
            )
        fence = self._fence_evidence()
        operator.ledger.record_gate(
            self.inventory.cohort_id,
            "legacy_mutation_fenced",
            passed=True,
            evidence=fence,
        )
        result = operator.prepare(target_authority_epoch=target_authority_epoch)
        receipt = self._write_action_receipt(
            "prepare",
            {
                "target_authority_epoch": target_authority_epoch,
                "fence": fence,
                "root_binding": root_binding,
                "gate_evidence_sha256": canonical_sha256(
                    {name: checks[name].evidence for name in sorted(_PREPARE_GATES)}
                ),
                "cohort_state": result.get("state"),
            },
        )
        return {"cohort": result, "operator_receipt": str(receipt)}

    def apply_and_verify_bindings(self) -> Mapping[str, Any]:
        operator = self._require_operator()
        checks = self.checks()
        self._require(checks, REQUIRED_ACTIVATION_GATES - {"binding_cohort_verified"})
        cohort = self._cohort()
        if str(cohort["state"]) in {"bindings_verified", "active"}:
            result = {"applied": 0, "verified": self._binding_evidence()["bindings"], "replayed": True}
        else:
            result = operator.apply_and_verify_bindings()
            # The stage/binding cohort has changed.  The digest-keyed cache
            # already prevents reuse; clearing also bounds retained isolated
            # restore evidence during a long-lived operator session.
            self._portable_restore_cache.clear()
        receipt = self._write_action_receipt("bindings", result)
        return {**result, "operator_receipt": str(receipt)}

    def activate(self, *, confirmation: str) -> Mapping[str, Any]:
        if confirmation != ACTIVATION_CONFIRMATION:
            raise CutoverPreconditionError(
                "Native Task activation needs the exact operator confirmation token."
            )
        operator = self._require_operator()
        cohort = self._cohort()
        already_active = str(cohort["state"]) == "active"
        if str(cohort["state"]) in {"prepared", "bindings_applying"}:
            raise CutoverPreconditionError(
                "Run the explicit bindings action before activation; the verified pre-binding "
                "portable recovery pair must then pass an isolated transition replay."
            )
        checks = self.checks()
        self._require(checks, PRODUCTION_ACTIVATION_GATES)
        # Refresh every externally-derived gate immediately before the final
        # binding/document-head CAS and SQLite authority transaction.
        if not already_active:
            for name in sorted(PRODUCTION_ACTIVATION_GATES - {"binding_cohort_verified"}):
                operator.ledger.record_gate(
                    self.inventory.cohort_id,
                    name,
                    passed=True,
                    evidence=checks[name].evidence,
                )
        result = operator.activate(
            confirmation=confirmation,
            sealed_tree_manifest_sha256=self.inventory.manifest_sha256,
        )
        receipt = self._write_action_receipt(
            "activate",
            {
                "cohort_state": result.get("state"),
                "authority_epoch": result.get("target_authority_epoch"),
                "cutover_receipt_id": result.get("cutover_receipt_id"),
                "required_gate_evidence_sha256": canonical_sha256(
                    {name: checks[name].evidence for name in sorted(checks)}
                ),
            },
        )
        return {"cohort": result, "operator_receipt": str(receipt)}

    def abort_before_activation(self) -> Mapping[str, Any]:
        operator = self._require_operator()
        result = operator.abort_before_activation()
        receipt = self._write_action_receipt(
            "abort-before-activation", {"cohort_state": result.get("state")}
        )
        return {"cohort": result, "operator_receipt": str(receipt)}


def _default_paths(args: argparse.Namespace) -> CutoverPaths:
    data_root = paths.data_dir()
    return CutoverPaths(
        manifest=Path(args.manifest),
        frozen_tree=Path(args.frozen_tree),
        legacy_tree=Path(args.legacy_tree),
        backup_receipts=Path(args.backup_receipts),
        restore_rehearsal=Path(args.restore_rehearsal),
        rollback_rehearsal=Path(args.rollback_rehearsal),
        process_stop_receipt=Path(args.process_stop_receipt),
        receipts=Path(args.receipt_dir),
        root_bindings=Path(args.root_bindings or data_root / "runtime" / "cowork_local_file_roots.db"),
        operations=Path(args.operations_dir or data_root / "agents" / "operations"),
        sidecar_state=Path(args.sidecar_state or paths.resolve("runtime/sidecar-state")),
        sidecar_pid=Path(args.sidecar_pid or paths.resolve("runtime/sidecar-pid")),
        tray_pid=Path(args.tray_pid or paths.resolve("runtime/tray-pid")),
        job_roots=tuple(
            Path(item)
            for item in (
                args.job_root
                or [paths.repo_root() / "sidecar_jobs", data_root / "user_jobs"]
            )
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or execute the independently verified native Task cutover. "
            "The default/status action never writes."
        )
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=(
            "status",
            "capture-stop",
            "cancel-retries",
            "prepare",
            "bindings",
            "activate",
            "abort",
        ),
        default="status",
    )
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--task-db", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--frozen-tree", required=True)
    parser.add_argument("--legacy-tree", required=True)
    parser.add_argument("--backup-receipts", required=True)
    parser.add_argument("--restore-rehearsal", required=True)
    parser.add_argument("--rollback-rehearsal", required=True)
    parser.add_argument("--process-stop-receipt", required=True)
    parser.add_argument("--receipt-dir", required=True)
    parser.add_argument("--root-bindings")
    parser.add_argument("--operations-dir")
    parser.add_argument("--sidecar-state")
    parser.add_argument("--sidecar-pid")
    parser.add_argument("--tray-pid")
    parser.add_argument("--job-root", action="append")
    parser.add_argument("--backup-fresh-hours", type=float, default=24.0)
    parser.add_argument("--stop-fresh-minutes", type=float, default=15.0)
    parser.add_argument("--target-authority-epoch")
    parser.add_argument("--confirmation")
    parser.add_argument("--sources-root")
    parser.add_argument("--cowork-store-root")
    parser.add_argument("--truth-registry")
    parser.add_argument("--actor", default="operator:production-task-cutover")
    parser.add_argument("--session-id")
    return parser


def _inventory(args: argparse.Namespace, resolved: CutoverPaths) -> LegacyInventory:
    # Parse the supplied CSV here as an early syntax check.  Exact membership
    # and its digest are revalidated by status; the immutable accepted
    # inventory itself comes from the cohort ledger (see load_accepted_inventory).
    LegacyManifestEntry.from_csv(resolved.manifest)
    return load_accepted_inventory(args.task_db, cohort_id=args.cohort_id)


def _operator_context(args: argparse.Namespace, inventory: LegacyInventory):
    missing = [
        name
        for name in ("sources_root", "cowork_store_root", "truth_registry")
        if not getattr(args, name)
    ]
    if missing:
        rendered = ", ".join("--" + name.replace("_", "-") for name in missing)
        raise CutoverPreconditionError(f"Mutating cutover actions also require: {rendered}")
    task_store = TaskStore(args.task_db)
    sources = SourceStore.create(args.sources_root)
    principal = ActorRef(
        sources.authority_id,
        "production-task-cutover",
        "service",
        "task-migration-tenant",
    )
    stores = TaskDocumentStoreManager(
        root=args.cowork_store_root,
        registry=TruthStoreRegistry(args.truth_registry),
    )
    importer = LegacyTaskDocumentImporter(
        source_root=args.frozen_tree,
        sources=sources,
        principal=principal,
        stores=stores,
        attestation_actor_ref=args.actor,
    )
    operator = LegacyTaskCutoverOperator(
        inventory=inventory,
        source_root=args.frozen_tree,
        task_store=task_store,
        document_importer=importer,
        actor=args.actor,
        session_id=args.session_id,
    )
    return importer, operator


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    resolved = _default_paths(args)
    inventory = _inventory(args, resolved)
    common = {
        "inventory": inventory,
        "task_db_path": args.task_db,
        "cutover_paths": resolved,
        "backup_freshness": timedelta(hours=max(0.01, args.backup_fresh_hours)),
        "stop_receipt_freshness": timedelta(minutes=max(0.1, args.stop_fresh_minutes)),
    }
    try:
        if args.action in {"status", "capture-stop", "cancel-retries"}:
            cutover = ProductionTaskCutover(**common)
            if args.action == "capture-stop":
                result = cutover.capture_process_stop_receipt()
            elif args.action == "cancel-retries":
                result = cutover.cancel_legacy_retries(
                    confirmation=str(args.confirmation or "")
                )
            else:
                result = cutover.status()
        else:
            importer, operator = _operator_context(args, inventory)
            try:
                cutover = ProductionTaskCutover(**common, operator=operator)
                if args.action == "prepare":
                    if not args.target_authority_epoch:
                        raise CutoverPreconditionError("prepare requires --target-authority-epoch")
                    result = cutover.prepare(target_authority_epoch=args.target_authority_epoch)
                elif args.action == "bindings":
                    result = cutover.apply_and_verify_bindings()
                elif args.action == "activate":
                    result = cutover.activate(confirmation=str(args.confirmation or ""))
                else:
                    result = cutover.abort_before_activation()
            finally:
                importer.close()
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "details": dict(getattr(exc, "details", {}) or {}),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if args.action == "status" and not bool(result.get("ready_for_prepare")):
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CANCEL_RETRIES_CONFIRMATION",
    "CutoverPaths",
    "GateCheck",
    "ProductionTaskCutover",
    "RESTORE_RECEIPT_SCHEMA",
    "RETRY_CANCELLATION_RECEIPT_SCHEMA",
    "STOP_RECEIPT_SCHEMA",
    "load_accepted_inventory",
    "main",
]
