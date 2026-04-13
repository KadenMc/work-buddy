"""Day Planner plugin (v0.28.0) runtime access via eval_js bridge.

Provides readiness checks, plan reading/writing, and resync triggers.
Plan entries are ephemeral scheduling artifacts — NOT canonical tasks.
The source of truth for tasks remains the master-task-list + SQLite store.

Runtime surface (discovered via probing):
- plugin.store (Redux): getState(), dispatch(), subscribe()
- plugin.dataviewFacade: getTasksFromPath(), getTaskAtLine(), taskCache
- plugin.vaultFacade: editFile(), editLineInFile(), toggleCheckboxInFile()
- plugin.transationWriter: writeTransaction([{path, updateFn}]), undo()
- plugin.sTaskEditor: edit(), clockIn/Out/CancelUnderCursor()
- plugin.settingsStore: Svelte writable (subscribe)

Plan entries use markdown format: ``- [ ] HH:mm - HH:mm Description``
"""

import re
from pathlib import Path
from typing import Any

from work_buddy.obsidian import bridge
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_JS_DIR = Path(__file__).parent / "_js"

_ENTRY_RE = re.compile(
    r"^-\s+\[([ x])\]\s*(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s+(.+)$"
)
_UNSCHEDULED_RE = re.compile(
    r"^-\s+\[([ x])\]\s+(.+)$"
)
_SECTION_RE = re.compile(
    r"^#\s+Day\s+planner\s*$", re.IGNORECASE | re.MULTILINE
)
_NEXT_H1_RE = re.compile(
    r"^#\s+\*{0,2}[A-Z]", re.MULTILINE
)


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
        raise RuntimeError(f"Day Planner error: {result['error']}")
    return result


# ── Readiness ───────────────────────────────────────────────────


def check_ready() -> dict[str, Any]:
    """Check if the Day Planner plugin is loaded and return key settings.

    Returns a dict with:
    - ready: bool
    - version: str — plugin version
    - plannerHeading, startHour, snapStepMinutes, etc. — plugin settings
    - hasRemoteCalendars: bool — whether iCal feeds are configured
    - reason: str — explanation if not ready
    """
    bridge.require_available()
    result = bridge.eval_js(_load_js("check_ready.js"), timeout=15)
    if result is None:
        return {"ready": False, "reason": "eval_js returned None"}
    if result.get("ready") and result.get("version"):
        from work_buddy.obsidian.plugin_versions import confirm_working
        confirm_working("obsidian-day-planner", result["version"])
    return result


# ── Plan reading ────────────────────────────────────────────────


def _get_section_bounds(content: str) -> tuple[int, int] | None:
    """Return (start, end) byte offsets of the Day planner section body.

    start = right after the Day planner header line
    end = start of the next H1 section (or EOF)
    """
    match = _SECTION_RE.search(content)
    if not match:
        return None
    section_start = match.end()
    rest = content[section_start:]
    next_h1 = _NEXT_H1_RE.search(rest)
    section_end = section_start + next_h1.start() if next_h1 else len(content)
    return section_start, section_end


def get_todays_plan(journal_path: str) -> dict[str, Any]:
    """Read today's plan entries from the Day Planner section of a journal.

    Args:
        journal_path: Vault-relative path (e.g. ``journal/2026-04-05.md``)

    Returns dict with:
    - found: bool — whether the Day planner section exists
    - entries: list[dict] — each with time_start, time_end, text, checked
    - unscheduled: list[dict] — entries without time ranges
    - entry_count: int — total entries (scheduled + unscheduled)
    """
    content = bridge.read_file(journal_path)
    if content is None:
        return {"found": False, "entries": [], "unscheduled": [],
                "entry_count": 0, "reason": "Could not read journal file"}

    bounds = _get_section_bounds(content)
    if bounds is None:
        return {"found": False, "entries": [], "unscheduled": [],
                "entry_count": 0, "reason": "No Day planner section found"}

    section_body = content[bounds[0]:bounds[1]]
    entries = []
    unscheduled = []

    for line in section_body.split("\n"):
        line = line.strip()
        m = _ENTRY_RE.match(line)
        if m:
            entries.append({
                "time_start": m.group(2),
                "time_end": m.group(3),
                "text": m.group(4).strip(),
                "checked": m.group(1) == "x",
            })
            continue
        m = _UNSCHEDULED_RE.match(line)
        if m:
            unscheduled.append({
                "text": m.group(2).strip(),
                "checked": m.group(1) == "x",
            })

    return {
        "found": True,
        "entries": entries,
        "unscheduled": unscheduled,
        "entry_count": len(entries) + len(unscheduled),
    }


# ── Plan writing ────────────────────────────────────────────────


def _format_entry(entry: dict) -> str:
    """Format a single plan entry as a markdown line."""
    status = "x" if entry.get("checked") else " "
    if entry.get("time_start") and entry.get("time_end"):
        return f"- [{status}] {entry['time_start']} - {entry['time_end']} {entry['text']}"
    return f"- [{status}] {entry['text']}"


def write_plan(
    journal_path: str,
    entries: list[dict],
) -> dict[str, Any]:
    """Write time-blocked plan entries to the Day Planner section of a journal.

    Replaces the entire section body with the new entries.
    Uses bridge.write_file which is consent-gated.

    Args:
        journal_path: Vault-relative path (e.g. ``journal/2026-04-05.md``)
        entries: list of dicts with time_start, time_end, text, checked

    Returns dict with success, entries_written, journal_path.
    """
    content = bridge.read_file(journal_path)
    if content is None:
        return {"success": False, "reason": "Could not read journal file"}

    bounds = _get_section_bounds(content)
    if bounds is None:
        return {"success": False, "reason": "No Day planner section found in journal"}

    section_start, section_end = bounds

    # Build the new section body
    lines = [_format_entry(e) for e in entries]
    new_body = "\n" + "\n".join(lines) + "\n\n"

    # Replace section body
    new_content = content[:section_start] + new_body + content[section_end:]

    ok = bridge.write_file(journal_path, new_content)
    if not ok:
        return {"success": False, "reason": "bridge.write_file failed"}

    logger.info("Wrote %d plan entries to %s", len(entries), journal_path)
    return {
        "success": True,
        "entries_written": len(entries),
        "journal_path": journal_path,
    }


# ── Resync ──────────────────────────────────────────────────────


def trigger_resync() -> dict[str, Any]:
    """Trigger Day Planner's re-sync command to refresh the timeline.

    Call this after writing plan entries so the visual timeline updates.
    """
    return _run_js("trigger_resync.js")
