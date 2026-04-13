"""Vault event tracking via eval_js-registered listeners.

Maintains a compact per-file stats ledger (modify counts per day + last-modified
timestamp) in localStorage with a configurable rolling window. Listeners are
registered idempotently via bootstrap() — re-calling after Obsidian restart
re-registers them and reconciles offline changes from file mtimes.

Data model per file:
    {
        last: int,               # unix ms of most recent event
        days: {"YYYY-MM-DD": N}, # modify count per day (within window)
        created: int|null,       # unix ms if created within window
        renamedFrom: str|null,   # previous path if renamed
    }

Queries return computed results — the raw ledger is never exposed to agents.
"""

from pathlib import Path
from typing import Any

from work_buddy.obsidian import bridge
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_JS_DIR = Path(__file__).parent / "_js"

_DEFAULT_WINDOW_DAYS = 7


def _load_js(name: str) -> str:
    """Load a JS snippet from the _js directory."""
    return (_JS_DIR / name).read_text(encoding="utf-8")


def _run_js(
    snippet_name: str,
    replacements: dict[str, str] | None = None,
    timeout: int = 15,
) -> Any:
    """Load a JS snippet, apply replacements, execute via eval_js."""
    bridge.require_available()
    js = _load_js(snippet_name)
    for placeholder, value in (replacements or {}).items():
        js = js.replace(placeholder, value)
    result = bridge.eval_js(js, timeout=timeout)
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"Vault events error: {result['error']}")
    return result


def _escape_js(text: str) -> str:
    """Escape a string for safe insertion into JS template placeholders."""
    return text.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")


def _default_exclude_folders() -> list[str]:
    """Get default folder exclusions from config."""
    from work_buddy.config import load_config
    cfg = load_config()
    return cfg.get("obsidian", {}).get("exclude_folders", [])


# ── Bootstrap ───────────────────────────────────────────────────


def bootstrap(window_days: int | None = None) -> dict[str, Any]:
    """Register vault event listeners and initialize the ledger.

    Idempotent — safe to call multiple times. On first call, registers
    create/modify/rename/delete listeners and reconciles with current
    file mtimes. Subsequent calls return current status.

    Args:
        window_days: Rolling window in days (default 7). Events older
            than this are pruned on bootstrap.

    Returns a dict with:
    - status: "bootstrapped" or "already_active"
    - file_count: int — files currently tracked
    - reconciled: int — files reconciled from offline changes (first boot)
    - window_days: int — active window size
    """
    if window_days is None:
        from work_buddy.config import load_config
        cfg = load_config()
        window_days = cfg.get("obsidian", {}).get(
            "vault_events_window_days", _DEFAULT_WINDOW_DAYS
        )

    return _run_js("bootstrap.js", {
        "__WINDOW_DAYS__": str(window_days),
    }, timeout=30)


# ── Queries ─────────────────────────────────────────────────────


def get_hot_files(
    since_date: str,
    until_date: str | None = None,
    limit: int = 20,
    exclude_folders: list[str] | None = None,
) -> dict[str, Any]:
    """Rank tracked files by modification hotness in a date window.

    Score combines recency, active days, and total modification count.
    Requires bootstrap() to have been called.

    Args:
        since_date: Start date YYYY-MM-DD (inclusive).
        until_date: End date YYYY-MM-DD (inclusive). Default: today.
        limit: Max files to return (default 20).
        exclude_folders: Folder prefixes to skip. Default: from config.

    Returns a dict with:
    - window: {since, until}
    - total_tracked: int — all files in ledger
    - matching_files: int — files with activity in window
    - files: list of dicts with path, hot_score, total_modifications,
      active_days, last_modified, created_in_window
    """
    if until_date is None:
        from work_buddy.journal import user_now
        until_date = user_now().strftime("%Y-%m-%d")

    if exclude_folders is None:
        exclude_folders = _default_exclude_folders()

    import json
    return _run_js("query_hot.js", {
        "__SINCE_DATE__": _escape_js(since_date),
        "__UNTIL_DATE__": _escape_js(until_date),
        "__LIMIT__": str(limit),
        "__EXCLUDE_FOLDERS__": json.dumps(exclude_folders),
    }, timeout=15)


def get_recent_files(
    since_hours: float = 2,
    limit: int = 30,
    exclude_folders: list[str] | None = None,
) -> dict[str, Any]:
    """Get files modified within the last N hours.

    Requires bootstrap() to have been called.

    Args:
        since_hours: Lookback window in hours (default 2).
        limit: Max files to return (default 30).
        exclude_folders: Folder prefixes to skip. Default: from config.

    Returns a dict with:
    - since: ISO datetime
    - total_results: int
    - files: list of dicts with path, last_modified, created_in_window,
      renamed_from
    """
    import time
    since_ms = int((time.time() - since_hours * 3600) * 1000)

    if exclude_folders is None:
        exclude_folders = _default_exclude_folders()

    import json
    return _run_js("query_recent.js", {
        "__SINCE_MS__": str(since_ms),
        "__LIMIT__": str(limit),
        "__EXCLUDE_FOLDERS__": json.dumps(exclude_folders),
    })


def status() -> dict[str, Any]:
    """Get ledger status: active, file count, storage size, date range.

    Returns a dict with:
    - active: bool — listeners registered
    - file_count: int
    - total_modifications: int
    - window_days: int
    - date_range: {oldest, newest} or None
    - bootstrapped: ISO datetime
    - storage_bytes: int
    """
    bridge.require_available()
    return bridge.eval_js(_load_js("status.js"), timeout=10)
