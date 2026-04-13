"""Obsidian Tasks plugin (v7.23.1) runtime access via eval_js.

All functions require Obsidian to be running with the work-buddy plugin active.
The Tasks plugin must be installed and its cache warm.

Discovered runtime surface:
- plugin.cache.tasks — array of Task objects
- plugin.cache.state — "Warm" when ready
- Task.status.type — "TODO" | "DONE"
- Task.priority — 1 (high), 2 (medium), 3 (none)
- Task.tags — array of #tasker/* tags (NOT #todo)
- Task._dueDate/_doneDate — Moment.js objects
- Task.taskLocation._tasksFile._path — vault-relative file path
- Task.taskLocation._lineNumber — line number in file
"""

from pathlib import Path
from typing import Any

from work_buddy.obsidian import bridge
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_JS_DIR = Path(__file__).parent / "_js"


def _load_js(name: str) -> str:
    """Load a JS snippet from the _js directory."""
    return (_JS_DIR / name).read_text(encoding="utf-8")


def _run_js(
    snippet_name: str,
    replacements: dict[str, str] | None = None,
    timeout: int = 15,
) -> Any:
    """Load a JS snippet, apply replacements, execute via eval_js.

    Raises RuntimeError if the bridge is unavailable or the JS returns an error.
    """
    bridge.require_available()
    js = _load_js(snippet_name)
    for placeholder, value in (replacements or {}).items():
        js = js.replace(placeholder, value)
    result = bridge.eval_js(js, timeout=timeout)
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"Tasks plugin error: {result['error']}")
    return result


# ── Readiness ───────────────────────────────────────────────────


def check_ready() -> dict[str, Any]:
    """Check if the Tasks plugin cache is warm and ready.

    Returns a dict with:
    - ready: bool — True when cache state is 'Warm'
    - state: str — cache state (e.g. 'Warm', 'Cold')
    - task_count: int — number of tasks in cache
    - version: str — plugin version
    """
    bridge.require_available()
    result = bridge.eval_js(_load_js("check_ready.js"), timeout=10)
    if result is None:
        return {"ready": False, "reason": "eval_js returned None"}
    if result.get("ready") and result.get("version"):
        from work_buddy.obsidian.plugin_versions import confirm_working
        confirm_working("obsidian-tasks-plugin", result["version"])
    return result


# ── Queries ─────────────────────────────────────────────────────


def query_tasks(
    status: str | None = None,
    file_path: str | None = None,
    tag: str | None = None,
    text: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Query tasks with optional filters.

    All filters are AND-combined. Pass None to skip a filter.

    Args:
        status: Filter by status type — 'TODO', 'DONE', or None for all.
        file_path: Filter by vault file path prefix (e.g. 'tasks/master-task-list.md').
        tag: Filter by exact tag (e.g. '#tasker/state/inbox').
        text: Filter by text in description (case-insensitive substring match).
        limit: Maximum number of tasks to return (default 500).

    Returns:
        Dict with 'total_matched', 'returned', and 'tasks' (list of task dicts).
        Each task dict has: description, status_type, status_symbol, priority,
        tags, file, line, section, due_date, done_date, created_date,
        scheduled_date, start_date, cancelled_date, has_children, id, block_link.
    """
    return _run_js(
        "query_tasks.js",
        {
            "__STATUS_TYPE__": status or "",
            "__FILE_PATH__": file_path or "",
            "__TAG_FILTER__": tag or "",
            "__TEXT_FILTER__": _escape_js(text) if text else "",
            "__LIMIT__": str(limit),
        },
    )


def get_task_counts() -> dict[str, Any]:
    """Get summary statistics for all tasks in the vault.

    Returns:
        Dict with:
        - total, todo, done: int — basic counts
        - overdue: int — incomplete tasks past their due date
        - due_soon: int — tasks due today
        - by_priority: dict — counts by priority (1=high, 2=medium, 3=none)
        - by_tag: dict — counts by tag
        - by_file: dict — counts by source file
    """
    return _run_js("task_counts.js")


def get_overdue_tasks() -> dict[str, Any]:
    """Find all incomplete tasks that are past their due date.

    Returns:
        Dict with 'count' and 'tasks' (list sorted by most overdue first).
        Each task has: description, due_date, priority, tags, file, line, days_overdue.
    """
    return _run_js("overdue_tasks.js")


def search_tasks(query: str) -> dict[str, Any]:
    """Search tasks by text content (case-insensitive).

    Searches across all tasks (both TODO and DONE) in the description field.

    Args:
        query: Search text.

    Returns:
        Dict with 'query', 'count', and 'tasks' (list of matching task dicts).
    """
    return _run_js("search_tasks.js", {"__QUERY__": _escape_js(query)})


# ── Convenience ─────────────────────────────────────────────────


def get_inbox_tasks() -> dict[str, Any]:
    """Get all incomplete tasks in inbox state.

    Checks the SQLite store first (new tasks), then falls back to
    querying the #tasker/state/inbox tag (legacy tasks).
    For the most accurate results, use manager.py functions instead.
    """
    # Legacy path: tag-based query (catches old tasks with inline tags)
    return query_tasks(status="TODO", tag="#tasker/state/inbox")


def get_focused_tasks() -> dict[str, Any]:
    """Get all incomplete tasks in focused state.

    Checks the SQLite store first (new tasks), then falls back to
    querying the #tasker/state/focused tag (legacy tasks).
    For the most accurate results, use manager.py functions instead.
    """
    # Legacy path: tag-based query (catches old tasks with inline tags)
    return query_tasks(status="TODO", tag="#tasker/state/focused")


def get_master_list_tasks(status: str | None = "TODO") -> dict[str, Any]:
    """Get tasks from the master task list file only.

    Args:
        status: 'TODO', 'DONE', or None for all.
    """
    return query_tasks(status=status, file_path="tasks/master-task-list.md")


# ── Helpers ─────────────────────────────────────────────────────


def _escape_js(text: str) -> str:
    """Escape text for safe insertion into a JS string literal."""
    return (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
