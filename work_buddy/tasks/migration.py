"""Typed, deterministic inventory and durable cohort control for task cutover.

This is an explicit one-way legacy import boundary.  It understands the old
task-list syntax, but it does not import the Obsidian integration package and
never treats a discovered file as authority merely because it exists.  Every
source entry must be named by a hash manifest before it can enter a cohort.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import posixpath
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.store import TruthStore

from .migrations import LEGACY_SCHEMA_VERSION, TASK_MIGRATIONS
from .runtime import arm_native_authority_latch, clear_pending_authority_latch
from .store import TaskStore


MIGRATION_SCHEMA_VERSION = 1
RETENTION_POLICY = "until_explicit_user_approval"
REQUIRED_ACTIVATION_GATES = frozenset(
    {
        "inventory_parity",
        "task_parity",
        "document_parity",
        "attachment_parity",
        "backup_restore_rehearsal",
        "legacy_mutation_fenced",
        "process_generations_stopped",
        "frozen_tree_sealed",
        "binding_cohort_verified",
    }
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_TASK_ID_RE = re.compile(r"(?:^|\s)🆔\s*(t-[0-9a-f]+)(?=\s|$)", re.IGNORECASE)
_NOTE_RE = re.compile(
    r"\[\[([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\|📓\]\]",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"(?<![\w/])#([a-z0-9][a-z0-9_/-]*)", re.IGNORECASE)
_DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
_DONE_RE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
_PRIORITY_RE = re.compile(r"[🔽🔼⏫]")
_UNSUPPORTED_PLUGIN_MARKER_RE = re.compile(r"[🔁⏳🛫]")
_WIKI_EMBED_RE = re.compile(r"!\[\[([^\]|#]+)")
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)#]+)(?:#[^)]+)?\)")
_URI_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)
_CODE_LOCATION_RE = re.compile(r"^.+\.[a-z0-9_+-]+:\d+(?::\d+)?$", re.IGNORECASE)
_DIAGNOSTIC_NOTES = frozenset(
    {
        "notes/_bridge_test_new.md",
        "notes/_raw_test.md",
        "notes/_sidecar_diag.md",
        "notes/test-bridge-check.md",
        "notes/test-check.md",
        "notes/test-write-check.md",
    }
)
_ANCILLARY_MARKDOWN = frozenset(
    {"task-dashboard.md", "Untitled.md", "notes/assets/assets.md"}
)
_NATIVE_REPLAY_DEFAULTS: Mapping[str, Any] = {
    "revision": 1,
    "due_date": None,
    "snooze_resume_state": None,
    "restored_at": None,
    "legacy_import_receipt_id": None,
    "summary_text": None,
    "dependencies_json": None,
}


def _accepted_tag(tag: str) -> bool:
    lowered = tag.casefold()
    return lowered not in {"todo", "wb/todo", "wb/done"} and not lowered.startswith(
        "tasker/"
    )


class LegacyMigrationError(RuntimeError):
    """Base error with a stable operator-facing code."""

    code = "legacy_task_migration_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class LegacyInventoryError(LegacyMigrationError):
    code = "legacy_task_inventory_invalid"


class CohortStateError(LegacyMigrationError):
    code = "legacy_task_cohort_state_invalid"


class CutoverPreconditionError(LegacyMigrationError):
    code = "legacy_task_cutover_precondition_failed"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_replay_item(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only schema-evolution defaults from a stored inventory item."""

    normalized = dict(value)
    metadata = dict(normalized.get("metadata") or {})
    if normalized.get("item_kind") == "database_task":
        row = dict(metadata.get("row") or {})
        for field_name, default in _NATIVE_REPLAY_DEFAULTS.items():
            if field_name in row and row[field_name] == default:
                row.pop(field_name)
        metadata["row"] = row
    normalized["metadata"] = metadata
    return normalized


def _inventory_replay_signature(
    *,
    cohort_id: str,
    manifest_sha256: str,
    source_root_fingerprint: str,
    source_file_count: int,
    source_tree_bytes: int,
    counts: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
) -> str:
    """Fingerprint immutable inputs while ignoring the ledger's own DB writes."""

    return canonical_sha256(
        {
            "schema": "wb.legacy-task-inventory-replay/v1",
            "cohort_id": cohort_id,
            "manifest_sha256": manifest_sha256,
            "source_root_fingerprint": source_root_fingerprint,
            "source_file_count": int(source_file_count),
            "source_tree_bytes": int(source_tree_bytes),
            "counts": dict(counts),
            "items": [
                _normalized_replay_item(item)
                for item in sorted(items, key=lambda item: str(item["item_key"]))
            ],
        }
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized_markdown_sha256(value: bytes) -> str:
    text = value.decode("utf-8-sig")
    return sha256_bytes(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))


def _safe_relative(value: str) -> str:
    raw = value.replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise LegacyInventoryError(f"Unsafe manifest path: {value!r}")
    return path.as_posix()


def _file_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            length += len(chunk)
            digest.update(chunk)
    return length, digest.hexdigest()


@dataclass(frozen=True, slots=True)
class LegacyManifestEntry:
    relative_path: str
    byte_length: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "relative_path", _safe_relative(self.relative_path))
        if self.byte_length < 0:
            raise LegacyInventoryError("Manifest byte lengths cannot be negative.")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise LegacyInventoryError("Manifest SHA-256 values must be lowercase hex.")

    @classmethod
    def from_csv(cls, path: str | Path) -> tuple["LegacyManifestEntry", ...]:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
            rows = csv.DictReader(stream)
            required = {"relative_path", "bytes", "sha256"}
            if rows.fieldnames is None or not required <= set(rows.fieldnames):
                raise LegacyInventoryError("Manifest CSV needs relative_path, bytes, and sha256.")
            return tuple(
                cls(
                    relative_path=str(row["relative_path"]),
                    byte_length=int(row["bytes"]),
                    sha256=str(row["sha256"]).strip().lower(),
                )
                for row in rows
            )


@dataclass(frozen=True, slots=True)
class ParsedLegacyTaskLine:
    source_key: str
    relative_path: str
    line_number: int
    exact_bytes: bytes
    line_sha256: str
    task_id: str | None
    imported_task_id: str
    description: str
    state: str
    urgency: str
    due_date: str | None
    completed_at: str | None
    archived: bool
    note_uuid: str | None
    tags: tuple[str, ...]
    checked: bool
    date_ambiguity: bool = False

    @property
    def is_idless(self) -> bool:
        return self.task_id is None

    def stage_fields(self, *, cohort_id: str, timestamp: str) -> dict[str, Any]:
        return {
            "description": self.description,
            "state": self.state,
            "urgency": self.urgency,
            "note_uuid": self.note_uuid,
            "due_date": self.due_date,
            "completed_at": self.completed_at,
            "archived_at": timestamp if self.archived else None,
            "created_at": timestamp,
            "updated_at": timestamp,
            "creation_provenance": "legacy_markdown_recovery",
            "legacy_import_receipt_id": f"legacy-line:{cohort_id}:{self.source_key}",
            "date_ambiguity": self.date_ambiguity,
        }


@dataclass(frozen=True, slots=True)
class InventoryItem:
    item_key: str
    item_kind: str
    classification: str
    reason: str
    relative_path: str | None = None
    line_number: int | None = None
    task_id: str | None = None
    note_uuid: str | None = None
    content_sha256: str | None = None
    byte_length: int | None = None
    source_bytes: bytes | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def digest_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_bytes"] = (
            None if self.source_bytes is None else sha256_bytes(self.source_bytes)
        )
        return value


@dataclass(frozen=True, slots=True)
class LegacyInventory:
    cohort_id: str
    manifest_sha256: str
    inventory_sha256: str
    source_root_fingerprint: str
    source_db_sha256: str
    source_db_integrity: str
    source_db_schema_version: int
    source_file_count: int
    source_tree_bytes: int
    items: tuple[InventoryItem, ...]
    task_lines: tuple[ParsedLegacyTaskLine, ...]
    errors: tuple[str, ...]
    counts: Mapping[str, int]

    @property
    def valid(self) -> bool:
        return not self.errors and self.source_db_integrity == "ok"

    def require_valid(self) -> "LegacyInventory":
        if not self.valid:
            raise LegacyInventoryError(
                "The legacy task inventory failed closed.",
                details={"errors": list(self.errors), "counts": dict(self.counts)},
            )
        return self

    def to_dict(self, *, include_items: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": "wb.legacy-task-inventory/v1",
            "cohort_id": self.cohort_id,
            "manifest_sha256": self.manifest_sha256,
            "inventory_sha256": self.inventory_sha256,
            "source_root_fingerprint": self.source_root_fingerprint,
            "source_db_sha256": self.source_db_sha256,
            "source_db_integrity": self.source_db_integrity,
            "source_db_schema_version": self.source_db_schema_version,
            "source_file_count": self.source_file_count,
            "source_tree_bytes": self.source_tree_bytes,
            "counts": dict(self.counts),
            "errors": list(self.errors),
            "valid": self.valid,
        }
        if include_items:
            result["items"] = [item.digest_dict() for item in self.items]
        return result


def deterministic_import_task_id(
    cohort_id: str,
    relative_path: str,
    line_number: int,
    exact_line_sha256: str,
) -> str:
    identity = f"wb-legacy-task/v1\0{cohort_id}\0{relative_path}\0{line_number}\0{exact_line_sha256}"
    return "t-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _description(line: str) -> str:
    value = re.sub(r"^\s*-\s*\[[ xX]\]\s*", "", line, count=1)
    value = re.sub(r"^#todo(?:\s+|$)", "", value, count=1, flags=re.IGNORECASE)
    boundaries = [
        index
        for token in ("[[", "#", "🆔", "📅", "✅", "🔽", "🔼", "⏫")
        if (index := value.find(token)) >= 0
    ]
    return value[: min(boundaries) if boundaries else len(value)].strip()


def _parse_date(match: re.Match[str] | None, *, label: str, errors: list[str]) -> str | None:
    if match is None:
        return None
    candidate = match.group(1)
    try:
        date.fromisoformat(candidate)
    except ValueError:
        errors.append(f"invalid {label} date {candidate}")
    return candidate


def _split_exact_lines(data: bytes) -> Iterable[tuple[int, bytes, str]]:
    for number, exact in enumerate(data.splitlines(keepends=True), 1):
        body = exact.rstrip(b"\r\n")
        yield number, exact, body.decode("utf-8-sig" if number == 1 else "utf-8")
    if not data:
        return
    # bytes.splitlines() already returns the final unterminated line.


def _resolve_embed(
    source_path: str,
    target: str,
    manifest_paths: set[str],
    basenames: Mapping[str, Sequence[str]],
) -> str | None:
    clean = target.strip().replace("\\", "/")
    if not clean or re.match(r"^[a-z][a-z0-9+.-]*://", clean, re.IGNORECASE):
        return None
    candidate = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), clean))
    if not candidate.startswith("../") and candidate in manifest_paths:
        return candidate
    direct = posixpath.normpath(clean)
    if not direct.startswith("../") and direct in manifest_paths:
        return direct
    matches = list(basenames.get(posixpath.basename(clean).casefold(), ()))
    return matches[0] if len(matches) == 1 else None


def _preserved_reference_classification(target: str, *, syntax: str) -> str | None:
    """Classify a non-manifest Markdown target that is not an attachment.

    Co-work preserves these strings in the imported document body, but they
    must not participate in frozen-tree attachment parity. Relative paths
    that look like real files remain fail-closed so a missing PDF, key, image,
    or note cannot be waved through as an ordinary link.
    """

    clean = target.strip()
    if not clean:
        return "empty_reference"
    if _WINDOWS_ABSOLUTE_RE.match(clean):
        return "external_local_path"
    if clean.startswith(("/", "\\")):
        return "absolute_uri_path"
    if _URI_SCHEME_RE.match(clean):
        return "external_uri"
    if syntax == "markdown" and _CODE_LOCATION_RE.match(clean):
        return "code_location"
    normalized = clean.replace("\\", "/")
    if (
        syntax == "markdown"
        and "/" not in normalized
        and not PurePosixPath(normalized).suffix
    ):
        return "relative_url_or_placeholder"
    return None


class LegacyTaskInventoryBuilder:
    """Build an exact inventory from a supplied manifest and read-only DB."""

    def __init__(
        self,
        *,
        cohort_id: str,
        source_root: str | Path,
        task_db_path: str | Path,
        manifest: Sequence[LegacyManifestEntry],
        reject_unmanifested: bool = True,
    ) -> None:
        self.cohort_id = str(cohort_id).strip()
        self.source_root = Path(source_root).expanduser().resolve()
        self.task_db_path = Path(task_db_path).expanduser().resolve()
        self.manifest = tuple(manifest)
        self.reject_unmanifested = reject_unmanifested
        if not self.cohort_id or len(self.cohort_id) > 128:
            raise LegacyInventoryError("A bounded cohort_id is required.")

    def build(self) -> LegacyInventory:
        errors: list[str] = []
        if not self.source_root.is_dir() or self.source_root.is_symlink():
            raise LegacyInventoryError("The legacy task root must be a real directory.")
        if not self.task_db_path.is_file() or self.task_db_path.is_symlink():
            raise LegacyInventoryError("The legacy task database must be a regular file.")

        manifest_by_path: dict[str, LegacyManifestEntry] = {}
        for entry in self.manifest:
            if entry.relative_path in manifest_by_path:
                errors.append(f"duplicate manifest path: {entry.relative_path}")
            manifest_by_path[entry.relative_path] = entry
        manifest_paths = set(manifest_by_path)
        manifest_sha = canonical_sha256(
            [asdict(manifest_by_path[path]) for path in sorted(manifest_by_path)]
        )

        if self.reject_unmanifested:
            discovered: set[str] = set()
            for path in self.source_root.rglob("*"):
                if path.is_symlink():
                    errors.append(
                        f"linked source entry is forbidden: {path.relative_to(self.source_root).as_posix()}"
                    )
                elif path.is_file():
                    discovered.add(path.relative_to(self.source_root).as_posix())
            for extra in sorted(discovered - manifest_paths):
                errors.append(f"unmanifested source entry: {extra}")
            for missing in sorted(manifest_paths - discovered):
                errors.append(f"manifest entry missing: {missing}")

        file_bytes: dict[str, bytes] = {}
        verified: dict[str, tuple[int, str]] = {}
        for relative, entry in sorted(manifest_by_path.items()):
            path = self.source_root.joinpath(*PurePosixPath(relative).parts)
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self.source_root)
                if path.is_symlink() or not resolved.is_file():
                    raise OSError("not a regular source file")
                length, digest = _file_sha256(resolved)
            except (OSError, ValueError) as exc:
                errors.append(f"cannot verify {relative}: {exc}")
                continue
            if length != entry.byte_length or digest != entry.sha256:
                errors.append(f"manifest mismatch: {relative}")
                continue
            verified[relative] = (length, digest)
            if relative.lower().endswith(".md"):
                if length > 16 * 1024 * 1024:
                    errors.append(f"Markdown exceeds the 16 MiB import limit: {relative}")
                    continue
                data = resolved.read_bytes()
                try:
                    data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    errors.append(f"invalid UTF-8 Markdown: {relative}")
                    continue
                file_bytes[relative] = data

        db_length, db_sha = _file_sha256(self.task_db_path)
        del db_length
        db_rows, tags, db_integrity, db_version = self._read_database(errors)
        db_by_id = {str(row["task_id"]): row for row in db_rows}
        db_note_to_ids: dict[str, list[str]] = {}
        for row in db_rows:
            if row.get("note_uuid"):
                db_note_to_ids.setdefault(str(row["note_uuid"]).casefold(), []).append(
                    str(row["task_id"])
                )
        for note_uuid, task_ids in sorted(db_note_to_ids.items()):
            if len(task_ids) > 1:
                errors.append(
                    f"database note UUID is linked to multiple tasks: {note_uuid} "
                    f"({', '.join(sorted(task_ids))})"
                )

        task_lines: list[ParsedLegacyTaskLine] = []
        line_task_ids: dict[str, str] = {}
        note_line_map: dict[str, str] = {}
        for relative in ("master-task-list.md", "archive.md"):
            data = file_bytes.get(relative)
            if data is None:
                errors.append(f"required task list unavailable: {relative}")
                continue
            for line_number, exact, decoded in _split_exact_lines(data):
                stripped = decoded.strip()
                if not re.match(r"^-\s*\[[ xX]\]", stripped):
                    continue
                line_errors: list[str] = []
                if _UNSUPPORTED_PLUGIN_MARKER_RE.search(stripped):
                    line_errors.append("unsupported plugin marker")
                task_match = _TASK_ID_RE.search(stripped)
                task_id = task_match.group(1).lower() if task_match else None
                note_match = _NOTE_RE.search(stripped)
                note_uuid = note_match.group(1).lower() if note_match else None
                due = _parse_date(_DUE_RE.search(stripped), label="due", errors=line_errors)
                completed = _parse_date(
                    _DONE_RE.search(stripped), label="completion", errors=line_errors
                )
                description = _description(stripped)
                if not description:
                    line_errors.append("empty description")
                checked = bool(re.match(r"^-\s*\[[xX]\]", stripped))
                urgency_match = _PRIORITY_RE.search(stripped)
                urgency = {
                    "⏫": "high",
                    "🔼": "medium",
                    "🔽": "low",
                }.get(urgency_match.group() if urgency_match else "", "medium")
                line_sha = sha256_bytes(exact)
                source_key = f"line:{relative}:{line_number}:{line_sha[:16]}"
                imported_id = task_id or deterministic_import_task_id(
                    self.cohort_id, relative, line_number, line_sha
                )
                if imported_id in line_task_ids:
                    line_errors.append(
                        f"duplicate task identity also at {line_task_ids[imported_id]}"
                    )
                else:
                    line_task_ids[imported_id] = f"{relative}:{line_number}"
                if note_uuid:
                    if note_uuid in note_line_map:
                        line_errors.append(
                            f"duplicate note link also at {note_line_map[note_uuid]}"
                        )
                    else:
                        note_line_map[note_uuid] = imported_id
                if task_id:
                    row = db_by_id.get(task_id)
                    if row is None or row.get("deleted_at") is not None:
                        line_errors.append("identified task is absent or tombstoned in database")
                    elif (row.get("note_uuid") or None) != note_uuid:
                        line_errors.append("task note UUID differs from the database row")
                date_ambiguity = bool(
                    task_id
                    and due
                    and db_by_id.get(task_id, {}).get("deadline_date") == due
                )
                parsed = ParsedLegacyTaskLine(
                    source_key=source_key,
                    relative_path=relative,
                    line_number=line_number,
                    exact_bytes=exact,
                    line_sha256=line_sha,
                    task_id=task_id,
                    imported_task_id=imported_id,
                    description=description,
                    state="done" if checked else "inbox",
                    urgency=urgency,
                    due_date=due,
                    completed_at=completed,
                    archived=relative == "archive.md",
                    note_uuid=note_uuid,
                    tags=tuple(
                        dict.fromkeys(
                            tag.casefold()
                            for tag in _TAG_RE.findall(stripped)
                            if _accepted_tag(tag)
                        )
                    ),
                    checked=checked,
                    date_ambiguity=date_ambiguity,
                )
                task_lines.append(parsed)
                for problem in line_errors:
                    errors.append(f"{relative}:{line_number}: {problem}")

        live_ids = {task_id for task_id, row in db_by_id.items() if row.get("deleted_at") is None}
        identified_ids = {line.task_id for line in task_lines if line.task_id is not None}
        for missing in sorted(live_ids - identified_ids):
            errors.append(f"live database task has no identified Markdown line: {missing}")
        for collision in sorted(
            {line.imported_task_id for line in task_lines if line.is_idless} & set(db_by_id)
        ):
            errors.append(f"deterministic ID-less task collides with database row: {collision}")

        basenames: dict[str, list[str]] = {}
        for relative in manifest_paths:
            basenames.setdefault(posixpath.basename(relative).casefold(), []).append(relative)
        referenced_by: dict[str, list[str]] = {}
        unresolved_embeds: list[tuple[str, str]] = []
        preserved_references: dict[str, list[dict[str, str]]] = {}
        for relative, data in file_bytes.items():
            if not relative.startswith("notes/"):
                continue
            text = data.decode("utf-8-sig")
            targets = [
                *(("wiki", target) for target in _WIKI_EMBED_RE.findall(text)),
                *(("markdown", target) for target in _MARKDOWN_LINK_RE.findall(text)),
            ]
            for syntax, target in targets:
                resolved = _resolve_embed(relative, target, manifest_paths, basenames)
                if resolved is None:
                    classification = _preserved_reference_classification(
                        target,
                        syntax=syntax,
                    )
                    if classification is None:
                        unresolved_embeds.append((relative, target))
                    else:
                        preserved_references.setdefault(relative, []).append(
                            {
                                "syntax": syntax,
                                "classification": classification,
                                "target": target,
                            }
                        )
                    continue
                referenced_by.setdefault(resolved, []).append(relative)
        for relative, target in unresolved_embeds:
            errors.append(f"unresolved local reference in {relative}: {target}")

        items: list[InventoryItem] = []
        for line in task_lines:
            items.append(
                InventoryItem(
                    item_key=line.source_key,
                    item_kind="task_line",
                    classification=("idless_task_stage" if line.is_idless else "identified_task"),
                    reason=(
                        "ID-less legacy task is staged until cohort activation."
                        if line.is_idless
                        else "Legacy identity is preserved on the evolved database row."
                    ),
                    relative_path=line.relative_path,
                    line_number=line.line_number,
                    task_id=line.imported_task_id,
                    note_uuid=line.note_uuid,
                    content_sha256=line.line_sha256,
                    byte_length=len(line.exact_bytes),
                    source_bytes=line.exact_bytes,
                    metadata={
                        "state": line.state,
                        "urgency": line.urgency,
                        "due_date": line.due_date,
                        "completed_at": line.completed_at,
                        "archived": line.archived,
                        "tags": list(line.tags),
                        "date_ambiguity": line.date_ambiguity,
                    },
                )
            )

        for row in sorted(db_rows, key=lambda item: str(item["task_id"])):
            task_id = str(row["task_id"])
            items.append(
                InventoryItem(
                    item_key=f"db-task:{task_id}",
                    item_kind="database_task",
                    classification=(
                        "existing_tombstone" if row.get("deleted_at") is not None else "existing_live"
                    ),
                    reason="Existing task row is evolved in place; it is never copied.",
                    task_id=task_id,
                    note_uuid=row.get("note_uuid"),
                    metadata={"row": row, "tags": tags.get(task_id, [])},
                )
            )

        for relative, entry in sorted(manifest_by_path.items()):
            classification, reason, task_id, note_uuid = self._classify_file(
                relative,
                note_line_map=note_line_map,
                db_by_id=db_by_id,
                db_note_to_ids=db_note_to_ids,
                referenced=bool(referenced_by.get(relative)),
            )
            if classification == "unclassified":
                errors.append(f"source entry has no accepted classification: {relative}")
            metadata: dict[str, Any] = {
                "referenced_by": sorted(referenced_by.get(relative, [])),
                "preserved_non_manifest_references": sorted(
                    preserved_references.get(relative, []),
                    key=lambda item: (
                        item["classification"],
                        item["syntax"],
                        item["target"],
                    ),
                ),
            }
            items.append(
                InventoryItem(
                    item_key=f"file:{relative}",
                    item_kind="source_file",
                    classification=classification,
                    reason=reason,
                    relative_path=relative,
                    task_id=task_id,
                    note_uuid=note_uuid,
                    content_sha256=entry.sha256,
                    byte_length=entry.byte_length,
                    metadata=metadata,
                )
            )

        standard_note_paths = {
            f"notes/{note}.md" for note in db_note_to_ids
        } | {f"notes/{note}.md" for note in note_line_map}
        for note_path in sorted(standard_note_paths - manifest_paths):
            note_uuid = PurePosixPath(note_path).stem.casefold()
            ids = db_note_to_ids.get(note_uuid, [])
            live_missing = any(db_by_id[task_id].get("deleted_at") is None for task_id in ids)
            if live_missing or note_uuid in note_line_map:
                errors.append(f"live task note is missing: {note_uuid}")
                classification = "missing_live_note"
                reason = "A live task references a missing note; activation is blocked."
            else:
                classification = "dangling_deleted_note"
                reason = "A deleted task's missing note remains explicit evidence."
            items.append(
                InventoryItem(
                    item_key=f"missing-note:{note_uuid}",
                    item_kind="missing_note_reference",
                    classification=classification,
                    reason=reason,
                    task_id=(ids[0] if len(ids) == 1 else None),
                    note_uuid=note_uuid,
                    metadata={"database_task_ids": sorted(ids)},
                )
            )

        counts: dict[str, int] = {}
        for item in items:
            counts[item.classification] = counts.get(item.classification, 0) + 1
        counts["database_tasks"] = len(db_rows)
        counts["task_lines"] = len(task_lines)
        counts["idless_tasks"] = sum(line.is_idless for line in task_lines)
        counts["identified_tasks"] = sum(not line.is_idless for line in task_lines)

        ordered = tuple(sorted(items, key=lambda item: item.item_key))
        digest_payload = {
            "schema": "wb.legacy-task-inventory/v1",
            "cohort_id": self.cohort_id,
            "manifest_sha256": manifest_sha,
            "source_db_sha256": db_sha,
            "source_db_integrity": db_integrity,
            "source_db_schema_version": db_version,
            "items": [item.digest_dict() for item in ordered],
        }
        inventory_sha = canonical_sha256(digest_payload)
        return LegacyInventory(
            cohort_id=self.cohort_id,
            manifest_sha256=manifest_sha,
            inventory_sha256=inventory_sha,
            source_root_fingerprint=hashlib.sha256(
                os.path.normcase(str(self.source_root)).encode("utf-8")
            ).hexdigest(),
            source_db_sha256=db_sha,
            source_db_integrity=db_integrity,
            source_db_schema_version=db_version,
            source_file_count=len(self.manifest),
            source_tree_bytes=sum(entry.byte_length for entry in self.manifest),
            items=ordered,
            task_lines=tuple(sorted(task_lines, key=lambda line: line.source_key)),
            errors=tuple(sorted(set(errors))),
            counts=counts,
        )

    def _read_database(
        self, errors: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], str, int]:
        uri = f"file:{self.task_db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity = str(integrity_row[0]) if integrity_row else "unavailable"
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if not LEGACY_SCHEMA_VERSION <= version <= TASK_MIGRATIONS.target_version:
                errors.append(
                    "task database schema is not an accepted legacy/native boundary: "
                    f"{version}"
                )
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "task_metadata" not in tables:
                errors.append("task database has no task_metadata table")
                return [], {}, integrity, version
            rows = [dict(row) for row in conn.execute("SELECT * FROM task_metadata ORDER BY task_id")]
            tags: dict[str, list[dict[str, Any]]] = {}
            if "task_tags" in tables:
                for row in conn.execute(
                    "SELECT task_id, tag, is_namespace FROM task_tags ORDER BY task_id, tag"
                ):
                    tags.setdefault(str(row["task_id"]), []).append(
                        {"tag": str(row["tag"]), "is_namespace": bool(row["is_namespace"])}
                    )
            return rows, tags, integrity, version
        finally:
            conn.close()

    @staticmethod
    def _classify_file(
        relative: str,
        *,
        note_line_map: Mapping[str, str],
        db_by_id: Mapping[str, Mapping[str, Any]],
        db_note_to_ids: Mapping[str, Sequence[str]],
        referenced: bool,
    ) -> tuple[str, str, str | None, str | None]:
        lower = relative.casefold()
        if relative in {"master-task-list.md", "archive.md"}:
            return "task_list_source", "Canonical legacy task-line source.", None, None
        if relative in _DIAGNOSTIC_NOTES:
            return "diagnostic_excluded", "Known diagnostic Markdown is not a task document.", None, None
        if relative in _ANCILLARY_MARKDOWN:
            return "ancillary_excluded", "Explicit ancillary Markdown classification.", None, None
        if lower.startswith("notes/") and lower.endswith(".md"):
            note_uuid = PurePosixPath(relative).stem.casefold()
            if _UUID_RE.fullmatch(note_uuid):
                line_task = note_line_map.get(note_uuid)
                db_ids = list(db_note_to_ids.get(note_uuid, ()))
                if line_task:
                    return (
                        "task_note_idless" if line_task not in db_by_id else "task_note_live",
                        "Task-linked Markdown imports to a projection-free Co-work document.",
                        line_task,
                        note_uuid,
                    )
                if db_ids:
                    deleted_ids = [
                        task_id
                        for task_id in db_ids
                        if db_by_id[task_id].get("deleted_at") is not None
                    ]
                    if len(deleted_ids) == len(db_ids):
                        return (
                            "task_note_deleted",
                            "Deleted-task note imports with retired document/binding lifecycle.",
                            deleted_ids[0] if len(deleted_ids) == 1 else None,
                            note_uuid,
                        )
                    return (
                        "task_note_live_db_only",
                        "A live database task links this note; Markdown task parity will decide cutover.",
                        db_ids[0] if len(db_ids) == 1 else None,
                        note_uuid,
                    )
                return (
                    "recovered_task_document",
                    "Unattached UUID note imports into the recovery catalog without a fake task.",
                    None,
                    note_uuid,
                )
            return "unclassified", "Unexpected Markdown below notes/.", None, None
        suffix = PurePosixPath(relative).suffix.casefold()
        if suffix == ".pdf":
            return (
                "local_file_pdf" if referenced else "backup_only_pdf",
                "PDF bytes remain local; only metadata may enter the link catalog.",
                None,
                None,
            )
        if suffix == ".ppk":
            return (
                "local_file_sensitive" if referenced else "backup_only_sensitive",
                "Private-key bytes remain local and the only allowed action is reveal.",
                None,
                None,
            )
        if suffix in {".mdb", ""}:
            return "backup_only_legacy", "Legacy dashboard/database artifact stays backup-only.", None, None
        return "unclassified", "No importer policy exists for this source type.", None, None


def epoch_number(epoch: str) -> int:
    if epoch == "legacy":
        return 0
    match = re.fullmatch(r"(?:native|rollback):(\d+)", epoch)
    if match is None:
        raise CutoverPreconditionError(
            "Cutover epochs must be legacy, native:<integer>, or rollback:<integer>."
        )
    return int(match.group(1))


class TaskMigrationLedger:
    """Durable cohort state and cross-store transition receipts."""

    def __init__(self, store: TaskStore, *, clock=utc_now) -> None:
        self.store = store
        self.clock = clock

    def cohort(self, cohort_id: str) -> dict[str, Any] | None:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM task_migration_cohorts WHERE cohort_id=?", (cohort_id,)
            ).fetchone()
            if row is None:
                return None
            value = dict(row)
            for key in ("counts_json", "approved_exceptions_json", "backup_receipts_json"):
                value[key.removesuffix("_json")] = json.loads(value.pop(key))
            return value
        finally:
            conn.close()

    def shadow_stage_snapshot(
        self,
        cohort_id: str,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[dict[str, Any], ...]]]:
        """Read the document/link staging cohort used by shadow fast-resume.

        The caller verifies every row against the current inventory and the
        independent Source/Truth stores before treating it as complete.  This
        method intentionally returns immutable value snapshots rather than
        leaking a live task-database connection across those checks.
        """

        conn = self.store.connect()
        try:
            conn.execute("BEGIN")
            self._require_state(conn, cohort_id, {"shadow"})
            documents_by_note = {
                str(row["note_uuid"]): dict(row)
                for row in conn.execute(
                    "SELECT * FROM task_migration_document_stage "
                    "WHERE cohort_id=? ORDER BY note_uuid",
                    (cohort_id,),
                )
            }
            links_by_note: dict[str, list[dict[str, Any]]] = {}
            for row in conn.execute(
                "SELECT * FROM task_migration_local_link_stage "
                "WHERE cohort_id=? ORDER BY note_uuid, link_id",
                (cohort_id,),
            ):
                links_by_note.setdefault(str(row["note_uuid"]), []).append(dict(row))
            result = documents_by_note, {
                note_uuid: tuple(rows) for note_uuid, rows in links_by_note.items()
            }
            conn.execute("COMMIT")
            return result
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def backfill_document_stage_source_receipt(
        self,
        cohort_id: str,
        *,
        note_uuid: str,
        source_receipt_id: str,
    ) -> None:
        """CAS-fill the sole nullable v18 replay field without other writes."""

        receipt = str(source_receipt_id).strip()
        if not receipt:
            raise ValueError("source_receipt_id is required")
        with self.store.transaction() as conn:
            self._require_state(conn, cohort_id, {"shadow"})
            row = conn.execute(
                "SELECT source_receipt_id FROM task_migration_document_stage "
                "WHERE cohort_id=? AND note_uuid=?",
                (cohort_id, note_uuid),
            ).fetchone()
            if row is None:
                raise CohortStateError("The staged task document is missing.")
            existing = row["source_receipt_id"]
            if existing is not None:
                if str(existing) != receipt:
                    raise CohortStateError(
                        "A staged task document receipt changed during backfill."
                    )
                return
            conn.execute(
                "UPDATE task_migration_document_stage SET source_receipt_id=? "
                "WHERE cohort_id=? AND note_uuid=? AND source_receipt_id IS NULL",
                (receipt, cohort_id, note_uuid),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise CohortStateError(
                    "A staged task document receipt changed during backfill."
                )

    def begin_shadow(
        self,
        inventory: LegacyInventory,
        *,
        actor: str,
        session_id: str | None,
        backup_receipts: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        inventory.require_valid()
        if not backup_receipts or any(
            not isinstance(receipt, Mapping) or receipt.get("verified") is not True
            for receipt in backup_receipts
        ):
            raise CutoverPreconditionError(
                "Shadow import requires at least one verified backup receipt."
            )
        now = self.clock()
        system = self.store.system_state()
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM task_migration_cohorts WHERE cohort_id=?",
                (inventory.cohort_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["inventory_sha256"]) != inventory.inventory_sha256:
                    stored_items: list[dict[str, Any]] = []
                    for row in conn.execute(
                        """
                        SELECT item_key, item_kind, classification, reason,
                               relative_path, line_number, task_id, note_uuid,
                               content_sha256, byte_length, source_bytes,
                               metadata_json
                        FROM task_migration_inventory
                        WHERE cohort_id=?
                        ORDER BY item_key
                        """,
                        (inventory.cohort_id,),
                    ):
                        stored = dict(row)
                        source_bytes = stored.pop("source_bytes")
                        stored["source_bytes"] = (
                            None
                            if source_bytes is None
                            else sha256_bytes(bytes(source_bytes))
                        )
                        stored["metadata"] = json.loads(stored.pop("metadata_json"))
                        stored_items.append(stored)
                    stored_signature = _inventory_replay_signature(
                        cohort_id=str(existing["cohort_id"]),
                        manifest_sha256=str(existing["manifest_sha256"]),
                        source_root_fingerprint=str(existing["source_root_fingerprint"]),
                        source_file_count=int(existing["source_file_count"]),
                        source_tree_bytes=int(existing["source_tree_bytes"]),
                        counts=json.loads(str(existing["counts_json"])),
                        items=stored_items,
                    )
                    current_signature = _inventory_replay_signature(
                        cohort_id=inventory.cohort_id,
                        manifest_sha256=inventory.manifest_sha256,
                        source_root_fingerprint=inventory.source_root_fingerprint,
                        source_file_count=inventory.source_file_count,
                        source_tree_bytes=inventory.source_tree_bytes,
                        counts=inventory.counts,
                        items=[item.digest_dict() for item in inventory.items],
                    )
                    if stored_signature != current_signature:
                        raise CohortStateError(
                            "The cohort ID is already bound to another inventory."
                        )
                return dict(existing)
            conn.execute(
                """
                INSERT INTO task_migration_cohorts (
                    cohort_id, schema_version, state, inventory_sha256,
                    manifest_sha256, source_file_count, source_tree_bytes,
                    source_root_fingerprint, source_db_sha256, source_db_integrity,
                    source_db_schema_version, previous_authority_epoch, actor,
                    session_id, retention_policy, counts_json,
                    backup_receipts_json, created_at, updated_at
                ) VALUES (?, ?, 'shadow', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inventory.cohort_id,
                    MIGRATION_SCHEMA_VERSION,
                    inventory.inventory_sha256,
                    inventory.manifest_sha256,
                    inventory.source_file_count,
                    inventory.source_tree_bytes,
                    inventory.source_root_fingerprint,
                    inventory.source_db_sha256,
                    inventory.source_db_integrity,
                    inventory.source_db_schema_version,
                    system.authority_epoch,
                    actor,
                    session_id,
                    RETENTION_POLICY,
                    canonical_json(inventory.counts),
                    canonical_json(list(backup_receipts)),
                    now,
                    now,
                ),
            )
            for item in inventory.items:
                conn.execute(
                    """
                    INSERT INTO task_migration_inventory (
                        cohort_id, item_key, item_kind, relative_path, line_number,
                        task_id, note_uuid, content_sha256, byte_length,
                        classification, reason, source_bytes, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        inventory.cohort_id,
                        item.item_key,
                        item.item_kind,
                        item.relative_path,
                        item.line_number,
                        item.task_id,
                        item.note_uuid,
                        item.content_sha256,
                        item.byte_length,
                        item.classification,
                        item.reason,
                        item.source_bytes,
                        canonical_json(dict(item.metadata)),
                    ),
                )
            for line in inventory.task_lines:
                if not line.is_idless:
                    continue
                conn.execute(
                    """
                    INSERT INTO task_migration_idless_stage (
                        cohort_id, source_key, task_id, exact_line, line_sha256,
                        fields_json, tags_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        inventory.cohort_id,
                        line.source_key,
                        line.imported_task_id,
                        line.exact_bytes,
                        line.line_sha256,
                        canonical_json(line.stage_fields(cohort_id=inventory.cohort_id, timestamp=now)),
                        canonical_json(list(line.tags)),
                    ),
                )
            database_items = {
                item.task_id: item
                for item in inventory.items
                if item.item_kind == "database_task" and item.task_id is not None
            }
            for line in inventory.task_lines:
                if line.is_idless:
                    continue
                database_item = database_items.get(line.imported_task_id)
                if database_item is None:
                    raise LegacyInventoryError(
                        f"Identified task row disappeared from inventory: {line.imported_task_id}"
                    )
                inventoried = dict(database_item.metadata)
                inventoried_row = dict(inventoried.get("row") or {})
                inventoried_tags = list(inventoried.get("tags") or [])
                current_row_record = conn.execute(
                    "SELECT * FROM task_metadata WHERE task_id=?",
                    (line.imported_task_id,),
                ).fetchone()
                if current_row_record is None:
                    raise CutoverPreconditionError(
                        f"Identified task disappeared before staging: {line.imported_task_id}"
                    )
                current_row = dict(current_row_record)
                if any(current_row.get(key) != value for key, value in inventoried_row.items()):
                    raise CutoverPreconditionError(
                        f"Identified task changed between inventory and staging: {line.imported_task_id}"
                    )
                current_tags = [
                    {"tag": str(row["tag"]), "is_namespace": bool(row["is_namespace"])}
                    for row in conn.execute(
                        "SELECT tag, is_namespace FROM task_tags WHERE task_id=? ORDER BY tag",
                        (line.imported_task_id,),
                    )
                ]
                if current_tags != inventoried_tags:
                    raise CutoverPreconditionError(
                        f"Identified task tags changed between inventory and staging: {line.imported_task_id}"
                    )
                expected_row = {"row": current_row, "tags": current_tags}
                conn.execute(
                    """
                    INSERT INTO task_migration_existing_task_stage (
                        cohort_id, source_key, task_id, expected_row_sha256,
                        fields_json, tags_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        inventory.cohort_id,
                        line.source_key,
                        line.imported_task_id,
                        canonical_sha256(expected_row),
                        canonical_json(
                            {
                                "description": line.description,
                                "note_uuid": line.note_uuid,
                                "due_date": line.due_date,
                                "legacy_import_receipt_id": (
                                    f"legacy-line:{inventory.cohort_id}:{line.source_key}"
                                ),
                            }
                        ),
                        canonical_json(list(line.tags)),
                    ),
                )
            self._receipt(
                conn,
                inventory.cohort_id,
                operation="shadow_begin",
                status="completed",
                payload=inventory.to_dict(),
                actor=actor,
                session_id=session_id,
                now=now,
            )
        result = self.cohort(inventory.cohort_id)
        assert result is not None
        return result

    def record_gate(
        self,
        cohort_id: str,
        gate_name: str,
        *,
        passed: bool,
        evidence: Mapping[str, Any],
        required: bool = True,
    ) -> None:
        if not gate_name or len(gate_name) > 128:
            raise ValueError("gate_name is required")
        encoded = canonical_json(dict(evidence))
        with self.store.transaction() as conn:
            self._require_state(conn, cohort_id, {"shadow", "prepared", "bindings_applying", "bindings_verified"})
            conn.execute(
                """
                INSERT INTO task_migration_gates (
                    cohort_id, gate_name, required, passed, evidence_sha256,
                    evidence_json, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cohort_id, gate_name) DO UPDATE SET
                    required=excluded.required, passed=excluded.passed,
                    evidence_sha256=excluded.evidence_sha256,
                    evidence_json=excluded.evidence_json,
                    checked_at=excluded.checked_at
                """,
                (
                    cohort_id,
                    gate_name,
                    int(required),
                    int(passed),
                    sha256_bytes(encoded.encode("utf-8")),
                    encoded,
                    self.clock(),
                ),
            )
            if gate_name == "frozen_tree_sealed" and passed:
                conn.execute(
                    "UPDATE task_local_file_roots SET status='sealed', updated_at=? "
                    "WHERE manifest_sha256=(SELECT manifest_sha256 "
                    "FROM task_migration_cohorts WHERE cohort_id=?)",
                    (self.clock(), cohort_id),
                )

    def record_document_stage(
        self,
        cohort_id: str,
        *,
        note_uuid: str,
        task_id: str | None,
        store_id: str,
        document_id: str,
        binding_id: str | None,
        source_ref: str,
        source_content_sha256: str,
        normalized_content_sha256: str,
        document_content_sha256: str,
        document_head_sha256: str,
        rewrite_manifest: Sequence[Mapping[str, Any]],
        lifecycle: str,
        classification: str,
        byte_parity: bool,
        normalized_parity: bool,
        source_receipt_id: str,
    ) -> None:
        source_receipt_id = str(source_receipt_id).strip()
        if not source_receipt_id:
            raise ValueError("source_receipt_id is required")
        now = self.clock()
        with self.store.transaction() as conn:
            cohort = self._require_state(conn, cohort_id, {"shadow"})
            existing = conn.execute(
                "SELECT * FROM task_migration_document_stage WHERE cohort_id=? AND note_uuid=?",
                (cohort_id, note_uuid),
            ).fetchone()
            rewrite_manifest_json = canonical_json(list(rewrite_manifest))
            values = (
                cohort_id,
                note_uuid,
                task_id,
                store_id,
                document_id,
                binding_id,
                source_ref,
                source_content_sha256,
                normalized_content_sha256,
                document_content_sha256,
                document_head_sha256,
                rewrite_manifest_json,
                lifecycle,
                classification,
                int(byte_parity),
                int(normalized_parity),
                source_receipt_id,
                now,
            )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO task_migration_document_stage (
                        cohort_id, note_uuid, task_id, store_id, document_id,
                        binding_id, source_ref, source_content_sha256,
                        normalized_content_sha256, document_content_sha256,
                        document_head_sha256, rewrite_manifest_json, lifecycle,
                        classification, byte_parity, normalized_parity,
                        source_receipt_id, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            else:
                comparable = (
                    existing["task_id"], existing["store_id"], existing["document_id"],
                    existing["binding_id"], existing["source_ref"],
                    existing["source_content_sha256"],
                    existing["normalized_content_sha256"],
                    existing["document_content_sha256"],
                    existing["document_head_sha256"],
                    existing["rewrite_manifest_json"], existing["lifecycle"],
                    existing["classification"], bool(existing["byte_parity"]),
                    bool(existing["normalized_parity"]),
                )
                requested = (
                    task_id, store_id, document_id, binding_id, source_ref,
                    source_content_sha256, normalized_content_sha256,
                    document_content_sha256, document_head_sha256,
                    rewrite_manifest_json, lifecycle, classification,
                    bool(byte_parity), bool(normalized_parity),
                )
                if comparable != requested:
                    raise CohortStateError("A staged task document changed on retry.")
                existing_receipt = existing["source_receipt_id"]
                if existing_receipt is None:
                    conn.execute(
                        "UPDATE task_migration_document_stage SET source_receipt_id=? "
                        "WHERE cohort_id=? AND note_uuid=? AND source_receipt_id IS NULL",
                        (source_receipt_id, cohort_id, note_uuid),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0] != 1:
                        raise CohortStateError(
                            "A staged task document receipt changed during backfill."
                        )
                elif str(existing_receipt) != source_receipt_id:
                    raise CohortStateError("A staged task document receipt changed on retry.")
            if classification == "recovered_task_document":
                recovery_id = "recovery_" + hashlib.sha256(
                    f"{cohort_id}\0{note_uuid}".encode("utf-8")
                ).hexdigest()[:32]
                conn.execute(
                    """
                    INSERT INTO recovered_task_documents (
                        recovery_id, note_uuid, store_id, document_id,
                        source_receipt_id, classification, lifecycle, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(note_uuid) DO UPDATE SET
                        store_id=excluded.store_id, document_id=excluded.document_id,
                        source_receipt_id=excluded.source_receipt_id,
                        classification=excluded.classification,
                        lifecycle=excluded.lifecycle
                    """,
                    (
                        recovery_id, note_uuid, store_id, document_id,
                        source_receipt_id, classification, lifecycle, now,
                    ),
                )
            if cohort["cowork_task_store_id"] is None:
                conn.execute(
                    "UPDATE task_migration_cohorts SET cowork_task_store_id=?, updated_at=? "
                    "WHERE cohort_id=?",
                    (store_id, now, cohort_id),
                )
            elif str(cohort["cowork_task_store_id"]) != store_id:
                raise CohortStateError("A cohort cannot span multiple task Co-work stores.")

    def stage_local_file_link(
        self,
        cohort_id: str,
        *,
        link_id: str,
        task_id: str | None,
        note_uuid: str,
        store_id: str,
        document_id: str,
        root_id: str,
        relative_path: str,
        display_name: str,
        suffix: str,
        media_type: str,
        byte_length: int,
        sha256: str,
        sensitivity: str,
        allowed_action: str,
        source_receipt_id: str,
        root_label: str = "Frozen legacy task tree",
        policy_revision: int = 1,
    ) -> None:
        now = self.clock()
        with self.store.transaction() as conn:
            cohort = self._require_state(conn, cohort_id, {"shadow"})
            manifest_sha256 = str(cohort["manifest_sha256"])
            existing_root = conn.execute(
                "SELECT * FROM task_local_file_roots WHERE root_id=?", (root_id,)
            ).fetchone()
            if existing_root is None:
                conn.execute(
                    """
                    INSERT INTO task_local_file_roots (
                        root_id, label, manifest_sha256, policy_revision,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending_seal', ?, ?)
                    """,
                    (
                        root_id, root_label, manifest_sha256,
                        policy_revision, now, now,
                    ),
                )
            elif (
                str(existing_root["label"]) != root_label
                or str(existing_root["manifest_sha256"]) != manifest_sha256
                or int(existing_root["policy_revision"]) != int(policy_revision)
            ):
                raise CohortStateError("A local-file root changed on retry.")

            safe_relative = _safe_relative(relative_path)
            requested = (
                task_id, note_uuid, store_id, document_id, root_id,
                safe_relative, display_name, suffix, media_type, int(byte_length),
                sha256, sensitivity, allowed_action, int(policy_revision),
                source_receipt_id,
            )
            existing = conn.execute(
                "SELECT * FROM task_migration_local_link_stage "
                "WHERE cohort_id=? AND link_id=?",
                (cohort_id, link_id),
            ).fetchone()
            if existing is None:
                try:
                    conn.execute(
                        """
                        INSERT INTO task_migration_local_link_stage (
                            cohort_id, link_id, task_id, note_uuid, store_id,
                            document_id, root_id, relative_path, display_name, suffix,
                            media_type, byte_length, sha256, sensitivity,
                            allowed_action, policy_revision, source_receipt_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (cohort_id, link_id, *requested),
                    )
                except sqlite3.IntegrityError as exc:
                    raise CohortStateError(
                        "A local-file document association changed on retry."
                    ) from exc
            else:
                comparable = (
                    existing["task_id"], existing["note_uuid"], existing["store_id"],
                    existing["document_id"], existing["root_id"],
                    existing["relative_path"], existing["display_name"],
                    existing["suffix"], existing["media_type"],
                    int(existing["byte_length"]), existing["sha256"],
                    existing["sensitivity"], existing["allowed_action"],
                    int(existing["policy_revision"]), existing["source_receipt_id"],
                )
                if comparable != requested:
                    raise CohortStateError("A local-file link changed on retry.")

    def arm_mutation_fence(
        self,
        cohort_id: str,
        *,
        fence_receipt_id: str,
        expected_process_generation: int,
        actor: str,
        session_id: str | None,
    ) -> None:
        now = self.clock()
        with self.store.transaction() as conn:
            cohort = self._require_state(conn, cohort_id, {"shadow", "prepared"})
            system = conn.execute("SELECT * FROM task_system_state WHERE id=1").fetchone()
            assert system is not None
            if str(system["authority_epoch"]) != str(cohort["previous_authority_epoch"]):
                raise CutoverPreconditionError("Task authority changed before the fence was armed.")
            if int(system["process_generation"]) != int(expected_process_generation):
                raise CutoverPreconditionError("The process generation does not match the stop receipt.")
            conn.execute(
                "UPDATE task_system_state SET rollback_fence=1, updated_at=? WHERE id=1",
                (now,),
            )
            conn.execute(
                "UPDATE task_migration_cohorts SET fence_receipt_id=?, "
                "expected_process_generation=?, updated_at=? WHERE cohort_id=?",
                (fence_receipt_id, expected_process_generation, now, cohort_id),
            )
            self._receipt(
                conn,
                cohort_id,
                operation="mutation_fence_arm",
                status="completed",
                payload={
                    "fence_receipt_id": fence_receipt_id,
                    "process_generation": expected_process_generation,
                },
                actor=actor,
                session_id=session_id,
                now=now,
            )

    def prepare(
        self,
        cohort_id: str,
        *,
        inventory_sha256: str,
        target_authority_epoch: str,
        approved_exceptions: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        now = self.clock()
        with self.store.transaction() as conn:
            cohort = self._require_state(conn, cohort_id, {"shadow", "prepared"})
            if cohort["state"] == "prepared":
                if (
                    cohort["inventory_sha256"] != inventory_sha256
                    or cohort["target_authority_epoch"] != target_authority_epoch
                ):
                    raise CohortStateError("Prepared cohort arguments changed on retry.")
                return dict(cohort)
            if str(cohort["inventory_sha256"]) != inventory_sha256:
                raise CutoverPreconditionError("The prepared inventory digest is stale.")
            previous = str(cohort["previous_authority_epoch"])
            if re.fullmatch(r"native:\d+", target_authority_epoch) is None:
                raise CutoverPreconditionError(
                    "Cutover preparation requires a native:<integer> authority epoch."
                )
            if epoch_number(target_authority_epoch) <= epoch_number(previous):
                raise CutoverPreconditionError("The target task authority epoch must be newer.")
            system = conn.execute("SELECT * FROM task_system_state WHERE id=1").fetchone()
            assert system is not None
            if (
                str(system["authority_epoch"]) != previous
                or not bool(system["rollback_fence"])
                or cohort["fence_receipt_id"] is None
                or int(system["process_generation"])
                != int(cohort["expected_process_generation"])
            ):
                raise CutoverPreconditionError("The legacy mutation/process fence is not current.")
            self._require_shadow_complete(conn, cohort_id)
            conn.execute(
                """
                UPDATE task_migration_cohorts
                SET state='prepared', target_authority_epoch=?,
                    approved_exceptions_json=?, updated_at=?
                WHERE cohort_id=?
                """,
                (
                    target_authority_epoch,
                    canonical_json(list(approved_exceptions)),
                    now,
                    cohort_id,
                ),
            )
        result = self.cohort(cohort_id)
        assert result is not None
        return result

    def apply_bindings(
        self,
        cohort_id: str,
        *,
        causality: DocumentCausalityStore,
    ) -> int:
        with self.store.transaction() as conn:
            cohort = self._require_state(conn, cohort_id, {"prepared", "bindings_applying"})
            if cohort["state"] == "prepared":
                conn.execute(
                    "UPDATE task_migration_cohorts SET state='bindings_applying', updated_at=? "
                    "WHERE cohort_id=?",
                    (self.clock(), cohort_id),
                )
            rows = conn.execute(
                "SELECT * FROM task_migration_document_stage WHERE cohort_id=? "
                "AND lifecycle='current' AND binding_id IS NOT NULL ORDER BY binding_id",
                (cohort_id,),
            ).fetchall()
        applied = 0
        for row in rows:
            binding_id = str(row["binding_id"])
            current = causality.get_binding(binding_id)
            if current is None or current.lifecycle != "current":
                raise CutoverPreconditionError(f"Current binding unavailable: {binding_id}")
            if current.projection_mode != "none" or current.projection_path is not None:
                raise CutoverPreconditionError(f"Binding is not projection-free: {binding_id}")
            before_authority = current.content_authority
            before_epoch = current.content_authority_epoch
            updated = causality.cutover_to_cowork(
                binding_id, domain_revision=str(row["source_content_sha256"])
            )
            result = "replayed" if before_authority == "co_work" else "applied"
            with self.store.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO task_migration_binding_transitions (
                        cohort_id, binding_id, direction, before_authority,
                        before_epoch, after_authority, after_epoch,
                        domain_revision, result, applied_at
                    ) VALUES (?, ?, 'to_cowork', ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cohort_id, binding_id, direction) DO UPDATE SET
                        after_authority=excluded.after_authority,
                        after_epoch=excluded.after_epoch, result=excluded.result,
                        applied_at=excluded.applied_at
                    """,
                    (
                        cohort_id,
                        binding_id,
                        before_authority,
                        before_epoch,
                        updated.content_authority,
                        updated.content_authority_epoch,
                        updated.domain_revision,
                        result,
                        self.clock(),
                    ),
                )
            applied += 1
        return applied

    def verify_bindings(
        self,
        cohort_id: str,
        *,
        causality: DocumentCausalityStore,
    ) -> int:
        with self.store.transaction() as conn:
            self._require_state(conn, cohort_id, {"bindings_applying", "bindings_verified"})
            expected_rows = conn.execute(
                "SELECT binding_id, lifecycle, source_content_sha256, store_id, "
                "document_id, document_content_sha256, document_head_sha256 "
                "FROM task_migration_document_stage WHERE cohort_id=? "
                "AND binding_id IS NOT NULL ORDER BY binding_id",
                (cohort_id,),
            ).fetchall()
            all_document_rows = conn.execute(
                "SELECT * FROM task_migration_document_stage WHERE cohort_id=? "
                "ORDER BY note_uuid",
                (cohort_id,),
            ).fetchall()
        truth_store = TruthStore.open(causality.path.parent)
        for row in all_document_rows:
            if str(row["store_id"]) != truth_store.store_id:
                raise CutoverPreconditionError("A staged task document changed stores.")
            document = documents.get_document(truth_store, str(row["document_id"]))
            if (
                document.ydoc_snapshot_sha256 is None
                or document.content_sha256 != str(row["document_content_sha256"])
            ):
                raise CutoverPreconditionError(
                    f"A staged task document changed after parity: {row['note_uuid']}"
                )
            current_head = ydoc_store.current_structured_head(
                truth_store,
                document_id=document.id,
                snapshot_sha256=document.ydoc_snapshot_sha256,
            )
            if current_head != str(row["document_head_sha256"]):
                raise CutoverPreconditionError(
                    f"A staged task document head changed after parity: {row['note_uuid']}"
                )
            document_lifecycle = documents.current_lifecycle(
                truth_store, document.id
            )
            if (
                (row["lifecycle"] == "retired" and document_lifecycle != "retired")
                or (row["lifecycle"] != "retired" and document_lifecycle == "retired")
            ):
                raise CutoverPreconditionError(
                    f"A staged task document lifecycle changed: {row['note_uuid']}"
                )
        expected_current = {
            str(row["binding_id"]): str(row["source_content_sha256"])
            for row in expected_rows
            if row["lifecycle"] == "current"
        }
        expected_retired = {
            str(row["binding_id"])
            for row in expected_rows
            if row["lifecycle"] == "retired"
        }
        actual_migration_current = {
            binding.binding_id
            for binding in causality.list_bindings()
            if binding.domain_namespace == "tasks"
            and binding.domain_kind == "task_knowledge"
            and binding.role == "task_knowledge"
            and binding.migration_origin == "legacy-task-cohort/v1"
        }
        if actual_migration_current != set(expected_current):
            raise CutoverPreconditionError(
                "The current migration binding cohort has missing or extra members.",
                details={
                    "missing": sorted(set(expected_current) - actual_migration_current),
                    "extra": sorted(actual_migration_current - set(expected_current)),
                },
            )
        for binding_id, revision in expected_current.items():
            binding = causality.get_binding(binding_id)
            if (
                binding is None
                or binding.lifecycle != "current"
                or binding.content_authority != "co_work"
                or binding.projection_mode != "none"
                or binding.projection_path is not None
                or binding.domain_revision != revision
            ):
                raise CutoverPreconditionError(f"Binding cohort verification failed: {binding_id}")
        for binding_id in expected_retired:
            binding = causality.get_binding(binding_id)
            if binding is None or binding.lifecycle != "retired":
                raise CutoverPreconditionError(f"Retired binding changed: {binding_id}")
        with self.store.transaction() as conn:
            transition_ids = {
                str(row[0])
                for row in conn.execute(
                    "SELECT binding_id FROM task_migration_binding_transitions "
                    "WHERE cohort_id=? AND direction='to_cowork' AND after_authority='co_work'",
                    (cohort_id,),
                )
            }
            if transition_ids != set(expected_current):
                raise CutoverPreconditionError("The applied binding receipt set is not exact.")
            conn.execute(
                "UPDATE task_migration_cohorts SET state='bindings_verified', updated_at=? "
                "WHERE cohort_id=?",
                (self.clock(), cohort_id),
            )
        self.record_gate(
            cohort_id,
            "binding_cohort_verified",
            passed=True,
            evidence={
                "current_binding_ids": sorted(expected_current),
                "retired_binding_ids": sorted(expected_retired),
            },
        )
        return len(expected_current)

    def activate(
        self,
        cohort_id: str,
        *,
        inventory_sha256: str,
        sealed_tree_manifest_sha256: str,
        actor: str,
        session_id: str | None,
    ) -> dict[str, Any]:
        now = self.clock()
        with self.store.transaction() as conn:
            cohort = self._require_state(conn, cohort_id, {"bindings_verified", "active"})
            if cohort["state"] == "active":
                if (
                    str(cohort["inventory_sha256"]) != inventory_sha256
                    or str(cohort["manifest_sha256"])
                    != sealed_tree_manifest_sha256
                ):
                    raise CohortStateError(
                        "Active cohort replay arguments do not match its cutover receipt."
                    )
                inactive_roots = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM (SELECT DISTINCT s.root_id "
                        "FROM task_migration_local_link_stage s "
                        "LEFT JOIN task_local_file_roots r ON r.root_id=s.root_id "
                        "WHERE s.cohort_id=? AND (r.root_id IS NULL OR r.status!='active'))",
                        (cohort_id,),
                    ).fetchone()[0]
                )
                if inactive_roots:
                    raise CutoverPreconditionError(
                        "An active cohort has an unavailable linked-file root."
                    )
                arm_native_authority_latch(
                    self.store.path,
                    cohort_id=cohort_id,
                    target_authority_epoch=str(cohort["target_authority_epoch"]),
                    cutover_receipt_id=str(cohort["cutover_receipt_id"]),
                    armed_at=str(cohort["activated_at"] or now),
                )
                return dict(cohort)
            if (
                str(cohort["inventory_sha256"]) != inventory_sha256
                or str(cohort["manifest_sha256"]) != sealed_tree_manifest_sha256
            ):
                raise CutoverPreconditionError("The final sealed-tree compare-and-swap failed.")
            self._require_activation_gates(conn, cohort_id)
            system = conn.execute("SELECT * FROM task_system_state WHERE id=1").fetchone()
            assert system is not None
            if (
                str(system["authority_epoch"]) != str(cohort["previous_authority_epoch"])
                or not bool(system["rollback_fence"])
                or int(system["process_generation"])
                != int(cohort["expected_process_generation"])
            ):
                raise CutoverPreconditionError("Task traffic is not fenced at the prepared epoch.")
            receipt_id = "cutover_" + hashlib.sha256(
                f"{cohort_id}\0{inventory_sha256}\0{cohort['target_authority_epoch']}".encode()
            ).hexdigest()[:32]
            self._activate_existing_rows(conn, cohort_id, now)
            self._activate_idless_rows(conn, cohort_id, now)
            self._activate_document_links(conn, cohort_id, now)
            self._activate_local_links(conn, cohort_id, now)
            expected_roots = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT root_id) FROM task_migration_local_link_stage "
                    "WHERE cohort_id=?",
                    (cohort_id,),
                ).fetchone()[0]
            )
            if expected_roots:
                sealed_roots = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM task_local_file_roots WHERE status='sealed' "
                        "AND root_id IN (SELECT DISTINCT root_id "
                        "FROM task_migration_local_link_stage WHERE cohort_id=?)",
                        (cohort_id,),
                    ).fetchone()[0]
                )
                if sealed_roots != expected_roots:
                    raise CutoverPreconditionError(
                        "The sealed linked-file root cohort is not exact."
                    )
                conn.execute(
                    "UPDATE task_local_file_roots SET status='active', updated_at=? "
                    "WHERE status='sealed' AND root_id IN (SELECT DISTINCT root_id "
                    "FROM task_migration_local_link_stage WHERE cohort_id=?)",
                    (now, cohort_id),
                )
                if int(conn.execute("SELECT changes()").fetchone()[0]) != expected_roots:
                    raise CutoverPreconditionError(
                        "Linked-file roots changed during activation."
                    )
            # This filesystem latch is intentionally durable before the
            # SQLite authority compare-and-swap. If the process dies between
            # these operations, routing sees native intent plus a legacy DB
            # and refuses all compatibility writes until activate or abort is
            # replayed. The inverse (native DB with no latch) is impossible
            # through this operator sequence.
            arm_native_authority_latch(
                self.store.path,
                cohort_id=cohort_id,
                target_authority_epoch=str(cohort["target_authority_epoch"]),
                cutover_receipt_id=receipt_id,
                armed_at=now,
            )
            conn.execute(
                """
                UPDATE task_system_state
                SET authority_epoch=?, cowork_task_store_id=?, cutover_receipt_id=?,
                    rollback_fence=0, process_generation=process_generation+1,
                    updated_at=? WHERE id=1 AND authority_epoch=?
                """,
                (
                    cohort["target_authority_epoch"],
                    cohort["cowork_task_store_id"],
                    receipt_id,
                    now,
                    cohort["previous_authority_epoch"],
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise CutoverPreconditionError("Task epoch changed during activation.")
            conn.execute(
                """
                UPDATE task_migration_cohorts
                SET state='active', cutover_receipt_id=?, activated_at=?, updated_at=?
                WHERE cohort_id=?
                """,
                (receipt_id, now, now, cohort_id),
            )
            self._receipt(
                conn,
                cohort_id,
                operation="cohort_activate",
                status="completed",
                payload={
                    "inventory_sha256": inventory_sha256,
                    "manifest_sha256": sealed_tree_manifest_sha256,
                    "previous_epoch": cohort["previous_authority_epoch"],
                    "target_epoch": cohort["target_authority_epoch"],
                },
                actor=actor,
                session_id=session_id,
                now=now,
                receipt_id=receipt_id,
            )
        result = self.cohort(cohort_id)
        assert result is not None
        return result

    def abort_before_activation(
        self,
        cohort_id: str,
        *,
        causality: DocumentCausalityStore,
        actor: str,
        session_id: str | None,
    ) -> dict[str, Any]:
        already_aborted = False
        with self.store.transaction() as conn:
            cohort = self._require_state(
                conn,
                cohort_id,
                {"shadow", "prepared", "bindings_applying", "bindings_verified", "aborted"},
            )
            if cohort["state"] == "aborted":
                already_aborted = True
                rows = []
            else:
                rows = conn.execute(
                    "SELECT binding_id, source_content_sha256 "
                    "FROM task_migration_document_stage "
                    "WHERE cohort_id=? AND lifecycle='current' "
                    "AND binding_id IS NOT NULL",
                    (cohort_id,),
                ).fetchall()
            target_authority_epoch = str(cohort["target_authority_epoch"] or "")
        for row in rows:
            binding_id = str(row["binding_id"])
            binding = causality.get_binding(binding_id)
            if binding is None:
                raise CutoverPreconditionError(f"Cannot abort missing binding: {binding_id}")
            if binding.content_authority == "co_work":
                before_epoch = binding.content_authority_epoch
                rolled = causality.rollback_to_domain(
                    binding_id,
                    domain_revision=str(row["source_content_sha256"]),
                    expected_epoch=before_epoch,
                )
                with self.store.transaction() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO task_migration_binding_transitions (
                            cohort_id, binding_id, direction, before_authority,
                            before_epoch, after_authority, after_epoch,
                            domain_revision, result, applied_at
                        ) VALUES (?, ?, 'abort_to_domain', 'co_work', ?, 'domain', ?, ?, 'applied', ?)
                        """,
                        (
                            cohort_id,
                            binding_id,
                            before_epoch,
                            rolled.content_authority_epoch,
                            rolled.domain_revision,
                            self.clock(),
                        ),
                    )
            refreshed = causality.get_binding(binding_id)
            if refreshed is None or refreshed.content_authority != "domain":
                raise CutoverPreconditionError(f"Abort binding verification failed: {binding_id}")
        if not already_aborted:
            now = self.clock()
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE task_system_state SET rollback_fence=0, updated_at=? "
                    "WHERE id=1",
                    (now,),
                )
                conn.execute(
                    "UPDATE task_migration_cohorts "
                    "SET state='aborted', aborted_at=?, updated_at=? "
                    "WHERE cohort_id=?",
                    (now, now, cohort_id),
                )
                conn.execute(
                    "UPDATE task_local_file_roots SET status='aborted', updated_at=? "
                    "WHERE status IN ('pending_seal','sealed') AND root_id IN "
                    "(SELECT DISTINCT root_id FROM task_migration_local_link_stage "
                    "WHERE cohort_id=?)",
                    (now, cohort_id),
                )
                self._receipt(
                    conn,
                    cohort_id,
                    operation="cohort_abort_before_activation",
                    status="completed",
                    payload={"bindings": len(rows)},
                    actor=actor,
                    session_id=session_id,
                    now=now,
                )
        # Cleanup follows the durable SQLite abort. A crash before unlinking
        # leaves the system unavailable (safe); replaying abort finishes it.
        if target_authority_epoch:
            clear_pending_authority_latch(
                self.store.path,
                cohort_id=cohort_id,
                target_authority_epoch=target_authority_epoch,
            )
        result = self.cohort(cohort_id)
        assert result is not None
        return result

    def prepare_rollback(
        self,
        cohort_id: str,
        *,
        rollback_authority_epoch: str,
        reverse_export_receipt: Mapping[str, Any],
        actor: str,
        session_id: str | None,
    ) -> dict[str, Any]:
        now = self.clock()
        with self.store.transaction() as conn:
            cohort = self._require_state(conn, cohort_id, {"active", "rollback_prepared"})
            if cohort["state"] == "rollback_prepared":
                if cohort["rollback_authority_epoch"] != rollback_authority_epoch:
                    raise CohortStateError("Rollback epoch changed on retry.")
                return dict(cohort)
            current_epoch = str(cohort["target_authority_epoch"])
            if re.fullmatch(r"rollback:\d+", rollback_authority_epoch) is None:
                raise CutoverPreconditionError(
                    "Rollback preparation requires a rollback:<integer> authority epoch."
                )
            if epoch_number(rollback_authority_epoch) <= epoch_number(current_epoch):
                raise CutoverPreconditionError("Rollback must use a strictly newer task epoch.")
            system = conn.execute("SELECT * FROM task_system_state WHERE id=1").fetchone()
            assert system is not None
            if str(system["authority_epoch"]) != current_epoch:
                raise CutoverPreconditionError("The active task epoch no longer matches the cohort.")
            schema = reverse_export_receipt.get("legacy_database_schema_version")
            if schema != 11 or not reverse_export_receipt.get("staged_tree_sha256"):
                raise CutoverPreconditionError(
                    "Rollback preparation needs a staged tree and legacy-compatible v11 database receipt."
                )
            conn.execute(
                "UPDATE task_system_state SET rollback_fence=1, updated_at=? WHERE id=1",
                (now,),
            )
            conn.execute(
                "UPDATE task_migration_cohorts SET state='rollback_prepared', "
                "rollback_authority_epoch=?, updated_at=? WHERE cohort_id=?",
                (rollback_authority_epoch, now, cohort_id),
            )
            self._receipt(
                conn,
                cohort_id,
                operation="rollback_prepare",
                status="completed",
                payload={
                    "active_epoch": current_epoch,
                    "rollback_epoch": rollback_authority_epoch,
                    "reverse_export_receipt": dict(reverse_export_receipt),
                },
                actor=actor,
                session_id=session_id,
                now=now,
            )
        result = self.cohort(cohort_id)
        assert result is not None
        return result

    @staticmethod
    def _require_state(
        conn: sqlite3.Connection,
        cohort_id: str,
        allowed: set[str],
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM task_migration_cohorts WHERE cohort_id=?", (cohort_id,)
        ).fetchone()
        if row is None:
            raise CohortStateError("The migration cohort does not exist.")
        if str(row["state"]) not in allowed:
            raise CohortStateError(
                f"Cohort state {row['state']!r} is not valid for this operation."
            )
        return row

    @staticmethod
    def _require_shadow_complete(conn: sqlite3.Connection, cohort_id: str) -> None:
        expected = int(
            conn.execute(
                "SELECT COUNT(*) FROM task_migration_inventory WHERE cohort_id=? "
                "AND classification IN ('task_note_live','task_note_live_db_only',"
                "'task_note_idless','task_note_deleted','recovered_task_document')",
                (cohort_id,),
            ).fetchone()[0]
        )
        imported = int(
            conn.execute(
                "SELECT COUNT(*) FROM task_migration_document_stage WHERE cohort_id=? "
                "AND byte_parity=1 AND normalized_parity=1 "
                "AND source_receipt_id IS NOT NULL",
                (cohort_id,),
            ).fetchone()[0]
        )
        if expected != imported:
            raise CutoverPreconditionError(
                f"Shadow document cohort is incomplete ({imported}/{expected})."
            )
        unclassified = int(
            conn.execute(
                "SELECT COUNT(*) FROM task_migration_inventory WHERE cohort_id=? "
                "AND classification IN ('unclassified','missing_live_note')",
                (cohort_id,),
            ).fetchone()[0]
        )
        if unclassified:
            raise CutoverPreconditionError("The inventory still contains blocking classifications.")

    @staticmethod
    def _require_activation_gates(conn: sqlite3.Connection, cohort_id: str) -> None:
        rows = {
            str(row["gate_name"]): bool(row["passed"])
            for row in conn.execute(
                "SELECT gate_name, passed FROM task_migration_gates "
                "WHERE cohort_id=? AND required=1",
                (cohort_id,),
            )
        }
        missing = sorted(REQUIRED_ACTIVATION_GATES - rows.keys())
        failed = sorted(name for name, passed in rows.items() if not passed)
        if missing or failed:
            raise CutoverPreconditionError(
                "Mandatory activation gates are not satisfied.",
                details={"missing": missing, "failed": failed},
            )

    @staticmethod
    def _activate_existing_rows(conn: sqlite3.Connection, cohort_id: str, now: str) -> None:
        rows = conn.execute(
            "SELECT * FROM task_migration_existing_task_stage WHERE cohort_id=? "
            "ORDER BY source_key",
            (cohort_id,),
        ).fetchall()
        for staged in rows:
            current = conn.execute(
                "SELECT * FROM task_metadata WHERE task_id=?",
                (staged["task_id"],),
            ).fetchone()
            if current is None:
                raise CutoverPreconditionError(
                    f"An identified task disappeared before activation: {staged['task_id']}"
                )
            current_tags = [
                {"tag": str(row["tag"]), "is_namespace": bool(row["is_namespace"])}
                for row in conn.execute(
                    "SELECT tag, is_namespace FROM task_tags WHERE task_id=? ORDER BY tag",
                    (staged["task_id"],),
                )
            ]
            current_digest = canonical_sha256(
                {"row": dict(current), "tags": current_tags}
            )
            if current_digest != staged["expected_row_sha256"]:
                raise CutoverPreconditionError(
                    f"An identified task changed after inventory: {staged['task_id']}"
                )
            fields = json.loads(staged["fields_json"])
            tags = json.loads(staged["tags_json"])
            next_revision = int(current["revision"]) + 1
            conn.execute(
                """
                UPDATE task_metadata
                SET description=?, note_uuid=?, due_date=?,
                    legacy_import_receipt_id=?, revision=?, updated_at=?,
                    last_actor='system'
                WHERE task_id=? AND revision=?
                """,
                (
                    fields["description"],
                    fields.get("note_uuid"),
                    fields.get("due_date"),
                    fields["legacy_import_receipt_id"],
                    next_revision,
                    now,
                    staged["task_id"],
                    current["revision"],
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise CutoverPreconditionError(
                    f"Identified task CAS failed: {staged['task_id']}"
                )
            conn.execute("DELETE FROM task_tags WHERE task_id=?", (staged["task_id"],))
            for tag in tags:
                conn.execute(
                    "INSERT INTO task_tags(task_id, tag, is_namespace) VALUES (?, ?, ?)",
                    (staged["task_id"], tag, int(tag.startswith("projects/") or "/" in tag)),
                )
            collection_revision = TaskMigrationLedger._next_collection_revision(
                conn, now
            )
            TaskMigrationLedger._append_activation_event(
                conn,
                cohort_id=cohort_id,
                task_id=str(staged["task_id"]),
                state=str(current["state"]),
                task_revision=next_revision,
                collection_revision=collection_revision,
                now=now,
                detail={"source_key": staged["source_key"], "existing_row": True},
            )
            conn.execute(
                "UPDATE task_migration_existing_task_stage SET activated_at=? "
                "WHERE cohort_id=? AND task_id=?",
                (now, cohort_id, staged["task_id"]),
            )

    @staticmethod
    def _activate_idless_rows(conn: sqlite3.Connection, cohort_id: str, now: str) -> None:
        rows = conn.execute(
            "SELECT * FROM task_migration_idless_stage WHERE cohort_id=? ORDER BY source_key",
            (cohort_id,),
        ).fetchall()
        for row in rows:
            fields = json.loads(row["fields_json"])
            tags = json.loads(row["tags_json"])
            existing = conn.execute(
                "SELECT legacy_import_receipt_id FROM task_metadata WHERE task_id=?",
                (row["task_id"],),
            ).fetchone()
            if existing is not None:
                if existing[0] != fields["legacy_import_receipt_id"]:
                    raise CutoverPreconditionError(f"Staged task ID collision: {row['task_id']}")
            else:
                conn.execute(
                    """
                    INSERT INTO task_metadata (
                        task_id, state, urgency, note_uuid, created_at, updated_at,
                        completed_at, archived_at, description, due_date, revision,
                        task_kind, density, creation_effort, user_involvement,
                        creation_provenance, has_deadline, has_dependency,
                        legacy_import_receipt_id, last_actor
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'task', 'sparse',
                              'developed', 'high', ?, 0, 0, ?, 'system')
                    """,
                    (
                        row["task_id"],
                        fields["state"],
                        fields["urgency"],
                        fields.get("note_uuid"),
                        fields["created_at"],
                        fields["updated_at"],
                        fields.get("completed_at"),
                        fields.get("archived_at"),
                        fields["description"],
                        fields.get("due_date"),
                        fields["creation_provenance"],
                        fields["legacy_import_receipt_id"],
                    ),
                )
            conn.execute("DELETE FROM task_tags WHERE task_id=?", (row["task_id"],))
            for tag in tags:
                conn.execute(
                    "INSERT INTO task_tags(task_id, tag, is_namespace) VALUES (?, ?, ?)",
                    (row["task_id"], tag, int(tag.startswith("projects/") or "/" in tag)),
                )
            collection_revision = TaskMigrationLedger._next_collection_revision(
                conn, now
            )
            TaskMigrationLedger._append_activation_event(
                conn,
                cohort_id=cohort_id,
                task_id=str(row["task_id"]),
                state=str(fields["state"]),
                task_revision=1,
                collection_revision=collection_revision,
                now=now,
                detail={"source_key": row["source_key"], "idless_recovery": True},
            )
            conn.execute(
                "UPDATE task_migration_idless_stage SET activated_at=? "
                "WHERE cohort_id=? AND source_key=?",
                (now, cohort_id, row["source_key"]),
            )

    @staticmethod
    def _next_collection_revision(conn: sqlite3.Connection, now: str) -> int:
        current = int(
            conn.execute("SELECT revision FROM task_collection_state WHERE id=1").fetchone()[0]
        )
        next_revision = current + 1
        conn.execute(
            "UPDATE task_collection_state SET revision=?, updated_at=? WHERE id=1",
            (next_revision, now),
        )
        return next_revision

    @staticmethod
    def _append_activation_event(
        conn: sqlite3.Connection,
        *,
        cohort_id: str,
        task_id: str,
        state: str,
        task_revision: int,
        collection_revision: int,
        now: str,
        detail: Mapping[str, Any],
    ) -> None:
        details = {"cohort_id": cohort_id, **dict(detail)}
        conn.execute(
            """
            INSERT INTO task_state_history (
                task_id, old_state, new_state, changed_at, reason, mutation,
                actor, task_revision, collection_revision, details_json
            ) VALUES (?, ?, ?, ?, 'legacy_cohort_activation',
                      'legacy_import_activate', 'system:migration', ?, ?, ?)
            """,
            (
                task_id,
                None if detail.get("idless_recovery") else state,
                state,
                now,
                task_revision,
                collection_revision,
                canonical_json(details),
            ),
        )
        event_id = "te_mig_" + hashlib.sha256(
            f"{cohort_id}\0{task_id}\0{collection_revision}".encode("utf-8")
        ).hexdigest()[:32]
        payload = {
            "schema": "wb.task-event/v1",
            "task_id": task_id,
            "mutation": "legacy_import_activate",
            "task_revision": task_revision,
            "collection_revision": collection_revision,
            "cohort_id": cohort_id,
        }
        conn.execute(
            """
            INSERT INTO task_event_outbox (
                event_id, task_id, mutation, task_revision,
                collection_revision, payload_json, created_at
            ) VALUES (?, ?, 'legacy_import_activate', ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                task_revision,
                collection_revision,
                canonical_json(payload),
                now,
            ),
        )

    @staticmethod
    def _activate_document_links(conn: sqlite3.Connection, cohort_id: str, now: str) -> None:
        rows = conn.execute(
            "SELECT * FROM task_migration_document_stage WHERE cohort_id=? "
            "AND task_id IS NOT NULL ORDER BY task_id",
            (cohort_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO task_document_links (
                    task_id, note_uuid, store_id, document_id, binding_id,
                    lifecycle, created_at, updated_at, retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    note_uuid=excluded.note_uuid, store_id=excluded.store_id,
                    document_id=excluded.document_id, binding_id=excluded.binding_id,
                    lifecycle=excluded.lifecycle, updated_at=excluded.updated_at,
                    retired_at=excluded.retired_at
                """,
                (
                    row["task_id"],
                    row["note_uuid"],
                    row["store_id"],
                    row["document_id"],
                    row["binding_id"],
                    row["lifecycle"],
                    row["imported_at"],
                    now,
                    now if row["lifecycle"] == "retired" else None,
                ),
            )
            conn.execute(
                "UPDATE task_migration_document_stage SET activated_at=? "
                "WHERE cohort_id=? AND note_uuid=?",
                (now, cohort_id, row["note_uuid"]),
            )

    @staticmethod
    def _activate_local_links(conn: sqlite3.Connection, cohort_id: str, now: str) -> None:
        rows = conn.execute(
            "SELECT * FROM task_migration_local_link_stage WHERE cohort_id=? ORDER BY link_id",
            (cohort_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO task_local_file_links (
                    link_id, task_id, store_id, document_id, root_id,
                    relative_path, display_name, suffix, media_type, byte_length,
                    sha256, sensitivity, allowed_action, policy_revision,
                    source_receipt_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(link_id) DO NOTHING
                """,
                (
                    row["link_id"],
                    row["task_id"],
                    row["store_id"],
                    row["document_id"],
                    row["root_id"],
                    row["relative_path"],
                    row["display_name"],
                    row["suffix"],
                    row["media_type"],
                    row["byte_length"],
                    row["sha256"],
                    row["sensitivity"],
                    row["allowed_action"],
                    row["policy_revision"],
                    row["source_receipt_id"],
                    now,
                ),
            )
            columns = (
                "link_id",
                "task_id",
                "store_id",
                "document_id",
                "root_id",
                "relative_path",
                "display_name",
                "suffix",
                "media_type",
                "byte_length",
                "sha256",
                "sensitivity",
                "allowed_action",
                "policy_revision",
                "source_receipt_id",
            )
            existing = conn.execute(
                "SELECT " + ",".join(columns) + " FROM task_local_file_links "
                "WHERE link_id=?",
                (row["link_id"],),
            ).fetchone()
            if existing is None or tuple(existing[column] for column in columns) != tuple(
                row[column] for column in columns
            ):
                raise CutoverPreconditionError(
                    f"A local-file link collides with different catalog data: {row['link_id']}"
                )
            conn.execute(
                "UPDATE task_migration_local_link_stage SET activated_at=? "
                "WHERE cohort_id=? AND link_id=?",
                (now, cohort_id, row["link_id"]),
            )

    @staticmethod
    def _receipt(
        conn: sqlite3.Connection,
        cohort_id: str,
        *,
        operation: str,
        status: str,
        payload: Mapping[str, Any],
        actor: str,
        session_id: str | None,
        now: str,
        receipt_id: str | None = None,
    ) -> str:
        encoded = canonical_json(dict(payload))
        chosen = receipt_id or "tmig_" + hashlib.sha256(
            f"{cohort_id}\0{operation}\0{encoded}".encode("utf-8")
        ).hexdigest()[:32]
        conn.execute(
            """
            INSERT OR IGNORE INTO task_migration_receipts (
                receipt_id, cohort_id, operation, status, payload_sha256,
                payload_json, actor, session_id, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chosen,
                cohort_id,
                operation,
                status,
                sha256_bytes(encoded.encode("utf-8")),
                encoded,
                actor,
                session_id,
                now,
                now if status == "completed" else None,
            ),
        )
        return chosen


__all__ = [
    "CohortStateError",
    "CutoverPreconditionError",
    "InventoryItem",
    "LegacyInventory",
    "LegacyInventoryError",
    "LegacyManifestEntry",
    "LegacyMigrationError",
    "LegacyTaskInventoryBuilder",
    "MIGRATION_SCHEMA_VERSION",
    "ParsedLegacyTaskLine",
    "REQUIRED_ACTIVATION_GATES",
    "RETENTION_POLICY",
    "TaskMigrationLedger",
    "canonical_json",
    "canonical_sha256",
    "deterministic_import_task_id",
    "epoch_number",
    "normalized_markdown_sha256",
]
