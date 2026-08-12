"""Single production seam for task-note body reads and mutations."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from work_buddy.paths import resolve
from work_buddy.task_notes.models import AuthorityState, TaskNoteDescriptor
from work_buddy.task_notes.store import TaskNoteMigrationStore


_NOTE_UUID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class TaskNoteContentError(RuntimeError):
    code = "task_note_content_error"


class TaskNoteContentConflict(TaskNoteContentError):
    code = "task_note_content_conflict"


class CoworkTaskNotePort(Protocol):
    def read(self, note_uuid: str) -> str | None: ...

    def replace(
        self,
        note_uuid: str,
        content: str,
        *,
        idempotency_key: str,
    ) -> None: ...

    def retire(self, note_uuid: str, *, idempotency_key: str) -> None: ...

    def modified_at(self, note_uuid: str) -> float: ...


@dataclass(frozen=True, slots=True)
class TaskNoteMutationResult:
    note_uuid: str
    changed: bool
    existed: bool
    saga_id: str | None


def validate_note_uuid(note_uuid: str) -> str:
    if _NOTE_UUID.fullmatch(note_uuid) is None:
        raise ValueError("invalid task note UUID")
    return note_uuid


def note_uuid_from_path(note_path: str) -> str:
    normalized = note_path.replace("\\", "/")
    match = re.fullmatch(r"tasks/notes/([^/]+)\.md", normalized)
    if match is None:
        raise ValueError("not a task-note path")
    return validate_note_uuid(match.group(1))


class TaskNoteContentAdapter:
    """Authority-aware task-note body adapter.

    With no migration coordinator this is a compatibility-only legacy seam.
    That is the shipped default.  A configured coordinator may shadow/read a
    bound Co-work document, but writes cannot switch until the coordinator has
    independently admitted that note to a Co-work authority epoch.
    """

    def __init__(
        self,
        *,
        vault_root: str | Path,
        bridge_client: Any | None = None,
        migration_store: TaskNoteMigrationStore | None = None,
        cowork: CoworkTaskNotePort | None = None,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.bridge = bridge_client
        self.migrations = migration_store
        self.cowork = cowork

    @staticmethod
    def relative_path(note_uuid: str) -> str:
        return f"tasks/notes/{validate_note_uuid(note_uuid)}.md"

    def absolute_path(self, note_uuid: str) -> Path:
        path = (self.vault_root / self.relative_path(note_uuid)).resolve()
        try:
            path.relative_to(self.vault_root)
        except ValueError as exc:  # pragma: no cover - UUID validation fences this
            raise TaskNoteContentError("task-note path escaped the vault") from exc
        return path

    def authority(self, note_uuid: str) -> AuthorityState:
        validate_note_uuid(note_uuid)
        if self.migrations is None:
            return AuthorityState.LEGACY
        epoch = self.migrations.get_authority("tasks", "task_note", note_uuid)
        return AuthorityState.LEGACY if epoch is None else epoch.state

    def read(
        self,
        note_uuid: str,
        *,
        filesystem_fallback: bool = True,
        strict_bridge: bool = False,
    ) -> str | None:
        authority = self.authority(note_uuid)
        if authority is AuthorityState.COWORK:
            if self.cowork is None:
                raise TaskNoteContentError("Co-work task-note content port is unavailable")
            return self.cowork.read(note_uuid)
        if authority is AuthorityState.RETIRED:
            return None
        return self._read_legacy(
            note_uuid,
            filesystem_fallback=filesystem_fallback,
            strict_bridge=strict_bridge,
        )

    def _read_legacy(
        self,
        note_uuid: str,
        *,
        filesystem_fallback: bool,
        strict_bridge: bool = False,
    ) -> str | None:
        relative = self.relative_path(note_uuid)
        if self.bridge is not None:
            # Typed bridge errors intentionally propagate.  A genuine absence
            # returns None and may use the explicit filesystem compatibility
            # fallback when requested by the caller.
            reader = (
                self.bridge.read_file_raw
                if strict_bridge
                else self.bridge.read_file
            )
            content = reader(relative)
            if content is not None:
                return content
        if not filesystem_fallback:
            return None
        path = self.absolute_path(note_uuid)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise TaskNoteContentError("task-note Markdown could not be read") from exc

    def create(
        self,
        note_uuid: str,
        content: str,
        *,
        idempotency_key: str,
        task_id: str | None = None,
    ) -> TaskNoteMutationResult:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        saga = None
        if self.migrations is not None:
            saga = self.migrations.begin_saga(
                operation="create",
                idempotency_key=f"task-note-create:{idempotency_key}",
                request_sha256=digest,
                note_uuid=validate_note_uuid(note_uuid),
                task_id=task_id,
                required_steps=("note", "master", "metadata"),
            )
        existing = self.read(
            note_uuid,
            filesystem_fallback=self.bridge is None,
        )
        if existing is not None:
            if existing != content:
                if saga is not None:
                    self.migrations.fail_saga(saga.saga_id, error_code="note_content_conflict")
                raise TaskNoteContentConflict("task note already exists with other content")
            if saga is not None:
                self.migrations.complete_saga_step(saga.saga_id, "note")
            return TaskNoteMutationResult(note_uuid, False, True, None if saga is None else saga.saga_id)
        try:
            self._replace(note_uuid, content, write_mode="create")
        except Exception:
            if saga is not None:
                self.migrations.fail_saga(saga.saga_id, error_code="note_write_failed")
            raise
        if saga is not None:
            self.migrations.complete_saga_step(saga.saga_id, "note")
        return TaskNoteMutationResult(note_uuid, True, False, None if saga is None else saga.saga_id)

    def replace(
        self,
        note_uuid: str,
        content: str,
        *,
        idempotency_key: str,
    ) -> TaskNoteMutationResult:
        authority = self.authority(note_uuid)
        if authority is AuthorityState.COWORK:
            if self.cowork is None:
                raise TaskNoteContentError("Co-work task-note content port is unavailable")
            self.cowork.replace(note_uuid, content, idempotency_key=idempotency_key)
            return TaskNoteMutationResult(note_uuid, True, True, None)
        if authority is AuthorityState.RETIRED:
            raise TaskNoteContentConflict("retired task note cannot be changed")
        prior = self._read_legacy(note_uuid, filesystem_fallback=True)
        if prior == content:
            return TaskNoteMutationResult(note_uuid, False, True, None)
        self._replace(note_uuid, content, write_mode="replace")
        return TaskNoteMutationResult(note_uuid, True, prior is not None, None)

    def append(
        self,
        note_uuid: str,
        content: str,
        *,
        idempotency_key: str,
    ) -> TaskNoteMutationResult:
        existing = self.read(note_uuid, filesystem_fallback=True)
        if existing is None:
            raise TaskNoteContentError("task note does not exist")
        addition = content.rstrip("\n")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        updated = existing + "\n" + addition + "\n"
        return self.replace(note_uuid, updated, idempotency_key=idempotency_key)

    def delete(
        self,
        note_uuid: str,
        *,
        idempotency_key: str,
        task_id: str | None = None,
    ) -> TaskNoteMutationResult:
        note_uuid = validate_note_uuid(note_uuid)
        digest = hashlib.sha256(
            f"delete\0{note_uuid}\0{task_id or ''}".encode()
        ).hexdigest()
        saga = None
        if self.migrations is not None:
            saga = self.migrations.begin_saga(
                operation="delete",
                idempotency_key=f"task-note-delete:{idempotency_key}",
                request_sha256=digest,
                note_uuid=note_uuid,
                task_id=task_id,
                required_steps=("master", "note", "binding", "metadata"),
            )
        authority = self.authority(note_uuid)
        existed = self.read(
            note_uuid,
            filesystem_fallback=self.bridge is None,
        ) is not None
        try:
            if authority is AuthorityState.COWORK:
                if self.cowork is None:
                    raise TaskNoteContentError("Co-work task-note content port is unavailable")
                self.cowork.retire(note_uuid, idempotency_key=idempotency_key)
            elif authority is not AuthorityState.RETIRED:
                self._delete_legacy(note_uuid)
        except Exception:
            if saga is not None:
                self.migrations.fail_saga(saga.saga_id, error_code="note_retire_failed")
            raise
        if self.migrations is not None:
            self.migrations.retire("tasks", "task_note", note_uuid)
            assert saga is not None
            self.migrations.complete_saga_step(saga.saga_id, "note")
            self.migrations.complete_saga_step(saga.saga_id, "binding")
        return TaskNoteMutationResult(note_uuid, existed, existed, None if saga is None else saga.saga_id)

    def mark_saga_step(self, saga_id: str | None, step: str) -> None:
        if saga_id is not None and self.migrations is not None:
            self.migrations.complete_saga_step(saga_id, step)

    def _replace(self, note_uuid: str, content: str, *, write_mode: str) -> None:
        relative = self.relative_path(note_uuid)
        if self.bridge is not None:
            self.bridge.write_file(
                relative,
                content,
                write_mode=write_mode,
                content_hint=content[:256] if content else None,
            )
            return
        path = self.absolute_path(note_uuid)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.wbtmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise TaskNoteContentError("task-note Markdown write failed") from exc

    def _delete_legacy(self, note_uuid: str) -> None:
        relative = self.relative_path(note_uuid)
        if self.bridge is not None:
            # The task-delete operation already owns the user consent boundary.
            js_path = relative.replace('"', '\\"')
            result = self.bridge.eval_js_internal(
                f'const f = app.vault.getAbstractFileByPath("{js_path}");'
                'if (f) { await app.vault.delete(f); return "deleted"; } '
                'else { return "not_found"; }'
            )
            if result not in {"deleted", "not_found"}:
                raise TaskNoteContentError("task-note deletion was not verified")
            return
        try:
            self.absolute_path(note_uuid).unlink(missing_ok=True)
        except OSError as exc:
            raise TaskNoteContentError("task-note deletion failed") from exc

    def discover(self, note_uuids: list[str]) -> list[TaskNoteDescriptor]:
        descriptors: list[TaskNoteDescriptor] = []
        for note_uuid in note_uuids:
            note_uuid = validate_note_uuid(note_uuid)
            authority = self.authority(note_uuid)
            if authority is AuthorityState.RETIRED:
                continue
            item_id = str(self.absolute_path(note_uuid))
            if authority is AuthorityState.COWORK:
                if self.cowork is None or self.cowork.read(note_uuid) is None:
                    continue
                modified = self.cowork.modified_at(note_uuid)
            else:
                try:
                    modified = self.absolute_path(note_uuid).stat().st_mtime
                except OSError:
                    continue
            descriptors.append(TaskNoteDescriptor(note_uuid, item_id, modified))
        return descriptors


def migration_feature_enabled(config: dict[str, Any] | None = None) -> bool:
    if config is None:
        from work_buddy import config as config_module

        cfg = config_module.load_config()
    else:
        cfg = config
    value = cfg.get("task_note_migration", {})
    return isinstance(value, dict) and value.get("enabled") is True


def get_task_note_adapter(
    *,
    vault_root: str | Path | None = None,
    bridge_client: Any | None = None,
    migration_store: TaskNoteMigrationStore | None = None,
    cowork: CoworkTaskNotePort | None = None,
) -> TaskNoteContentAdapter:
    from work_buddy import config as config_module

    cfg = config_module.load_config() or {}
    root = Path(vault_root or cfg.get("vault_root") or ".")
    coordinator = migration_store
    if coordinator is None and migration_feature_enabled(cfg):
        coordinator = TaskNoteMigrationStore(resolve("db/task-note-migration"))
    if coordinator is not None and cowork is None:
        # Local import avoids an adapter↔migration import cycle while making
        # feature-enabled Co-work-authoritative reads operational by default.
        from work_buddy.task_notes.migration import BoundTaskNoteReader

        cowork = BoundTaskNoteReader(
            vault_root=root,
            migration_store=coordinator,
        )
    return TaskNoteContentAdapter(
        vault_root=root,
        bridge_client=bridge_client,
        migration_store=coordinator,
        cowork=cowork,
    )


__all__ = [
    "CoworkTaskNotePort",
    "TaskNoteContentAdapter",
    "TaskNoteContentConflict",
    "TaskNoteContentError",
    "TaskNoteMutationResult",
    "get_task_note_adapter",
    "migration_feature_enabled",
    "note_uuid_from_path",
    "validate_note_uuid",
]
