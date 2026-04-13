"""Journal update: read journal state, manage log entries, and append to Obsidian journals.

Programmatic entry points for the update-journal workflow DAG:
- read_journal_state() — read-journal task (date resolution, existence check, activity window)
- append_to_journal() — write task (consent-gated, chronological insertion)

Sign-in extraction, wellness interpretation, and briefing persistence for the
morning-routine workflow DAG are also here (journal I/O).

Signal gathering is handled by work_buddy/collectors/ via the `collect` CLI with --since/--until.
Synthesis is an agentic step — the agent interprets the collector's digest.
"""

import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.consent import requires_consent
from work_buddy.logging_config import get_logger
from work_buddy.utils.git import get_wb_commit_hash

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def journal_path_for_date(date_str: str | None = None, vault_root: Path | None = None) -> Path:
    """Resolve the journal file path for a given date.

    Args:
        date_str: Date as 'YYYY-MM-DD'. Defaults to today.
        vault_root: Vault root path. Defaults to config value.

    Returns:
        Path to the journal markdown file.
    """
    if vault_root is None:
        vault_root = Path(load_config()["vault_root"])
    if date_str is None:
        date_str = user_now().strftime("%Y-%m-%d")
    return vault_root / "journal" / f"{date_str}.md"


# ---------------------------------------------------------------------------
# Journal existence + Obsidian availability
# ---------------------------------------------------------------------------

def ensure_journal_exists(
    vault_root: Path, date_str: str | None = None,
) -> dict[str, Any]:
    """Ensure the journal file for the target date exists.

    For today: triggers the Obsidian "Daily notes: Open today's daily note"
    command, which creates the file from template if it doesn't exist.

    For other dates: checks if the file exists. If not, reports that it must
    be created manually (Obsidian's daily-notes command only works for today).

    Requires Obsidian to be running with the Local REST API plugin.

    Returns:
        Dict with ``exists``, ``file``, ``created``, ``message`` keys.
    """
    if date_str is None:
        date_str = user_now().strftime("%Y-%m-%d")

    journal_file = vault_root / "journal" / f"{date_str}.md"
    today_str = user_now().strftime("%Y-%m-%d")

    if journal_file.exists():
        return {
            "exists": True,
            "file": str(journal_file),
            "created": False,
            "message": f"Journal for {date_str} already exists.",
        }

    # File doesn't exist — try to create it via Obsidian
    if date_str == today_str:
        try:
            from work_buddy.obsidian.commands import ObsidianCommands
            from work_buddy.obsidian.commands.daily_notes import DailyNotesCommands

            client = ObsidianCommands(vault_root)
            # This checks is_available() internally and raises if Obsidian isn't running
            daily = DailyNotesCommands(client)
            daily.open_today()

            # Give Obsidian a moment to create the file, then verify
            import time
            time.sleep(1)

            if journal_file.exists():
                return {
                    "exists": True,
                    "file": str(journal_file),
                    "created": True,
                    "message": f"Created today's journal ({date_str}) via Obsidian daily notes command.",
                }
            else:
                return {
                    "exists": False,
                    "file": str(journal_file),
                    "created": False,
                    "message": (
                        f"Triggered Obsidian daily notes command but journal file "
                        f"for {date_str} was not created. Check Obsidian."
                    ),
                }
        except RuntimeError as e:
            return {
                "exists": False,
                "file": str(journal_file),
                "created": False,
                "message": str(e),
            }
    else:
        # Past date — can't auto-create from template
        return {
            "exists": False,
            "file": str(journal_file),
            "created": False,
            "message": (
                f"Journal for {date_str} does not exist. "
                f"Obsidian can only auto-create today's note from template. "
                f"Open Obsidian and manually create the note, or target a date that exists."
            ),
        }


# ---------------------------------------------------------------------------
# Timezone-aware time
# ---------------------------------------------------------------------------

def user_now() -> datetime:
    """Return the current time in the user's configured timezone."""
    from work_buddy.config import USER_TZ
    return datetime.now(USER_TZ)


# ---------------------------------------------------------------------------
# Target date resolution
# ---------------------------------------------------------------------------

class TargetDateResult:
    """Result of resolving a target date, with ambiguity detection."""

    def __init__(self, date: str, ambiguous: bool = False, hint: str = ""):
        self.date = date
        self.ambiguous = ambiguous
        self.hint = hint

    def __str__(self):
        return self.date


def resolve_target_date(target: str | None = None) -> TargetDateResult:
    """Resolve the target journal date as an ISO string (YYYY-MM-DD).

    Uses the user's configured timezone (config.yaml ``timezone`` key).

    When ``target`` is None and the local time is between midnight and 4 AM,
    the result is marked ``ambiguous=True`` with a hint — the caller MUST
    ask the user before proceeding.

    Args:
        target: "today" (default/None), "yesterday", or an explicit YYYY-MM-DD string.

    Returns:
        TargetDateResult with ``.date``, ``.ambiguous``, and ``.hint`` fields.
    """
    now = user_now()

    if target is None or target == "today":
        date_str = now.strftime("%Y-%m-%d")
        # Ambiguity window: midnight to 4 AM
        if 0 <= now.hour < 4:
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            return TargetDateResult(
                date=date_str,
                ambiguous=True,
                hint=(
                    f"It's {now.strftime('%I:%M %p %Z')} — did you mean "
                    f"today ({date_str}) or yesterday ({yesterday})?"
                ),
            )
        return TargetDateResult(date=date_str)

    if target == "yesterday":
        return TargetDateResult(date=(now - timedelta(days=1)).strftime("%Y-%m-%d"))

    # Validate explicit date
    try:
        datetime.strptime(target, "%Y-%m-%d")
        return TargetDateResult(date=target)
    except ValueError:
        raise ValueError(
            f"Invalid target date '{target}'. Use 'today', 'yesterday', or YYYY-MM-DD."
        )


# ---------------------------------------------------------------------------
# Journal reading / timestamp extraction
# ---------------------------------------------------------------------------

# Matches lines like "- 1:11 PM - Done." or "* 12:00 PM - Started working."
_LOG_TIMESTAMP_RE = re.compile(
    r"^[\*\-]\s+"                     # bullet: * or -
    r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))"  # time: "1:11 PM"
    r"\s*-\s*",                       # separator dash
    re.MULTILINE,
)


def extract_last_log_timestamp(
    journal_content: str, journal_date: str | None = None,
) -> datetime | None:
    """Extract the timestamp of the last Log entry from a journal file.

    Parses the Log section for timestamped bullets (e.g., ``- 1:11 PM - ...``)
    and returns a datetime for the last one found.

    Args:
        journal_content: Full text of the journal file.
        journal_date: ISO date string (YYYY-MM-DD) for the journal. Defaults to today.

    Returns:
        datetime of the last log entry, or None if no timestamped entries found.
    """
    if journal_date is None:
        journal_date = user_now().strftime("%Y-%m-%d")

    # Find the Log section
    log_match = re.search(r"^#\s+\*{0,2}Log\*{0,2}\s*$", journal_content, re.MULTILINE)
    if not log_match:
        return None

    log_start = log_match.end()

    # Find the next top-level section after Log
    next_section = re.search(r"^#\s+\*{0,2}[A-Z]", journal_content[log_start:], re.MULTILINE)
    log_end = log_start + next_section.start() if next_section else len(journal_content)
    log_body = journal_content[log_start:log_end]

    # Find all timestamped entries in the Log section
    matches = list(_LOG_TIMESTAMP_RE.finditer(log_body))
    if not matches:
        return None

    last_time_str = matches[-1].group(1).strip()

    try:
        time_obj = datetime.strptime(last_time_str, "%I:%M %p").time()
        date_obj = datetime.strptime(journal_date, "%Y-%m-%d").date()
        return datetime.combine(date_obj, time_obj)
    except ValueError:
        return None


def extract_frontmatter_time(journal_content: str) -> datetime | None:
    """Extract time_started from the journal frontmatter as a fallback."""
    match = re.search(r"^time_started:\s*(.+)$", journal_content, re.MULTILINE)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1).strip())
    except ValueError:
        return None


def get_activity_window(journal_content: str, journal_date: str | None = None) -> datetime | None:
    """Determine when the activity window starts (last log entry or time_started).

    Returns the latest timestamp found, or None if neither exists.
    """
    log_ts = extract_last_log_timestamp(journal_content, journal_date)
    if log_ts is not None:
        return log_ts

    return extract_frontmatter_time(journal_content)


def read_journal_state(target: str | None = None) -> dict[str, Any]:
    """Read the journal for a target date and return everything needed for the DAG.

    This is the programmatic entry point for the ``read-journal`` DAG task.
    It bundles: date resolution, ambiguity detection, journal existence check,
    content reading, and activity window extraction.

    Args:
        target: "today", "yesterday", YYYY-MM-DD, or None (auto-detect).

    Returns:
        Dict with keys:
        - ``target_date``: resolved YYYY-MM-DD string
        - ``ambiguous``: True if the agent must ask the user to disambiguate
        - ``hint``: human-readable disambiguation prompt (empty if not ambiguous)
        - ``exists``: whether the journal file exists (after creation attempt)
        - ``created``: whether the file was just created via Obsidian
        - ``error``: error message if something failed, else None
        - ``collect_since``: ISO datetime for range start, or None
        - ``collect_until``: ISO datetime for range end, or None
        - ``last_log_ts``: ISO timestamp of last log entry, or None
        - ``log_section``: text of the Log section only (existing entries), or ""
        - ``sign_in_section``: text of the Sign-In section only, or ""
    """
    cfg = load_config()
    vault_root = Path(cfg["vault_root"])

    # 1. Resolve target date (with ambiguity detection)
    resolved = resolve_target_date(target)

    if resolved.ambiguous:
        return {
            "target_date": resolved.date,
            "ambiguous": True,
            "hint": resolved.hint,
            "exists": False,
            "created": False,
            "error": None,
            "collect_since": None,
            "collect_until": None,
            "last_log_ts": None,
            "log_section": None,
            "sign_in_section": None,
        }

    # 2. Ensure journal file exists
    ensure_result = ensure_journal_exists(vault_root, resolved.date)

    if not ensure_result["exists"]:
        return {
            "target_date": resolved.date,
            "ambiguous": False,
            "hint": "",
            "exists": False,
            "created": False,
            "error": ensure_result["message"],
            "collect_since": None,
            "collect_until": None,
            "last_log_ts": None,
            "log_section": None,
            "sign_in_section": None,
        }

    # 3. Read journal and extract activity window
    journal_path = vault_root / "journal" / f"{resolved.date}.md"
    content = journal_path.read_text(encoding="utf-8")
    last_log_ts = get_activity_window(content, journal_date=resolved.date)

    # 4. Extract only the sections needed downstream (Log + Sign-In).
    # The full journal can be huge due to carried-over Running Notes — returning
    # it all wastes context and is never used by synthesis (which uses the
    # activity digest from the collect step instead).
    log_section = ""
    bounds = _get_log_section_bounds(content)
    if bounds:
        log_section = content[bounds[0]:bounds[1]].strip()

    sign_in_section = ""
    si_match = re.search(r"^#\s+\*{0,2}Sign-In\*{0,2}\s*$", content, re.MULTILINE)
    if si_match:
        si_start = si_match.end()
        si_next = re.search(r"^#\s+\*{0,2}[A-Z]", content[si_start:], re.MULTILINE)
        si_end = si_start + si_next.start() if si_next else len(content)
        sign_in_section = content[si_start:si_end].strip()

    # 5. Calculate explicit time range for collection
    #
    # We want ALL activity on the target date, starting from either:
    #   a) The last log entry timestamp (if entries exist — only new activity)
    #   b) Start of the target day (if no entries — full day)
    #
    # The end boundary is:
    #   a) 5 AM the next day (if targeting a past date) — the user's "day" ends
    #      when they go to sleep, not at midnight. Activity before ~5 AM belongs
    #      to the previous day's journal.
    #   b) Now (if targeting today)
    target_date_obj = datetime.strptime(resolved.date, "%Y-%m-%d").date()
    today = user_now().date()

    if last_log_ts:
        collect_since = last_log_ts.isoformat()
    else:
        collect_since = f"{resolved.date}T00:00:00"

    if target_date_obj < today:
        next_day = target_date_obj + timedelta(days=1)
        collect_until = f"{next_day.isoformat()}T05:00:00"
    else:
        collect_until = user_now().strftime("%Y-%m-%dT%H:%M:%S")

    return {
        "target_date": resolved.date,
        "ambiguous": False,
        "hint": "",
        "exists": True,
        "created": ensure_result["created"],
        "collect_since": collect_since,
        "collect_until": collect_until,
        "last_log_ts": last_log_ts.isoformat() if last_log_ts else None,
        "log_section": log_section,
        "sign_in_section": sign_in_section,
        "error": None,
    }



# ---------------------------------------------------------------------------
# Journal writing
# ---------------------------------------------------------------------------

def _parse_time_for_sort(time_str: str) -> float:
    """Parse a time string like '3:49 PM' into minutes since midnight for sorting."""
    try:
        t = datetime.strptime(time_str.strip(), "%I:%M %p")
        return t.hour * 60 + t.minute
    except ValueError:
        return -1  # unparseable → sort to the top


def _get_log_section_bounds(content: str) -> tuple[int, int] | None:
    """Return (start, end) byte offsets of the Log section body.

    start = right after the Log header line
    end = start of the next top-level section (or EOF)
    """
    log_match = re.search(r"^#\s+\*{0,2}Log\*{0,2}\s*$", content, re.MULTILINE)
    if not log_match:
        return None
    log_start = log_match.end()
    next_section = re.search(r"^#\s+\*{0,2}[A-Z]", content[log_start:], re.MULTILINE)
    log_end = log_start + next_section.start() if next_section else len(content)
    return log_start, log_end


def _find_chronological_insertion_point(
    content: str, new_time_str: str,
) -> int | None:
    """Find the byte offset where a new log entry should be inserted chronologically.

    Parses existing timestamped entries in the Log section and finds the correct
    position so entries stay in time order. Non-timestamped entries (e.g., the
    ``<font>`` instruction line, or entries like ``*  - Arrived``) are treated as
    anchored in place and skipped over.

    Returns the offset where the new line should be inserted (before the newline
    of the entry that comes after it chronologically), or the end of the last
    entry if the new entry is the latest.
    """
    bounds = _get_log_section_bounds(content)
    if bounds is None:
        return None

    log_start, log_end = bounds
    log_body = content[log_start:log_end]
    new_minutes = _parse_time_for_sort(new_time_str)

    # Sleep-boundary rule: times between midnight and 5 AM are post-midnight
    # continuation — they are always the latest activity in the day and should
    # be appended to the end, never inserted chronologically by clock time.
    is_post_midnight = 0 <= new_minutes < 5 * 60

    # Parse each line's position and timestamp
    lines = log_body.split("\n")
    last_content_end = log_start  # fallback: after the header

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("<font"):
            continue

        line_abs_start = log_start + sum(len(l) + 1 for l in lines[:i])
        line_abs_end = line_abs_start + len(line)
        last_content_end = line_abs_end

        # Try to extract timestamp from this line
        ts_match = _LOG_TIMESTAMP_RE.match(stripped)
        if ts_match and not is_post_midnight:
            existing_minutes = _parse_time_for_sort(ts_match.group(1))
            if new_minutes >= 0 and existing_minutes > new_minutes:
                # This existing entry is later than our new one — insert before it
                return line_abs_start

    # New entry is the latest (or no timestamped entries exist) — append after last content
    return last_content_end


LOG_TAG = "#wb/journal/log"


def _format_log_entry(time: str, description: str) -> str:
    """Format a single Log entry in the canonical journal format.

    Args:
        time: Time string, e.g. "5:15 PM".
        description: What happened. Should NOT include the tag — it's added automatically.

    Returns:
        Formatted line: ``* 5:15 PM - Description. #wb/journal/log``
    """
    desc = description.rstrip().rstrip(".")
    return f"* {time} - {desc}. {LOG_TAG}"



# Mutex serialising concurrent read-modify-write cycles on journal files.
# The MCP gateway dispatches capability calls via asyncio.to_thread, so
# concurrent sessions invoking append_to_journal run in separate threads.
_journal_write_lock = threading.Lock()


@requires_consent(
    operation="update_journal_entry",
    reason="Appending auto-generated activity entries to the daily journal Log section in Obsidian",
    risk="moderate",
    default_ttl=10,
)
def append_to_journal(
    entries: list[tuple[str, str]],
    vault_root: Path,
    date_str: str | None = None,
) -> dict[str, Any]:
    """Append structured log entries to the Log section of a daily journal file.

    Args:
        entries: List of ``(time, description)`` tuples.
            Example: ``[("5:15 PM", "Started work-buddy development session")]``
            Formatting (bullet, tag) is handled automatically.
        vault_root: Path to the Obsidian vault root.
        date_str: ISO date string (YYYY-MM-DD). Defaults to today.

    Returns:
        Dict with ``success``, ``file``, ``entries_written``, ``message`` keys.
    """
    if date_str is None:
        date_str = user_now().strftime("%Y-%m-%d")

    journal_file = vault_root / "journal" / f"{date_str}.md"

    if not journal_file.exists():
        return {
            "success": False,
            "file": str(journal_file),
            "entries_written": 0,
            "message": f"Journal file for {date_str} does not exist. Run ensure_journal_exists() first, or create the note in Obsidian.",
        }

    if not entries:
        return {
            "success": False,
            "file": str(journal_file),
            "entries_written": 0,
            "message": "No entries provided.",
        }

    # Serialize concurrent read-modify-write cycles. Without this lock,
    # two sessions calling append_to_journal simultaneously will read the
    # same file version, modify independently, and the second write
    # clobbers the first's changes.
    with _journal_write_lock:
        return _append_to_journal_locked(entries, journal_file, date_str)


def _append_to_journal_locked(
    entries: list[tuple[str, str]],
    journal_file: Path,
    date_str: str,
) -> dict[str, Any]:
    """Inner logic for append_to_journal, called under _journal_write_lock."""
    file_content = journal_file.read_text(encoding="utf-8")

    if _get_log_section_bounds(file_content) is None:
        return {
            "success": False,
            "file": str(journal_file),
            "entries_written": 0,
            "message": "Could not find a Log section in the journal file.",
        }

    # Sort new entries by time so we insert earliest first
    sorted_entries = sorted(entries, key=lambda e: _parse_time_for_sort(e[0]))

    # Insert each entry chronologically — re-read content after each insert
    # because offsets shift
    inserted_count = 0
    skipped = []
    for time_str, description in sorted_entries:
        formatted_line = _format_log_entry(time_str, description)
        insertion_point = _find_chronological_insertion_point(file_content, time_str)
        if insertion_point is None:
            skipped.append(time_str)
            continue

        # Determine if we need a newline before/after
        before = file_content[:insertion_point]
        after = file_content[insertion_point:]

        # Ensure we start on a new line
        if before and not before.endswith("\n"):
            formatted_line = "\n" + formatted_line
        # Ensure the following content starts on a new line
        if after and not after.startswith("\n"):
            formatted_line = formatted_line + "\n"

        file_content = before + formatted_line + after
        inserted_count += 1

    if inserted_count == 0:
        return {
            "success": False,
            "file": str(journal_file),
            "entries_written": 0,
            "message": f"No entries inserted — all {len(entries)} entries skipped (no valid insertion points). Skipped times: {skipped}",
        }

    # Write via Obsidian bridge if available (avoids Obsidian auto-save
    # clobbering direct writes), fall back to direct file I/O.
    vault_rel_path = f"journal/{date_str}.md"
    write_method = "direct"
    try:
        from work_buddy.obsidian.bridge import write_file_raw, is_available
        if is_available():
            ok = write_file_raw(vault_rel_path, file_content)
            if ok:
                write_method = "bridge"
            else:
                logger.warning("Bridge write returned False, falling back to direct write")
                journal_file.write_text(file_content, encoding="utf-8")
        else:
            journal_file.write_text(file_content, encoding="utf-8")
    except Exception as exc:
        logger.warning("Bridge write failed (%s), falling back to direct write", exc)
        journal_file.write_text(file_content, encoding="utf-8")

    result = {
        "success": True,
        "file": str(journal_file),
        "entries_written": inserted_count,
        "write_method": write_method,
        "message": f"Appended {inserted_count} entries to Log section of {date_str} journal (via {write_method}).",
    }
    if skipped:
        result["skipped"] = skipped
        result["message"] += f" Skipped {len(skipped)}: {skipped}"
    return result


# ---------------------------------------------------------------------------
# Sign-In extraction and writing
# ---------------------------------------------------------------------------

_DAILYWORKQ_RE = re.compile(
    r"#dailyworkq/(sleep|energy|mood):\s*(\d+\.?\d*)",
)

_CHECKIN_RE = re.compile(
    r"#dailyworkq/check-in:\s*(.+)$", re.MULTILINE,
)

_MOTTO_RE = re.compile(
    r"#dailyworkq/motto:\s*(.+)$", re.MULTILINE,
)

_PLACEHOLDER = "<u>X</u>"


def extract_sign_in(journal_path: Path) -> dict[str, Any]:
    """Extract Sign-In field values from a journal file.

    Returns a dict with keys: sleep, energy, mood (float|None),
    check_in, motto (str|None), and all_filled (bool).
    Fields still containing the ``<u>X</u>`` placeholder or empty
    are returned as None.
    """
    if not journal_path.exists():
        return {
            "sleep": None, "energy": None, "mood": None,
            "check_in": None, "motto": None, "all_filled": False,
        }

    content = journal_path.read_text(encoding="utf-8")

    result: dict[str, Any] = {
        "sleep": None, "energy": None, "mood": None,
        "check_in": None, "motto": None,
    }

    # Numeric metrics
    for m in _DAILYWORKQ_RE.finditer(content):
        result[m.group(1)] = float(m.group(2))

    # Check-in text
    cm = _CHECKIN_RE.search(content)
    if cm:
        val = cm.group(1).strip()
        if val and val != _PLACEHOLDER:
            result["check_in"] = val

    # Motto text
    mm = _MOTTO_RE.search(content)
    if mm:
        val = mm.group(1).strip()
        if val and val != _PLACEHOLDER:
            result["motto"] = val

    result["all_filled"] = all(
        result[k] is not None for k in ("sleep", "energy", "mood", "check_in", "motto")
    )
    return result


def interpret_wellness(cfg: dict[str, Any]) -> str:
    """Produce a compact wellness trend interpretation for agent context.

    Reads the last 14 days of journal sign-in data and returns a 1-3
    sentence summary like:
        "Sleep: 6.5h avg (↓ from 7.1h). Energy: 7.0 (→). Mood: 7.0 (→). 4/14 days missing."

    This is used internally by the agent, not shown to the user.
    """
    from work_buddy.collectors.obsidian_collector import _parse_wellness, _trend_direction

    vault_root = Path(cfg["vault_root"])
    obs_cfg = cfg.get("obsidian", {})
    journal_dir = obs_cfg.get("journal_dir", "journal")
    wellness_days = obs_cfg.get("wellness_days", 14)

    journal_path = vault_root / journal_dir
    if not journal_path.is_dir():
        return "No journal directory found — cannot assess wellness."

    rows = _parse_wellness(journal_path, wellness_days)
    metrics = ["sleep", "energy", "mood"]
    half = min(7, wellness_days // 2)

    parts: list[str] = []
    for metric in metrics:
        recent_vals = [r[metric] for r in rows[:half] if r[metric] is not None]
        prior_vals = [r[metric] for r in rows[half:half * 2] if r[metric] is not None]

        if recent_vals:
            r_avg = sum(recent_vals) / len(recent_vals)
            arrow = _trend_direction(recent_vals, prior_vals)
            unit = "h" if metric == "sleep" else ""
            if prior_vals:
                p_avg = sum(prior_vals) / len(prior_vals)
                parts.append(f"{metric.title()}: {r_avg:.1f}{unit} {arrow} (from {p_avg:.1f}{unit})")
            else:
                parts.append(f"{metric.title()}: {r_avg:.1f}{unit} (no prior data)")
        else:
            parts.append(f"{metric.title()}: no data")

    # Count missing days
    missing = sum(1 for r in rows if all(r[m] is None for m in metrics))
    parts.append(f"{missing}/{wellness_days} days missing")

    return ". ".join(parts) + "."


@requires_consent(
    operation="morning.write_sign_in",
    reason="Write sign-in responses (sleep, energy, mood, check-in, motto) to today's journal.",
    risk="moderate",
    default_ttl=15,
)
def write_sign_in(journal_path: Path, fields: dict[str, Any]) -> dict[str, Any]:
    """Write sign-in field values to the journal file.

    Only writes non-None fields. Skips fields that are already filled.

    Args:
        journal_path: Path to the journal markdown file.
        fields: Dict with optional keys: sleep, energy, mood (numeric),
                check_in, motto (str).

    Returns:
        Dict with ``success``, ``fields_written`` (list), and ``path``.
    """
    if not journal_path.exists():
        raise FileNotFoundError(f"Journal file not found: {journal_path}")

    content = journal_path.read_text(encoding="utf-8")
    written: list[str] = []

    # Write numeric metrics (replace value after the tag)
    for metric in ("sleep", "energy", "mood"):
        val = fields.get(metric)
        if val is None:
            continue
        pattern = re.compile(
            rf"(#dailyworkq/{metric}:\s*)(\S*)",
        )
        m = pattern.search(content)
        if m:
            existing = m.group(2).strip()
            if existing and existing != _PLACEHOLDER:
                logger.debug("Sign-in %s already filled (%s), skipping", metric, existing)
                continue
            content = content[:m.start(2)] + str(val) + content[m.end(2):]
            written.append(metric)

    # Write text fields (replace <u>X</u> placeholder)
    for field, tag in (("check_in", "check-in"), ("motto", "motto")):
        val = fields.get(field)
        if val is None:
            continue
        pattern = re.compile(
            rf"(#dailyworkq/{tag}:\s*){re.escape(_PLACEHOLDER)}",
        )
        m = pattern.search(content)
        if m:
            content = content[:m.start(1)] + m.group(1) + val + content[m.end():]
            written.append(field)
        else:
            # Check if already filled (no placeholder to replace)
            if f"#dailyworkq/{tag}:" in content:
                logger.debug("Sign-in %s already filled, skipping", field)

    if written:
        journal_path.write_text(content, encoding="utf-8")
        logger.info("Wrote sign-in fields %s to %s", written, journal_path)

    return {"success": True, "fields_written": written, "path": journal_path.as_posix()}


# ---------------------------------------------------------------------------
# Briefing callout formatting and journal persistence
# ---------------------------------------------------------------------------

# Pattern to detect an existing briefing callout block (greedy across lines)
_BRIEFING_CALLOUT_RE = re.compile(
    r"^> \[!briefing\].*?\n(?:>.*\n)*",
    re.MULTILINE,
)

# Section heading pattern (any level, with optional bold markers)
_HEADING_RE = re.compile(r"^(#{1,6})\s+\**(.+?)\**\s*$", re.MULTILINE)


def format_briefing_callout(briefing_md: str, date_str: str) -> str:
    """Wrap a briefing in a collapsed Obsidian callout block.

    The callout header includes the work-buddy commit hash and generation
    timestamp for traceability — the reader can tell which version of
    work-buddy produced the briefing and whether it was generated in the
    morning or after the fact.

    Args:
        briefing_md: The briefing body (from ``morning.format_briefing``).
        date_str: ISO date for the heading.

    Returns:
        Full callout block ready for journal insertion.
    """
    commit = get_wb_commit_hash()
    now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    header = f"> [!briefing]- Morning Briefing #wb/briefing (`{commit}` {now_str})"
    body_lines = [f"> {line}" if line.strip() else ">" for line in briefing_md.split("\n")]
    return header + "\n" + "\n".join(body_lines)


def _find_sign_in_end(content: str) -> int | None:
    """Find the byte offset where the Sign-In section ends.

    Returns the position just before the next heading after Sign-In,
    or None if Sign-In is not found.
    """
    for m in _HEADING_RE.finditer(content):
        heading_text = m.group(2).strip()
        if heading_text.lower() == "sign-in":
            # Find the next heading after this one
            search_start = m.end()
            next_heading = _HEADING_RE.search(content, search_start)
            if next_heading:
                return next_heading.start()
            return len(content)
    return None


@requires_consent(
    operation="morning.persist_briefing",
    reason="Write morning briefing callout to today's journal Sign-In section.",
    risk="moderate",
    default_ttl=15,
)
def persist_briefing_to_journal(
    briefing_md: str,
    vault_root: str,
    date_str: str,
) -> dict[str, Any]:
    """Insert or replace a ``[!briefing]`` callout in the journal Sign-In section.

    Idempotent: if a briefing callout already exists it is replaced in-place.

    Args:
        briefing_md: The formatted briefing body (from ``morning.format_briefing``).
        vault_root: Path to the Obsidian vault root.
        date_str: Target date as YYYY-MM-DD.

    Returns:
        Dict with ``success``, ``action`` ("inserted" | "replaced"), and ``path``.

    Raises:
        FileNotFoundError: If the journal file does not exist.
    """
    journal_path = Path(vault_root) / "journal" / f"{date_str}.md"
    if not journal_path.exists():
        raise FileNotFoundError(f"Journal file not found: {journal_path}")

    content = journal_path.read_text(encoding="utf-8")
    callout = format_briefing_callout(briefing_md, date_str)

    # Check for existing callout — replace if found
    existing = _BRIEFING_CALLOUT_RE.search(content)
    if existing:
        new_content = content[:existing.start()] + callout + "\n" + content[existing.end():]
        journal_path.write_text(new_content, encoding="utf-8")
        logger.info("Replaced existing briefing callout in %s", journal_path)
        return {"success": True, "action": "replaced", "path": journal_path.as_posix()}

    # No existing callout — insert before the next section after Sign-In
    insert_pos = _find_sign_in_end(content)
    if insert_pos is None:
        logger.warning("Sign-In section not found in %s — appending at end", journal_path)
        insert_pos = len(content)

    new_content = content[:insert_pos] + callout + "\n\n" + content[insert_pos:]
    journal_path.write_text(new_content, encoding="utf-8")
    logger.info("Inserted briefing callout in %s", journal_path)
    return {"success": True, "action": "inserted", "path": journal_path.as_posix()}

