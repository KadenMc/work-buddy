"""Dataclasses for the inline-command subsystem.

Mirrors the conventions used in ``work_buddy.threads.models``:

- ``@dataclass`` with defaults for every field
- ``to_dict()`` for JSON-safe serialisation (handlers/callables are excluded)
- ``from_row()`` on persisted models to rehydrate from a SQLite row
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Runtime context delivered to handlers
# ---------------------------------------------------------------------------


@dataclass
class InlineContext:
    """Execution context handed to an inline command handler.

    Attributes:
        surface: ``"menu"`` or ``"tag"``.
        file_path: Vault-relative path of the active file, if any.
        selection: User text selection (menu surface only).
        line_text: The single line containing the trigger / cursor.
        cursor_line: 0-indexed line number of the cursor (or tag line).
        cursor_ch: 0-indexed column of the cursor (menu surface only).
        paragraph: Blank-line-bounded block surrounding the cursor.
        section: Heading-bounded block surrounding the cursor.
        full_text: Entire file text (loaded on demand by the dispatcher).
        tag: ``{"name": str, "line": int}`` for ``tag`` surface; else ``None``.
        thread_id: Optional interactive thread this invocation opened.
    """

    surface: str = ""
    file_path: str | None = None
    selection: str = ""
    line_text: str = ""
    cursor_line: int | None = None
    cursor_ch: int | None = None
    paragraph: str = ""
    section: str = ""
    full_text: str = ""
    tag: dict[str, Any] | None = None
    thread_id: str | None = None

    def text_for_llm(self) -> str:
        """Return the richest non-empty text candidate for LLM input."""
        for candidate in (self.selection, self.paragraph, self.line_text):
            if candidate and candidate.strip():
                return candidate
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "file_path": self.file_path,
            "selection": self.selection,
            "line_text": self.line_text,
            "cursor_line": self.cursor_line,
            "cursor_ch": self.cursor_ch,
            "paragraph": self.paragraph,
            "section": self.section,
            "full_text": self.full_text,
            "tag": self.tag,
            "thread_id": self.thread_id,
        }


# ---------------------------------------------------------------------------
# Command definition (registry entry)
# ---------------------------------------------------------------------------


@dataclass
class InlineCommand:
    """Declarative registration for a handler.

    ``handler`` is excluded from :meth:`to_dict` since it is not JSON-safe.
    """

    name: str = ""
    description: str = ""
    surfaces: list[str] = field(default_factory=list)
    consume_mode: str = "leave"
    persistent: bool = False
    menu_label: str | None = None
    interactive: bool = False
    context_scope: str = "line"
    handler: Callable | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "surfaces": list(self.surfaces),
            "consume_mode": self.consume_mode,
            "persistent": self.persistent,
            "menu_label": self.menu_label,
            "interactive": self.interactive,
            "context_scope": self.context_scope,
        }


# ---------------------------------------------------------------------------
# Persisted records
# ---------------------------------------------------------------------------


@dataclass
class InlineInvocation:
    """An invocation history row — stored for audit / replay / UI."""

    invocation_id: str = ""
    command_name: str = ""
    surface: str = ""
    context: dict = field(default_factory=dict)
    status: str = "pending"
    result: dict | None = None
    created_at: str = ""
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "command_name": self.command_name,
            "surface": self.surface,
            "context": self.context,
            "status": self.status,
            "result": self.result,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_row(cls, row: dict) -> InlineInvocation:
        ctx = row.get("context")
        if isinstance(ctx, str) and ctx:
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}
        elif not ctx:
            ctx = {}
        result = row.get("result")
        if isinstance(result, str) and result:
            try:
                result = json.loads(result)
            except Exception:
                result = None
        return cls(
            invocation_id=row["invocation_id"],
            command_name=row["command_name"],
            surface=row["surface"],
            context=ctx,
            status=row.get("status", "pending"),
            result=result,
            created_at=row["created_at"],
            completed_at=row.get("completed_at"),
        )


@dataclass
class PersistentWatcher:
    """A persistent ``#wb/cmd/*`` tag that runs on a schedule."""

    watcher_id: str = ""
    command_name: str = ""
    file_path: str = ""
    tag: str = ""
    tag_line: int | None = None
    params: dict = field(default_factory=dict)
    created_at: str = ""
    last_run_at: str | None = None
    schedule: str | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "watcher_id": self.watcher_id,
            "command_name": self.command_name,
            "file_path": self.file_path,
            "tag": self.tag,
            "tag_line": self.tag_line,
            "params": self.params,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "schedule": self.schedule,
            "enabled": self.enabled,
        }

    @classmethod
    def from_row(cls, row: dict) -> PersistentWatcher:
        params = row.get("params")
        if isinstance(params, str) and params:
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        elif not params:
            params = {}
        return cls(
            watcher_id=row["watcher_id"],
            command_name=row["command_name"],
            file_path=row["file_path"],
            tag=row["tag"],
            tag_line=row.get("tag_line"),
            params=params,
            created_at=row["created_at"],
            last_run_at=row.get("last_run_at"),
            schedule=row.get("schedule"),
            enabled=bool(row.get("enabled", 1)),
        )
