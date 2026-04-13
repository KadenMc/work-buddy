"""Keep the Rhythm plugin (v0.2.8) writing activity access via eval_js.

Queries the plugin's in-memory activity ledger (plugin.data.stats.dailyActivity)
for per-file, per-day writing records with 5-minute bucketed word/char deltas.

Runtime surface (discovered via eval_js probing):
  Plugin instance: app.plugins.plugins["keep-the-rhythm"]
  Data store: plugin.data (loaded from data.json on plugin load)
    - schema: "0.2"
    - settings: {enabledLanguages, dailyWritingGoal, ...}
    - stats: {currentStreak, highestStreak, daysWithCompletedGoal, dailyActivity}

  dailyActivity record schema:
    {date: "YYYY-MM-DD", filePath: str, wordCountStart: int,
     charCountStart: int, changes: [{timeKey: "HH:MM", w: int, c: int}], id: int}

  No Dexie DB at runtime — all data is in-memory via plugin.data.
  On external settings change, plugin merges new activity into its store.

  Commands:
    - keep-the-rhythm:open-keep-the-rhythm
    - keep-the-rhythm:check-ktr-streak
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
    """Load a JS snippet, apply replacements, execute via eval_js."""
    bridge.require_available()
    js = _load_js(snippet_name)
    for placeholder, value in (replacements or {}).items():
        js = js.replace(placeholder, value)
    result = bridge.eval_js(js, timeout=timeout)
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"Keep the Rhythm error: {result['error']}")
    return result


def _escape_js(text: str) -> str:
    """Escape a string for safe insertion into JS template placeholders."""
    return text.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")


# ── Readiness ───────────────────────────────────────────────────


def check_ready() -> dict[str, Any]:
    """Check if Keep the Rhythm plugin is loaded and has activity data.

    Returns a dict with:
    - ready: bool
    - version: str — plugin version
    - activity_count: int — total daily activity records
    - unique_files: int — files with tracked writing activity
    - current_streak: int — current writing streak in days
    - reason: str — explanation if not ready
    """
    bridge.require_available()
    result = bridge.eval_js(_load_js("check_ready.js"), timeout=15)
    if result is None:
        return {"ready": False, "reason": "eval_js returned None"}
    if result.get("ready") and result.get("version"):
        from work_buddy.obsidian.plugin_versions import confirm_working
        confirm_working("keep-the-rhythm", result["version"])
    return result


# ── Query Operations ────────────────────────────────────────────


def get_hot_files(
    since_date: str,
    until_date: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Get files ranked by writing activity "hotness" in a time window.

    Hotness is a weighted score of recency, frequency (active days),
    and intensity (number of 5-minute editing buckets).

    Args:
        since_date: Start date YYYY-MM-DD (inclusive).
        until_date: End date YYYY-MM-DD (inclusive). Default: today.
        limit: Maximum files to return (default 20).

    Returns a dict with:
    - window: {since, until}
    - total_files: int — files with any activity in window
    - files: list of dicts, each with:
      - filePath: str
      - active_days: int — number of distinct days with activity
      - total_buckets: int — 5-minute editing sessions
      - total_word_delta: int — absolute word changes
      - total_char_delta: int — absolute character changes
      - last_active_date: str — most recent activity date
      - last_active_time: str — most recent activity time (HH:MM)
      - hot_score: float — composite hotness score
    """
    if until_date is None:
        from work_buddy.journal import user_now
        until_date = user_now().strftime("%Y-%m-%d")

    return _run_js("hot_files.js", {
        "__SINCE_DATE__": _escape_js(since_date),
        "__UNTIL_DATE__": _escape_js(until_date),
        "__LIMIT__": str(limit),
    }, timeout=20)


def get_file_activity(
    file_path: str,
    since_date: str,
    until_date: str | None = None,
) -> dict[str, Any]:
    """Get detailed writing activity for a specific file.

    Args:
        file_path: Vault-relative path (e.g. "journal/2026-04-04.md").
        since_date: Start date YYYY-MM-DD (inclusive).
        until_date: End date YYYY-MM-DD (inclusive). Default: today.

    Returns a dict with:
    - filePath: str
    - found: bool
    - records: list of dicts, each with:
      - date: str
      - word_count_start: int — baseline word count at file open
      - char_count_start: int — baseline char count
      - changes: list of {timeKey, w, c} — 5-minute bucketed deltas
      - total_word_delta: int — net words changed that day
      - total_char_delta: int — net chars changed that day
    """
    if until_date is None:
        from work_buddy.journal import user_now
        until_date = user_now().strftime("%Y-%m-%d")

    return _run_js("file_activity.js", {
        "__FILE_PATH__": _escape_js(file_path),
        "__SINCE_DATE__": _escape_js(since_date),
        "__UNTIL_DATE__": _escape_js(until_date),
    })
