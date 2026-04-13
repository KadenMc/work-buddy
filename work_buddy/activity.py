"""Structured activity timeline: parse journal entries and infer recent activity.

Provides a composable ``infer_activity()`` primitive that returns a typed
``ActivityTimeline`` from journal entries (shallow) and optionally deeper
signals like git commits (deep mode).  Multiple workflows can consume this
instead of re-inventing "what happened recently?" from raw markdown.

Phase 1: journal parsing + shallow timeline + gap analysis + formatting.
Phase 2+: deep mode with git, chat, vault, message sources.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from work_buddy.journal import (
    _LOG_TIMESTAMP_RE,
    _get_log_section_bounds,
    journal_path_for_date,
    user_now,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class EventSource(Enum):
    """Where an activity event originated."""

    JOURNAL_MANUAL = "journal_manual"
    JOURNAL_AGENT = "journal_agent"
    GIT_COMMIT = "git_commit"
    CHAT_SESSION = "chat_session"
    VAULT_EDIT = "vault_edit"
    MESSAGE = "message"
    CALENDAR = "calendar"
    CHROME_TAB = "chrome_tab"
    TASK = "task"
    MCP_GATEWAY = "mcp_gateway"


@dataclass
class ActivityEvent:
    """A unified event from any tracked source."""

    timestamp: datetime
    source: EventSource
    summary: str
    metadata: dict = field(default_factory=dict)


@dataclass
class JournalEntry:
    """A single parsed Log line from a journal file."""

    timestamp: datetime
    description: str
    source: EventSource  # JOURNAL_MANUAL or JOURNAL_AGENT
    tags: list[str] = field(default_factory=list)
    raw_line: str = ""
    incomplete: bool = False  # True when #wb/TODO is present
    related_artifacts: list[ActivityEvent] = field(default_factory=list)


@dataclass
class ActivityGap:
    """A period with no recorded activity."""

    start: datetime
    end: datetime
    duration_minutes: float


@dataclass
class ActivityTimeline:
    """Result of ``infer_activity()``: sorted events + gap analysis."""

    events: list[ActivityEvent]
    journal_entries: list[JournalEntry]
    gaps: list[ActivityGap]
    window_start: datetime
    window_end: datetime
    deep: bool


# ---------------------------------------------------------------------------
# Journal log parsing
# ---------------------------------------------------------------------------

# Extracts bullet char, time, and everything after the separator dash.
_LOG_LINE_RE = re.compile(
    r"^([\*\-])\s+"  # bullet: * or -
    r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))"  # time
    r"\s*-\s*"  # separator dash
    r"(.+)$",  # rest of line
)

_TAG_RE = re.compile(r"#\S+")


def parse_journal_log(
    journal_content: str,
    journal_date: str,
) -> list[JournalEntry]:
    """Parse all Log section lines into ``JournalEntry`` objects.

    Args:
        journal_content: Full text of the journal file.
        journal_date: ISO date string (YYYY-MM-DD).

    Returns:
        List of entries, sorted by timestamp.
    """
    bounds = _get_log_section_bounds(journal_content)
    if bounds is None:
        return []

    log_start, log_end = bounds
    log_body = journal_content[log_start:log_end]
    date_obj = datetime.strptime(journal_date, "%Y-%m-%d").date()
    entries: list[JournalEntry] = []

    for line in log_body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        match = _LOG_LINE_RE.match(stripped)
        if not match:
            continue

        bullet, time_str, rest = match.group(1), match.group(2), match.group(3)

        # Parse timestamp
        try:
            time_obj = datetime.strptime(time_str.strip(), "%I:%M %p").time()
        except ValueError:
            continue
        timestamp = datetime.combine(date_obj, time_obj)

        # Extract tags and clean description
        tags = _TAG_RE.findall(rest)
        description = _TAG_RE.sub("", rest).strip().rstrip(".")

        # Determine source
        has_log_tag = "#wb/journal/log" in tags
        is_asterisk = bullet == "*"
        if is_asterisk or has_log_tag:
            source = EventSource.JOURNAL_AGENT
        else:
            source = EventSource.JOURNAL_MANUAL

        # Check for incomplete marker
        incomplete = any("#wb/TODO" in t for t in tags)

        entries.append(
            JournalEntry(
                timestamp=timestamp,
                description=description,
                source=source,
                tags=tags,
                raw_line=stripped,
                incomplete=incomplete,
            )
        )

    entries.sort(key=lambda e: e.timestamp)
    return entries


# ---------------------------------------------------------------------------
# Activity inference
# ---------------------------------------------------------------------------


def _normalize_dt(value: datetime | str) -> datetime:
    """Convert ISO string or datetime to a naive datetime."""
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _journal_entry_to_event(entry: JournalEntry) -> ActivityEvent:
    """Convert a JournalEntry to a generic ActivityEvent."""
    return ActivityEvent(
        timestamp=entry.timestamp,
        source=entry.source,
        summary=entry.description,
        metadata={"tags": entry.tags, "incomplete": entry.incomplete},
    )


def infer_activity(
    since: datetime | str,
    until: datetime | str | None = None,
    deep: bool = False,
    target_date: str | None = None,
) -> ActivityTimeline:
    """Infer recent activity from journal entries and optionally deeper signals.

    Args:
        since: Start of the activity window (datetime or ISO string).
        until: End of the window. Defaults to now.
        deep: If True, also collect git/chat/vault signals (Phase 2+).
        target_date: Journal date YYYY-MM-DD. Inferred from ``since`` if omitted.

    Returns:
        ActivityTimeline with events, journal entries, and gap analysis.
    """
    since_dt = _normalize_dt(since)
    until_dt = _normalize_dt(until) if until else user_now().replace(tzinfo=None)

    # Determine which journal dates to read
    if target_date:
        dates = [target_date]
    else:
        dates = _date_range(since_dt, until_dt)

    # Parse journal entries from all relevant dates
    all_entries: list[JournalEntry] = []
    for date_str in dates:
        journal_file = journal_path_for_date(date_str)
        if not journal_file.exists():
            continue
        content = journal_file.read_text(encoding="utf-8")
        entries = parse_journal_log(content, date_str)
        all_entries.extend(entries)

    # Filter to the requested window
    all_entries = [
        e for e in all_entries if since_dt <= e.timestamp <= until_dt
    ]

    # Convert to events
    events = [_journal_entry_to_event(e) for e in all_entries]

    # Deep mode: git, chat, vault, chrome, ledger
    if deep:
        events.extend(_collect_git_events(since_dt, until_dt))
        events.extend(_collect_chat_events(since_dt, until_dt))
        events.extend(_collect_vault_events(since_dt, until_dt))
        events.extend(_collect_chrome_events(since_dt, until_dt))
        events.extend(_collect_task_events(since_dt, until_dt))
        events.extend(_collect_ledger_events(since_dt, until_dt))

    events.sort(key=lambda e: e.timestamp)
    gaps = _compute_gaps(events, since_dt, until_dt)

    return ActivityTimeline(
        events=events,
        journal_entries=all_entries,
        gaps=gaps,
        window_start=since_dt,
        window_end=until_dt,
        deep=deep,
    )


def _date_range(start: datetime, end: datetime) -> list[str]:
    """Return list of YYYY-MM-DD strings spanning the window."""
    dates = []
    current = start.date()
    end_date = end.date()
    while current <= end_date:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


# ---------------------------------------------------------------------------
# Deep collection stubs (Phase 2+)
# ---------------------------------------------------------------------------


def _collect_git_events(
    since: datetime, until: datetime,
) -> list[ActivityEvent]:
    """Collect git commits across all repos as ActivityEvents."""
    from work_buddy.collectors.git_collector import _discover_repos, _get_recent_commits
    from work_buddy.config import load_config

    cfg = load_config()
    repos_root = Path(cfg["repos_root"])
    repos = _discover_repos(repos_root)

    since_iso = since.isoformat()
    until_iso = until.isoformat()
    events: list[ActivityEvent] = []

    for repo_path in repos:
        repo_name = repo_path.name
        raw = _get_recent_commits(
            repo_path, since_days=0, max_commits=200,
            since=since_iso, until=until_iso,
        )
        if not raw:
            continue

        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # Format: %aI %h %s → "2026-04-04T14:11:00-04:00 3cfc71d Fix task ID placement"
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            ts_str, commit_hash, subject = parts
            try:
                ts = datetime.fromisoformat(ts_str)
                # Convert to user's local timezone, then strip tzinfo for
                # consistency with journal entries (which are naive local times)
                if ts.tzinfo is not None:
                    from work_buddy.config import USER_TZ
                    ts = ts.astimezone(USER_TZ).replace(tzinfo=None)
            except ValueError:
                continue

            events.append(ActivityEvent(
                timestamp=ts,
                source=EventSource.GIT_COMMIT,
                summary=f"[{repo_name}] {subject}",
                metadata={"repo": repo_name, "hash": commit_hash},
            ))

    return events


def _collect_chat_events(
    since: datetime, until: datetime,
) -> list[ActivityEvent]:
    """Collect Claude Code chat sessions as ActivityEvents."""
    from work_buddy.collectors.chat_collector import _get_claude_code_conversations
    from work_buddy.config import load_config

    cfg = load_config()
    project_filter = cfg.get("chats", {}).get("project_filter")

    sessions = _get_claude_code_conversations(
        days=0, project_filter=project_filter,
        since=since.isoformat(), until=until.isoformat(),
    )

    events: list[ActivityEvent] = []
    for sess in sessions:
        # Parse start_time for the event timestamp
        ts_str = sess.get("start_time")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                from work_buddy.config import USER_TZ
                ts = ts.astimezone(USER_TZ).replace(tzinfo=None)
        except ValueError:
            continue

        # Filter: skip sessions whose start_time falls outside the window
        if ts < since or ts > until:
            continue

        # Build summary — clean up the first user message for display
        project = sess.get("project_name", "unknown")
        raw_topic = sess.get("first_user_message", "")
        # Strip XML/command tags and collapse whitespace
        topic = re.sub(r"<[^>]+>", "", raw_topic).strip()
        topic = re.sub(r"\s+", " ", topic)[:100]
        tool_count = sess.get("tool_use_count", 0)
        duration = ""
        if sess.get("start_time") and sess.get("end_time"):
            from work_buddy.collectors.chat_collector import _format_duration
            duration = _format_duration(sess["start_time"], sess["end_time"])

        summary_parts = [f"[{project}]"]
        if topic:
            summary_parts.append(topic)
        detail_parts = []
        if tool_count:
            detail_parts.append(f"{tool_count} tool calls")
        if duration:
            detail_parts.append(duration)
        if detail_parts:
            summary_parts.append(f"({', '.join(detail_parts)})")

        events.append(ActivityEvent(
            timestamp=ts,
            source=EventSource.CHAT_SESSION,
            summary=" ".join(summary_parts),
            metadata={
                "session_id": sess.get("full_session_id", sess.get("session_id", "")),
                "project": project,
                "tool_count": tool_count,
                "tool_names": sess.get("tool_names", {}),
            },
        ))

    return events


def _collect_vault_events(
    since: datetime, until: datetime,
) -> list[ActivityEvent]:
    """Collect recently modified vault files as ActivityEvents.

    Uses the vault event ledger (event-driven) when available, falls back
    to O(n) mtime scanning. Enriches with KTR writing intensity data.
    """
    # Try event-driven ledger first
    ledger_files = _get_ledger_recent(since, until)

    if ledger_files is not None:
        files = ledger_files
    else:
        # Fallback: mtime scanning
        from work_buddy.collectors.obsidian_collector import _get_recent_files
        from work_buddy.config import load_config

        cfg = load_config()
        vault_root = Path(cfg["vault_root"])
        exclude_folders = cfg.get("obsidian", {}).get("exclude_folders", [])
        exclude_folders = list(set(exclude_folders) | {"journal"})

        raw = _get_recent_files(
            vault_root, days=0, exclude_folders=exclude_folders,
            since=since.isoformat(), until=until.isoformat(),
        )
        files = []
        for f in raw:
            try:
                # _get_recent_files formats mtime as UTC — convert to local
                from datetime import timezone as _tz
                ts_utc = datetime.strptime(f["modified"], "%Y-%m-%d %H:%M").replace(
                    tzinfo=_tz.utc
                )
                from work_buddy.config import USER_TZ
                ts = ts_utc.astimezone(USER_TZ).replace(tzinfo=None)
            except (ValueError, KeyError):
                continue
            files.append({"path": f["path"].replace("\\", "/"), "ts": ts})

    # Try to get KTR hot-file data for enrichment
    ktr_scores = _get_ktr_scores(since, until)

    events: list[ActivityEvent] = []
    for f in files:
        file_path = f["path"]
        if _is_junk_path(file_path):
            continue
        ts = f["ts"]
        metadata: dict = {"file": file_path}

        # Enrich with KTR writing intensity if available
        ktr = ktr_scores.get(file_path)
        if ktr:
            metadata["hot_score"] = ktr["hot_score"]
            metadata["active_days"] = ktr["active_days"]
            metadata["total_buckets"] = ktr["total_buckets"]
            metadata["total_word_delta"] = ktr["total_word_delta"]

        events.append(ActivityEvent(
            timestamp=ts,
            source=EventSource.VAULT_EDIT,
            summary=file_path,
            metadata=metadata,
        ))

    return events


def _get_ledger_recent(
    since: datetime, until: datetime,
) -> list[dict] | None:
    """Try to get recent files from the vault event ledger. Returns None on failure."""
    try:
        from work_buddy.obsidian.vault_events import bootstrap, get_recent_files

        # Ensure ledger is active (idempotent)
        bootstrap()

        since_hours = max(0.1, (until - since).total_seconds() / 3600)
        result = get_recent_files(
            since_hours=since_hours, limit=100,
            exclude_folders=["journal"],
        )

        files = []
        for f in result.get("files", []):
            try:
                ts = datetime.fromisoformat(
                    f["last_modified"].replace("Z", "+00:00")
                )
                if ts.tzinfo is not None:
                    from work_buddy.config import USER_TZ
                    ts = ts.astimezone(USER_TZ).replace(tzinfo=None)
            except (ValueError, KeyError):
                continue
            files.append({"path": f["path"], "ts": ts})

        return files
    except Exception:
        return None


def _collect_chrome_events(
    since: datetime, until: datetime,
) -> list[ActivityEvent]:
    """Collect Chrome tab changes as ActivityEvents from the rolling ledger.

    Diffs consecutive snapshots to find tab opens, closes, and navigations,
    then synthesizes browsing session summaries by domain.
    """
    try:
        from work_buddy.collectors.chrome_ledger import get_tab_changes, get_tab_sessions

        changes = get_tab_changes(since, until)
        sessions = get_tab_sessions(since, until)
    except Exception:
        return []

    events: list[ActivityEvent] = []

    # Individual tab opens
    for tab in changes.get("opened", []):
        url = tab.get("url", "")
        title = tab.get("title", "")
        time_str = tab.get("time", "")
        if not time_str:
            continue
        try:
            ts = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                from work_buddy.config import USER_TZ
                ts = ts.astimezone(USER_TZ).replace(tzinfo=None)
        except ValueError:
            continue

        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        display = f"Opened {domain}"
        if title:
            display += f" — '{title[:60]}'"

        events.append(ActivityEvent(
            timestamp=ts,
            source=EventSource.CHROME_TAB,
            summary=display,
            metadata={"url": url, "title": title, "action": "opened"},
        ))

    # Browsing session summaries (domain clusters with significant activity)
    for sess in sessions.get("sessions", []):
        if sess.get("estimated_minutes", 0) < 10:
            continue  # Skip trivial presence
        domain = sess.get("domain", "")
        pages = sess.get("page_count", 0)
        minutes = sess.get("estimated_minutes", 0)
        titles = sess.get("sample_titles", [])

        first_seen = sess.get("first_seen", "")
        if not first_seen:
            continue
        try:
            ts = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                from work_buddy.config import USER_TZ
                ts = ts.astimezone(USER_TZ).replace(tzinfo=None)
        except ValueError:
            continue

        summary = f"Browsing session: {minutes} min on {domain} ({pages} pages)"
        if titles:
            summary += f" — e.g. '{titles[0][:50]}'"

        events.append(ActivityEvent(
            timestamp=ts,
            source=EventSource.CHROME_TAB,
            summary=summary,
            metadata={
                "domain": domain,
                "page_count": pages,
                "estimated_minutes": minutes,
                "action": "session",
            },
        ))

    return events


def _collect_task_events(
    since: datetime, until: datetime,
) -> list[ActivityEvent]:
    """Collect task state changes from the SQLite store as ActivityEvents."""
    try:
        from work_buddy.obsidian.tasks.store import get_events_in_range

        raw = get_events_in_range(since.isoformat(), until.isoformat())
    except Exception:
        return []

    events: list[ActivityEvent] = []
    for evt in raw:
        ts_str = evt.get("changed_at", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue

        old = evt.get("old_state") or "new"
        new = evt["new_state"]
        tid = evt["task_id"]
        reason = evt.get("reason", "")
        desc = f"{tid}: {old} \u2192 {new}"
        if reason and reason not in ("created", "toggled", "archived", "deleted"):
            desc += f" ({reason})"

        events.append(ActivityEvent(
            timestamp=ts,
            source=EventSource.TASK,
            summary=desc,
            metadata={
                "task_id": tid,
                "old_state": old,
                "new_state": new,
                "reason": reason,
            },
        ))
    return events


def _collect_ledger_events(
    since: datetime, until: datetime,
) -> list[ActivityEvent]:
    """Collect MCP gateway activity from the session activity ledger."""
    try:
        from work_buddy.mcp_server.activity_ledger import query_activity
        result = query_activity(last_n=100, include_searches=False)
    except Exception:
        return []

    events: list[ActivityEvent] = []
    for ev in result.get("events", []):
        ts_str = ev.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str).replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if ts < since or ts > until:
            continue

        ev_type = ev.get("type", "")
        if ev_type == "capability_invoked":
            cap = ev.get("capability", "?")
            cat = ev.get("category", "?")
            status = ev.get("status", "ok")
            dur = ev.get("duration_ms", 0)
            summary = f"wb_run({cap}) [{cat}] {status} ({dur}ms)"
            rs = ev.get("result_summary") or {}
            # Append key artifact if present
            for key in ("task_id", "entry_count", "slug"):
                val = rs.get(key)
                if val:
                    summary += f" → {key}={val}"
        elif ev_type == "workflow_started":
            wf = ev.get("workflow_name", "?")
            steps = ev.get("step_count", "?")
            summary = f"workflow started: {wf} ({steps} steps)"
        elif ev_type == "workflow_step_completed":
            step = ev.get("step_id") or ev.get("step_name") or "?"
            summary = f"workflow step: {step}"
        else:
            continue

        events.append(ActivityEvent(
            timestamp=ts,
            source=EventSource.MCP_GATEWAY,
            summary=summary,
            metadata={k: v for k, v in ev.items() if k != "ts"},
        ))
    return events


_JUNK_EXTENSIONS = {".pyc", ".pyo", ".tmp", ".bak", ".db-wal", ".db-shm"}
_JUNK_SEGMENTS = {"__pycache__", "node_modules", ".git", "logs", "agents"}


def _is_junk_path(path: str) -> bool:
    """Filter out binary, cache, temp, log, and agent files."""
    for ext in _JUNK_EXTENSIONS:
        if path.endswith(ext) or ext + "." in path:
            return True
    for seg in _JUNK_SEGMENTS:
        if seg + "/" in path or path.startswith(seg + "/"):
            return True
    if ".tmp." in path or path.endswith("~") or path.endswith(".log"):
        return True
    return False


def _get_ktr_scores(since: datetime, until: datetime) -> dict[str, dict]:
    """Try to fetch KTR hot-file scores. Returns empty dict on failure."""
    try:
        from work_buddy.obsidian.ktr import get_hot_files
        result = get_hot_files(
            since_date=since.strftime("%Y-%m-%d"),
            until_date=until.strftime("%Y-%m-%d"),
            limit=100,
        )
        return {f["filePath"]: f for f in result.get("files", [])}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------------


def _compute_gaps(
    events: list[ActivityEvent],
    window_start: datetime,
    window_end: datetime,
    min_gap_minutes: float = 30,
) -> list[ActivityGap]:
    """Find periods with no events longer than the threshold."""
    if not events:
        duration = (window_end - window_start).total_seconds() / 60
        if duration > min_gap_minutes:
            return [ActivityGap(window_start, window_end, duration)]
        return []

    gaps: list[ActivityGap] = []
    sorted_events = sorted(events, key=lambda e: e.timestamp)

    # Gap before first event
    first_gap = (sorted_events[0].timestamp - window_start).total_seconds() / 60
    if first_gap > min_gap_minutes:
        gaps.append(ActivityGap(window_start, sorted_events[0].timestamp, first_gap))

    # Gaps between consecutive events
    for i in range(len(sorted_events) - 1):
        gap_min = (
            sorted_events[i + 1].timestamp - sorted_events[i].timestamp
        ).total_seconds() / 60
        if gap_min > min_gap_minutes:
            gaps.append(
                ActivityGap(
                    sorted_events[i].timestamp,
                    sorted_events[i + 1].timestamp,
                    gap_min,
                )
            )

    # Gap after last event
    last_gap = (window_end - sorted_events[-1].timestamp).total_seconds() / 60
    if last_gap > min_gap_minutes:
        gaps.append(ActivityGap(sorted_events[-1].timestamp, window_end, last_gap))

    return gaps


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_relative_time(
    dt: datetime, reference: datetime | None = None,
) -> str:
    """Format like ``9:40 AM (3 hours ago)`` or ``4:42 PM (2 days ago)``."""
    if reference is None:
        reference = user_now().replace(tzinfo=None)

    # Windows-safe time formatting
    time_str = dt.strftime("%I:%M %p").lstrip("0")

    delta = reference - dt
    total_seconds = delta.total_seconds()

    if total_seconds < 0:
        relative = "in the future"
    elif total_seconds < 60:
        relative = "just now"
    elif total_seconds < 3600:
        mins = int(total_seconds / 60)
        relative = f"{mins} min{'s' if mins != 1 else ''} ago"
    elif total_seconds < 86400:
        hours = int(total_seconds / 3600)
        relative = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        days = delta.days
        relative = f"{days} day{'s' if days != 1 else ''} ago"

    return f"{time_str} ({relative})"


def format_timeline(timeline: ActivityTimeline) -> str:
    """Render an ActivityTimeline as markdown for agent consumption."""
    now = user_now().replace(tzinfo=None)
    lines: list[str] = []

    window_start_str = format_relative_time(timeline.window_start, now)
    window_end_str = format_relative_time(timeline.window_end, now)
    lines.append(f"## Activity Timeline: {window_start_str} to {window_end_str}")
    lines.append(f"**Mode:** {'deep' if timeline.deep else 'shallow (journal only)'}")
    lines.append("")

    # Group events by source
    by_source: dict[EventSource, list[ActivityEvent]] = {}
    for event in timeline.events:
        by_source.setdefault(event.source, []).append(event)

    # Journal entries first
    journal_sources = {EventSource.JOURNAL_MANUAL, EventSource.JOURNAL_AGENT}
    journal_events = []
    for src in journal_sources:
        journal_events.extend(by_source.pop(src, []))
    journal_events.sort(key=lambda e: e.timestamp)

    if journal_events:
        lines.append("### Journal Log entries")
        for event in journal_events:
            time_str = format_relative_time(event.timestamp, now)
            source_tag = "agent" if event.source == EventSource.JOURNAL_AGENT else "manual"
            incomplete_tag = " [INCOMPLETE]" if event.metadata.get("incomplete") else ""
            lines.append(f"- {time_str} [{source_tag}]{incomplete_tag} {event.summary}")
        lines.append("")

    # Other sources
    source_labels = {
        EventSource.GIT_COMMIT: "Git commits",
        EventSource.CHAT_SESSION: "Chat sessions",
        EventSource.VAULT_EDIT: "Vault edits",
        EventSource.MESSAGE: "Messages",
        EventSource.CALENDAR: "Calendar",
        EventSource.CHROME_TAB: "Chrome browsing",
        EventSource.TASK: "Task changes",
        EventSource.MCP_GATEWAY: "Work-buddy MCP activity",
    }

    for source, events in by_source.items():
        label = source_labels.get(source, source.value)
        lines.append(f"### {label}")
        for event in sorted(events, key=lambda e: e.timestamp):
            time_str = format_relative_time(event.timestamp, now)
            suffix = ""
            # Show KTR writing intensity for vault edits
            if source == EventSource.VAULT_EDIT and event.metadata.get("hot_score"):
                hs = event.metadata["hot_score"]
                wd = event.metadata.get("total_word_delta", 0)
                suffix = f" (hot: {hs:.0f}, {wd}w)"
            lines.append(f"- {time_str} {event.summary}{suffix}")
        lines.append("")

    # Gaps
    if timeline.gaps:
        lines.append("### Gaps (no activity)")
        for gap in timeline.gaps:
            start_str = format_relative_time(gap.start, now)
            dur = int(gap.duration_minutes)
            hours, mins = divmod(dur, 60)
            if hours:
                dur_str = f"{hours}h {mins}m" if mins else f"{hours}h"
            else:
                dur_str = f"{mins}m"
            lines.append(f"- {start_str}: {dur_str} gap")
        lines.append("")

    # Summary
    lines.append(f"**Total:** {len(timeline.events)} events, "
                 f"{len(timeline.gaps)} gaps, "
                 f"{len(timeline.journal_entries)} journal entries")

    return "\n".join(lines)
