"""Information routing — create tasks, considerations, append to notes.

Each public function is consent-gated. Internal ``_impl`` variants exist
so that ``execute_routing_plan`` can batch operations under a single
consent grant without triggering per-item consent prompts.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import re
from pathlib import Path
from typing import Any

from work_buddy.consent import requires_consent
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Valid enum values
VALID_URGENCIES = {"low", "medium", "high"}
VALID_CONSIDERATION_TYPES = {
    "consideration", "note", "question", "assumption",
    "risk", "decision", "idea", "bug",
}
VALID_CONSIDERATION_STATUSES = {
    "open", "explore", "decide", "actioned", "parked", "archived",
}


# ---------------------------------------------------------------------------
# Internal implementations (no consent — called by execute_routing_plan)
# ---------------------------------------------------------------------------


@contextmanager
def _legacy_obsidian_action_guard():
    """Fence one complete legacy action before it can inspect the archive."""

    from work_buddy.health.preferences import is_wanted
    from work_buddy.journal_capture.authority import (
        JournalAuthorityStateError,
        legacy_markdown_write_guard,
    )

    if is_wanted("obsidian") is False:
        raise JournalAuthorityStateError(
            "Obsidian-backed Journal actions are disabled by user preference."
        )
    try:
        with legacy_markdown_write_guard():
            yield
    except JournalAuthorityStateError as exc:
        raise JournalAuthorityStateError(
            "Obsidian-backed Journal actions are retired under native authority."
        ) from exc


def _guard_legacy_obsidian_action(func):
    """Hold the durable Journal writer barrier around a legacy action."""

    @wraps(func)
    def guarded(*args, **kwargs):
        with _legacy_obsidian_action_guard():
            return func(*args, **kwargs)

    return guarded

def _create_task_impl(
    task_text: str,
    vault_root: Path | None,
    urgency: str = "medium",
    project: str | None = None,
    due_date: str | None = None,
) -> dict[str, Any]:
    """Create a task through the authority-aware WorkItem port. No consent gate.

    Native authority returns the task identity, revisions, and mutation receipt
    without claiming a Markdown line or file path.  Before cutover, the same port
    preserves the legacy master-list response. ``vault_root`` is a compatibility
    argument and is not used by native task creation.
    """
    if urgency not in VALID_URGENCIES:
        raise ValueError(
            f"Invalid urgency {urgency!r}; must be one of {VALID_URGENCIES}"
        )

    from work_buddy.threads.models import Task
    from work_buddy.tasks.runtime import native_authority_active

    native_tasks = native_authority_active()
    result = Task.create(
        task_text=task_text,
        urgency=urgency,
        project=project,
        due_date=due_date,
        creation_provenance="agent_inferred_from_journal",
        user_involvement="medium",
    )

    if not result.get("success"):
        return {
            "success": False,
            "message": result.get("message", "task creation failed"),
        }

    logger.info("Created task: %s (id=%s)", task_text[:60], result.get("task_id"))
    if native_tasks:
        from work_buddy.tasks.integration_results import native_creation_result

        return {
            "success": True,
            **native_creation_result(result),
            "message": "Task created",
        }
    return {
        "success": True,
        "task_line": result.get("task_line"),
        "task_id": result.get("task_id"),
        "file": result.get("file"),
        "message": "Task created in master-task-list.md",
    }


@_guard_legacy_obsidian_action
def _create_consideration_impl(
    title: str,
    vault_root: Path,
    project: str,
    type: str = "consideration",
    status: str = "open",
    body: str = "",
    review_date: str | None = None,
) -> dict[str, Any]:
    """Create a consideration file. No consent gate."""
    if type not in VALID_CONSIDERATION_TYPES:
        raise ValueError(
            f"Invalid type {type!r}; must be one of {VALID_CONSIDERATION_TYPES}"
        )
    if status not in VALID_CONSIDERATION_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}; must be one of {VALID_CONSIDERATION_STATUSES}"
        )

    from work_buddy.vault_index.authority_exclusions import (
        legacy_root_write_guard,
    )

    considerations_dir = vault_root / "work" / "considerations"
    with legacy_root_write_guard(considerations_dir):
        return _create_consideration_locked(
            title=title,
            vault_root=vault_root,
            project=project,
            type=type,
            status=status,
            body=body,
            review_date=review_date,
        )


def _create_consideration_locked(
    *,
    title: str,
    vault_root: Path,
    project: str,
    type: str,
    status: str,
    body: str,
    review_date: str | None,
) -> dict[str, Any]:
    """Create a consideration while all matching archive locks are held."""

    considerations_dir = vault_root / "work" / "considerations"

    # Place in project subdirectory if it exists
    project_dir = considerations_dir / project
    if project_dir.is_dir():
        target_dir = project_dir
    else:
        target_dir = considerations_dir

    # Slugify title for filename
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        slug = "untitled"
    filename = f"{slug}.md"

    # Avoid collisions
    target_path = target_dir / filename
    counter = 1
    while target_path.exists():
        counter += 1
        target_path = target_dir / f"{slug}-{counter}.md"

    # Build frontmatter
    fm_lines = [
        "---",
        f"project: {project}",
        f"type: {type}",
        f"status: {status}",
        "importance:",
        "effort_hours:",
    ]
    if review_date:
        fm_lines.append(f"decision_date: {review_date}")
    else:
        fm_lines.append("decision_date:")
    fm_lines.append("---")

    # Build body
    body_lines = [
        f"**Tags:** #projects/{project}",
        "> [!tip] TL;DR",
        f"> - {title}",
        "",
        "## Context & Motivation",
        "",
    ]

    if body:
        body_lines.append(body)
        body_lines.append("")

    body_lines.extend([
        "## TODOs",
        "",
        "## Technical Notes",
        "",
        "## Open Questions",
        "",
    ])

    content = "\n".join(fm_lines) + "\n" + "\n".join(body_lines)

    try:
        target_path.write_text(content, encoding="utf-8")
    except OSError as e:
        return {"success": False, "message": f"File write error: {e}"}

    logger.info(f"Created consideration: {target_path}")
    return {
        "success": True,
        "file": target_path.as_posix(),
        "title": title,
        "message": f"Consideration created: {target_path.name}",
    }


@_guard_legacy_obsidian_action
def _append_to_note_impl(
    content: str,
    vault_root: Path,
    note_path: str,
) -> dict[str, Any]:
    """Append content to an existing vault note. No consent gate."""
    resolved = (vault_root / note_path).resolve()

    # Security: prevent path traversal outside vault
    if not resolved.is_relative_to(vault_root.resolve()):
        raise ValueError(
            f"Path traversal detected: {note_path} resolves outside vault"
        )

    from work_buddy.vault_index.authority_exclusions import (
        legacy_root_write_guard,
    )

    with legacy_root_write_guard(resolved):
        return _append_to_note_locked(
            content=content,
            resolved=resolved,
            note_path=note_path,
        )


def _append_to_note_locked(
    *,
    content: str,
    resolved: Path,
    note_path: str,
) -> dict[str, Any]:
    """Append to one note while its configured archive lock is held."""

    if not resolved.suffix == ".md":
        return {
            "success": False,
            "message": f"Not a markdown file: {note_path}",
        }

    if not resolved.exists():
        return {
            "success": False,
            "message": f"Note not found: {note_path}",
        }

    try:
        adapter = None
        snapshot = None
        if (
            resolved.parent.name.lower() == "journal"
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved.stem)
        ):
            from work_buddy.journal_capture.content_adapter import JournalContentAdapter

            adapter = JournalContentAdapter(resolved.parent.parent)
            snapshot = adapter.snapshot(resolved.stem)
            existing = snapshot.content
        else:
            existing = resolved.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        existing += "\n" + content + "\n"
        if adapter is not None and snapshot is not None:
            adapter.write_day_cas(
                resolved.stem,
                expected_file_sha256=snapshot.file_sha256,
                content=existing,
                content_hint="wb:journal-backlog-append/v1",
            )
        else:
            resolved.write_text(existing, encoding="utf-8")
    except (OSError, RuntimeError) as e:
        return {"success": False, "message": f"File write error: {e}"}

    logger.info(f"Appended to note: {note_path}")
    return {
        "success": True,
        "file": str(resolved),
        "message": f"Content appended to {note_path}",
    }


# ---------------------------------------------------------------------------
# Public API (consent-gated)
# ---------------------------------------------------------------------------

@requires_consent(
    operation="journal_backlog_create_task",
    reason="Creating a new task in the task system",
    risk="moderate",
    default_ttl=30,
)
def create_task(
    task_text: str,
    vault_root: Path,
    urgency: str = "medium",
    project: str | None = None,
    due_date: str | None = None,
) -> dict[str, Any]:
    """Create a task through the active task authority (consent-gated).

    Args:
        task_text: The task description text.
        vault_root: Compatibility vault root used only before native cutover.
        urgency: ``"low"``, ``"medium"``, or ``"high"``.
        project: Optional project slug (e.g., ``"my-research"``).
        due_date: Optional due date as ``YYYY-MM-DD``.

    Returns:
        Native results include task/document identity, revisions, and receipt;
        legacy results retain ``task_line`` and ``file``.
    """
    return _create_task_impl(task_text, vault_root, urgency, project, due_date)


@requires_consent(
    operation="journal_backlog_create_consideration",
    reason="Creating a new consideration note in the Obsidian vault",
    risk="moderate",
    default_ttl=30,
)
def create_consideration(
    title: str,
    vault_root: Path,
    project: str,
    type: str = "consideration",
    status: str = "open",
    body: str = "",
    review_date: str | None = None,
) -> dict[str, Any]:
    """Create a consideration file (consent-gated).

    Args:
        title: Title of the consideration.
        vault_root: Path to the Obsidian vault root.
        project: Project slug.
        type: One of the valid consideration types.
        status: One of the valid consideration statuses.
        body: Optional body text for the Context & Motivation section.
        review_date: Optional review/decision date as ``YYYY-MM-DD``.

    Returns:
        Dict with ``success``, ``file``, ``title``, ``message``.
    """
    return _create_consideration_impl(
        title, vault_root, project, type, status, body, review_date
    )


@requires_consent(
    operation="journal_backlog_append_to_note",
    reason="Appending content to an existing note in the Obsidian vault",
    risk="moderate",
    default_ttl=30,
)
def append_to_note(
    content: str,
    vault_root: Path,
    note_path: str,
) -> dict[str, Any]:
    """Append content to an existing vault note (consent-gated).

    Args:
        content: Markdown text to append.
        vault_root: Path to the Obsidian vault root.
        note_path: Path relative to vault root (e.g., ``"work/projects/my-research/my-research.md"``).

    Returns:
        Dict with ``success``, ``file``, ``message``.
    """
    return _append_to_note_impl(content, vault_root, note_path)


@requires_consent(
    operation="journal_backlog_execute_routing",
    reason="Executing a batch of routing decisions: creating tasks, considerations, and appending to notes",
    risk="moderate",
    default_ttl=30,
)
def execute_routing_plan(
    plan: list[dict[str, Any]],
    vault_root: Path,
) -> dict[str, Any]:
    """Execute a batch of routing decisions (consent-gated).

    Each item in ``plan`` should have:
    - ``id`` -- thread ID
    - ``action`` -- ``"route"``, ``"delete"``, ``"skip"``, or ``"split"``
    - For ``"route"``: ``destination_type`` (``"task"``, ``"consideration"``,
      ``"note"``), plus type-specific fields
    - For ``"split"``: ``splits`` list with per-split action dicts

    Args:
        plan: List of routing decision dicts.
        vault_root: Path to the Obsidian vault root.

    Returns:
        Dict with ``success``, ``results`` (per-item), and ``summary``.
    """
    results: list[dict[str, Any]] = []
    summary = {"routed": 0, "deleted": 0, "skipped": 0, "split": 0, "errors": 0}

    for item in plan:
        item_id = item.get("id", "unknown")
        action = item.get("action", "skip")

        try:
            if action == "delete":
                results.append({
                    "id": item_id,
                    "action": "delete",
                    "success": True,
                    "reason": item.get("reason", ""),
                })
                summary["deleted"] += 1

            elif action == "skip":
                results.append({
                    "id": item_id,
                    "action": "skip",
                    "success": True,
                    "reason": item.get("reason", "User deferred"),
                })
                summary["skipped"] += 1

            elif action == "route":
                result = _execute_single_route(item, vault_root)
                results.append(result)
                if result.get("success"):
                    summary["routed"] += 1
                else:
                    summary["errors"] += 1

            elif action == "split":
                split_results = []
                for split_item in item.get("splits", []):
                    if split_item.get("action") == "route":
                        r = _execute_single_route(split_item, vault_root)
                        split_results.append(r)
                        if r.get("success"):
                            summary["routed"] += 1
                        else:
                            summary["errors"] += 1
                    elif split_item.get("action") == "delete":
                        split_results.append({
                            "action": "delete",
                            "success": True,
                            "reason": split_item.get("reason", ""),
                        })
                        summary["deleted"] += 1

                results.append({
                    "id": item_id,
                    "action": "split",
                    "success": True,
                    "split_results": split_results,
                })
                summary["split"] += 1

            else:
                results.append({
                    "id": item_id,
                    "action": action,
                    "success": False,
                    "message": f"Unknown action: {action!r}",
                })
                summary["errors"] += 1

        except Exception as e:
            logger.error(f"Error routing {item_id}: {e}")
            results.append({
                "id": item_id,
                "action": action,
                "success": False,
                "message": str(e),
            })
            summary["errors"] += 1

    logger.info(
        f"Routing plan executed: {summary['routed']} routed, "
        f"{summary['deleted']} deleted, {summary['skipped']} skipped, "
        f"{summary['split']} split, {summary['errors']} errors"
    )

    return {
        "success": summary["errors"] == 0,
        "results": results,
        "summary": summary,
    }


def _execute_single_route(
    item: dict[str, Any], vault_root: Path
) -> dict[str, Any]:
    """Dispatch a single route action to the appropriate _impl function."""
    dest_type = item.get("destination_type", "")
    item_id = item.get("id", "unknown")

    if dest_type == "task":
        return _create_task_impl(
            task_text=item.get("task_text", item.get("text", "")),
            vault_root=vault_root,
            urgency=item.get("urgency", "medium"),
            project=item.get("project"),
            due_date=item.get("due_date"),
        )

    elif dest_type == "consideration":
        return _create_consideration_impl(
            title=item.get("title", item.get("text", "")),
            vault_root=vault_root,
            project=item.get("project", "general"),
            type=item.get("consideration_type", "consideration"),
            status=item.get("status", "open"),
            body=item.get("body", ""),
            review_date=item.get("review_date"),
        )

    elif dest_type == "note":
        return _append_to_note_impl(
            content=item.get("content", item.get("text", "")),
            vault_root=vault_root,
            note_path=item.get("note_path", ""),
        )

    else:
        return {
            "id": item_id,
            "success": False,
            "message": f"Unknown destination_type: {dest_type!r}",
        }
