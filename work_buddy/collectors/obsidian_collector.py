"""Collect Obsidian vault context: journal entries, recent files, and tasks."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# Sections we want to extract from daily journal files
JOURNAL_SECTIONS = [
    "Sign-In",
    "Tasks & Objectives",
    "WOOP",
    "Hard Thing First",
    "Most Important Tasks",
    "Irreversible Micro-Decision",
    "Log",
    "Running Notes / Considerations",
    "Recon",
    "Sign-Off",
    "Reflection",
    "AAR",
]

# Patterns to strip from extracted content
_TEMPLATER_BLOCK = re.compile(r"<%[\s\S]*?%>", re.MULTILINE)
_DATAVIEW_BLOCK = re.compile(r"```(?:dataview|tracker|custom-frames)[\s\S]*?```", re.MULTILINE)
_SERIALIZED_QUERY = re.compile(r"<!-- SerializedQuery:[\s\S]*?SerializedQuery END -->", re.MULTILINE)
_QUERY_TO_SERIALIZE = re.compile(r"<!-- QueryToSerialize:[\s\S]*?-->", re.MULTILINE)
_PLACEHOLDER = re.compile(r"<u>X</u>")
_FONT_TAG = re.compile(r"<font[^>]*>.*?</font>", re.DOTALL)


def _extract_sections(content: str) -> dict[str, str]:
    """Extract named sections from a daily journal markdown file.

    Returns a dict of section_name -> cleaned content.
    Only includes sections that have meaningful content (not just placeholders).
    """
    # Strip templater and dataview blocks first
    content = _TEMPLATER_BLOCK.sub("", content)
    content = _DATAVIEW_BLOCK.sub("", content)
    content = _SERIALIZED_QUERY.sub("", content)
    content = _QUERY_TO_SERIALIZE.sub("", content)
    content = _FONT_TAG.sub("", content)

    sections = {}
    # Split on markdown headers
    header_pattern = re.compile(r"^(#{1,6})\s+\**(.+?)\**\s*$", re.MULTILINE)
    matches = list(header_pattern.finditer(content))

    for i, match in enumerate(matches):
        title = match.group(2).strip()
        # Check if this matches any section we care about
        matched_section = None
        for sec in JOURNAL_SECTIONS:
            if sec.lower() in title.lower():
                matched_section = sec
                break

        if matched_section is None:
            continue

        # Get content between this header and the next
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()

        # Strip placeholders and template markers to check for real content
        cleaned = _PLACEHOLDER.sub("", body)
        cleaned = re.sub(r"\[\[.*?\]\]", "", cleaned)  # wikilinks
        cleaned = re.sub(r"#\S+", "", cleaned)  # tags
        cleaned = re.sub(r"\*+", "", cleaned)  # bold/italic markers
        cleaned = re.sub(r"[-\d)(]+\s*(?:Select|Once done|delete the).*", "", cleaned)  # template instructions
        cleaned = re.sub(r"How many hours.*?\?|How's my.*?\?|Brief check-in|Mindful Motto", "", cleaned)
        cleaned = re.sub(r"Task:|Definition of Done \(DoD\):|Wish.*?:|Outcome:|Obstacle.*?:|Plan:", "", cleaned)
        cleaned = re.sub(r"Snapshot.*?:|Thought Challenge.*?:|Gratitude:|Avoidance noticed:|Next Right Action:", "", cleaned)
        cleaned = re.sub(r"What worked:|Where I avoided:|Constraint for tomorrow:|Tomorrow's HTF-1 task:|Ship Ugly-1:", "", cleaned)
        cleaned = re.sub(r"Novelty threat\?.*", "", cleaned)
        cleaned = re.sub(r"[:\-\s\d.>|]+", " ", cleaned).strip()

        if not cleaned or len(cleaned) < 10:
            continue

        # Also clean the body itself: remove unfilled placeholder lines
        body_lines = []
        for line in body.split("\n"):
            # Skip lines that are purely template prompts with <u>X</u>
            if "<u>X</u>" in line:
                continue
            # Skip empty template prompt lines (e.g., "How's my energy (1-10)? #tag: ")
            if re.match(r"^.*\?\s*#\S+:\s*$", line):
                continue
            # Skip dataview/template instruction lines
            if re.match(r"^\d+\)\s*(Select|Once done|delete the)", line.strip()):
                continue
            body_lines.append(line)

        clean_body = "\n".join(body_lines).strip()
        if clean_body:
            sections[matched_section] = clean_body

    return sections


def _get_journal_entries(
    vault_root: Path,
    journal_dir: str,
    days: int,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Read recent daily journal files and extract sections.

    If ``since``/``until`` are provided, they override ``days`` and
    determine the date range of journal files to read.
    """
    journal_path = vault_root / journal_dir
    if not journal_path.is_dir():
        return []

    today = datetime.now().date()

    if since:
        start_date = datetime.fromisoformat(since).date()
    else:
        start_date = today - timedelta(days=days - 1)

    end_date = datetime.fromisoformat(until).date() if until else today

    entries = []
    current = end_date
    while current >= start_date:
        file_path = journal_path / f"{current.isoformat()}.md"
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8", errors="replace")
            sections = _extract_sections(content)
            if sections:
                entries.append({
                    "date": current.isoformat(),
                    "sections": sections,
                })
        current -= timedelta(days=1)

    return entries


def _walk_vault(vault_root: Path, exclude_set: set[str]):
    """Walk the vault, skipping excluded directories entirely.

    Yields Path objects for .md files. Uses os.walk instead of rglob
    to skip excluded subtrees early and avoid Windows long-path errors.
    """
    import os

    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Prune excluded directories in-place so os.walk skips them
        dirnames[:] = [
            d for d in dirnames
            if d.lower() not in exclude_set
        ]
        for fname in filenames:
            if fname.endswith(".md"):
                yield Path(dirpath) / fname


def _get_recent_files(
    vault_root: Path,
    days: int,
    exclude_folders: list[str],
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Find recently modified .md files across the vault.

    If ``since``/``until`` are provided, they override ``days``.
    """
    since_dt = datetime.fromisoformat(since).astimezone(timezone.utc) if since else (datetime.now(timezone.utc) - timedelta(days=days))
    until_dt = datetime.fromisoformat(until).astimezone(timezone.utc) if until else datetime.now(timezone.utc)
    exclude_set = {f.lower() for f in exclude_folders}
    results = []

    for md_file in _walk_vault(vault_root, exclude_set):
        try:
            mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue

        if since_dt <= mtime <= until_dt:
            rel = md_file.relative_to(vault_root)
            results.append({
                "path": str(rel),
                "modified": mtime.strftime("%Y-%m-%d %H:%M"),
            })

    results.sort(key=lambda x: x["modified"], reverse=True)
    return results


def _get_journal_stats(vault_root: Path, journal_dir: str) -> dict[str, Any] | None:
    """Extract stats for today's journal: Log entries and Running Notes backlog.

    Reuses patterns from journal.py (Log timestamps) and
    journal_backlog/extract.py (Running Notes / banner stripping).

    Returns None if today's journal doesn't exist.
    """
    today = datetime.now().date().isoformat()
    journal_file = vault_root / journal_dir / f"{today}.md"
    if not journal_file.exists():
        return None

    content = journal_file.read_text(encoding="utf-8", errors="replace")

    stats: dict[str, Any] = {"date": today}

    # --- Log section stats ---
    log_header = re.search(r"^#\s+\*{0,2}Log\*{0,2}\s*$", content, re.MULTILINE)
    if log_header:
        log_start = log_header.end()
        next_sec = re.search(r"^#\s+\*{0,2}[A-Z]", content[log_start:], re.MULTILINE)
        log_end = log_start + next_sec.start() if next_sec else len(content)
        log_body = content[log_start:log_end]

        # Count timestamped entries: "- 1:11 PM - ..." or "* 12:00 PM - ..."
        log_ts_re = re.compile(
            r"^[\*\-]\s+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\s*-\s*",
            re.MULTILINE,
        )
        log_entries = list(log_ts_re.finditer(log_body))
        stats["log_entry_count"] = len(log_entries)

        if log_entries:
            # Extract timestamp from last entry
            last_match = log_entries[-1]
            ts_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))", last_match.group())
            stats["log_last_entry_time"] = ts_match.group(1).strip() if ts_match else None
        else:
            stats["log_last_entry_time"] = None
    else:
        stats["log_entry_count"] = 0
        stats["log_last_entry_time"] = None

    # --- Running Notes stats ---
    rn_header = re.search(
        r"^#\s+\*{0,2}Running Notes\s*/\s*Considerations\*{0,2}\s*$",
        content,
        re.MULTILINE,
    )
    if rn_header:
        rn_start = rn_header.end()
        rn_end_marker = re.search(r"^%\s*RUNNING\s+END\s*$", content[rn_start:], re.MULTILINE)
        if rn_end_marker:
            rn_end = rn_start + rn_end_marker.start()
        else:
            next_heading = re.search(r"^#\s+\*{0,2}[A-Z]", content[rn_start:], re.MULTILINE)
            rn_end = rn_start + next_heading.start() if next_heading else len(content)

        rn_body = content[rn_start:rn_end]

        # Count carried-over banners
        banner_re = re.compile(
            r"^\*{3}'Running Notes\s*/\s*Considerations'\s*carried over from\s+"
            r"(\d{4}-\d{2}-\d{2})\*{3}\s*$",
            re.MULTILINE,
        )
        banners = list(banner_re.finditer(rn_body))
        carried_dates = [m.group(1) for m in banners]

        # Count non-empty content lines (excluding banners and separators)
        content_lines = 0
        for line in rn_body.split("\n"):
            stripped = line.strip()
            if (
                stripped
                and not banner_re.match(stripped)
                and not re.match(r"^-{3,}\s*$", stripped)
            ):
                content_lines += 1

        stats["running_notes_lines"] = content_lines
        stats["running_notes_carried_dates"] = len(carried_dates)
        stats["running_notes_oldest_date"] = carried_dates[-1] if carried_dates else None
    else:
        stats["running_notes_lines"] = 0
        stats["running_notes_carried_dates"] = 0
        stats["running_notes_oldest_date"] = None

    return stats


def _get_tasks(vault_root: Path) -> str:
    """Extract incomplete tasks from master task list."""
    task_file = vault_root / "tasks" / "master-task-list.md"
    if not task_file.exists():
        return ""

    content = task_file.read_text(encoding="utf-8", errors="replace")
    lines = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            # Clean up tags for readability
            task_text = stripped[5:].strip()
            lines.append(f"- [ ] {task_text}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wellness tracker: parse #dailyworkq/ sign-in metrics and compute trends
# ---------------------------------------------------------------------------

_DAILYWORKQ_RE = re.compile(
    r"#dailyworkq/(sleep|energy|mood):\s*(\d+\.?\d*)",
)


def _parse_wellness(journal_path: Path, days: int) -> list[dict[str, Any]]:
    """Parse numeric sign-in metrics from the last *days* journal files.

    Returns a list of dicts sorted newest-first:
    ``[{"date": "2026-04-03", "sleep": 5.0, "energy": 7.0, "mood": 7.0}, ...]``
    Missing values are None.
    """
    today = datetime.now().date()
    results = []

    for offset in range(days):
        d = today - timedelta(days=offset)
        fpath = journal_path / f"{d.isoformat()}.md"
        row: dict[str, Any] = {"date": d.isoformat(), "sleep": None, "energy": None, "mood": None}

        if fpath.exists():
            content = fpath.read_text(encoding="utf-8", errors="replace")
            for m in _DAILYWORKQ_RE.finditer(content):
                key = m.group(1)
                row[key] = float(m.group(2))

        results.append(row)

    return results


def _trend_direction(recent: list[float], prior: list[float], threshold: float = 0.5) -> str:
    """Compare two period averages and return an arrow indicator."""
    if not recent or not prior:
        return "—"
    r_avg = sum(recent) / len(recent)
    p_avg = sum(prior) / len(prior)
    diff = r_avg - p_avg
    if diff > threshold:
        return "↑"
    elif diff < -threshold:
        return "↓"
    return "→"


def collect_wellness(cfg: dict[str, Any]) -> str:
    """Collect wellness sign-in metrics and trends.

    Returns a compact markdown summary with a table and trend indicators.
    """
    vault_root = Path(cfg["vault_root"])
    obs_cfg = cfg.get("obsidian", {})
    journal_dir = obs_cfg.get("journal_dir", "journal")
    wellness_days = obs_cfg.get("wellness_days", 14)

    journal_path = vault_root / journal_dir
    if not journal_path.is_dir():
        return "# Wellness\n\n*No journal directory found.*\n"

    rows = _parse_wellness(journal_path, wellness_days)
    metrics = ["sleep", "energy", "mood"]

    # Build table
    lines = [
        "# Wellness",
        "",
        f"*Last {wellness_days} days from journal sign-in trackers*",
        "",
        "| Date | Sleep | Energy | Mood |",
        "|------|-------|--------|------|",
    ]
    for row in rows:
        def _fmt(v):
            return f"{v:g}" if v is not None else "—"
        lines.append(
            f"| {row['date']} | {_fmt(row['sleep'])} | {_fmt(row['energy'])} | {_fmt(row['mood'])} |"
        )

    # Compute trends: recent 7 days vs prior 7 days
    lines.append("")
    lines.append("### Trends (recent 7d avg vs prior 7d)")
    lines.append("")

    half = min(7, wellness_days // 2)
    for metric in metrics:
        recent_vals = [r[metric] for r in rows[:half] if r[metric] is not None]
        prior_vals = [r[metric] for r in rows[half:half * 2] if r[metric] is not None]

        if recent_vals:
            r_avg = sum(recent_vals) / len(recent_vals)
            arrow = _trend_direction(recent_vals, prior_vals)
            unit = "h" if metric == "sleep" else ""
            if prior_vals:
                p_avg = sum(prior_vals) / len(prior_vals)
                lines.append(f"- **{metric.title()}:** {r_avg:.1f}{unit} {arrow} (from {p_avg:.1f}{unit})")
            else:
                lines.append(f"- **{metric.title()}:** {r_avg:.1f}{unit} (no prior data)")
        else:
            lines.append(f"- **{metric.title()}:** no data")

    # Today's snapshot
    if rows and any(rows[0][m] is not None for m in metrics):
        today = rows[0]
        parts = []
        for m in metrics:
            v = today[m]
            if v is not None:
                unit = "h" if m == "sleep" else ""
                parts.append(f"{m.title()}: {v:g}{unit}")
        lines.append("")
        lines.append(f"### Today — {' | '.join(parts)}")

    lines.append("")
    return "\n".join(lines)


def collect(cfg: dict[str, Any]) -> tuple[str, str]:
    """Collect Obsidian context. Returns (obsidian_summary_md, tasks_summary_md)."""
    vault_root = Path(cfg["vault_root"])
    obs_cfg = cfg.get("obsidian", {})

    journal_dir = obs_cfg.get("journal_dir", "journal")
    journal_days = obs_cfg.get("journal_days", 7)
    recent_days = obs_cfg.get("recent_modified_days", 3)
    exclude_folders = obs_cfg.get("exclude_folders", [])

    # Explicit time range overrides (from update-journal workflow)
    range_since = cfg.get("since")
    range_until = cfg.get("until")

    now = datetime.now(timezone.utc)

    # Journal entries (since/until override journal_days when provided)
    journal_entries = _get_journal_entries(
        vault_root, journal_dir, journal_days,
        since=range_since, until=range_until,
    )

    # Recently modified files
    recent_files = _get_recent_files(vault_root, recent_days, exclude_folders, since=range_since, until=range_until)

    # Tasks
    tasks_md = _get_tasks(vault_root)

    # Task events (state changes within the collection window)
    task_events = []
    event_lookback_hours = cfg.get("tasks", {}).get("event_lookback_hours", 48)
    try:
        from work_buddy.obsidian.tasks.store import get_events_in_range
        if range_since or range_until:
            since_str = range_since or "1970-01-01T00:00:00"
            until_str = range_until or "9999-12-31T23:59:59"
        else:
            since_dt = now - timedelta(hours=event_lookback_hours)
            since_str = since_dt.isoformat()
            until_str = now.isoformat()
        task_events = get_events_in_range(since_str, until_str)
    except Exception:
        pass  # store may not be initialized

    # Journal stats (today only — Log entries + Running Notes backlog)
    journal_stats = _get_journal_stats(vault_root, journal_dir)

    # Build obsidian summary
    lines = [
        "# Obsidian Summary",
        "",
        f"*Collected: {now.strftime('%Y-%m-%d %H:%M UTC')}*",
        f"*Vault: `{vault_root}`*",
        "",
    ]

    if journal_stats:
        lines.append("## Today's Journal Status")
        lines.append("")
        log_count = journal_stats["log_entry_count"]
        last_time = journal_stats["log_last_entry_time"]
        rn_lines = journal_stats["running_notes_lines"]
        rn_dates = journal_stats["running_notes_carried_dates"]
        rn_oldest = journal_stats["running_notes_oldest_date"]

        lines.append(f"- **Log entries:** {log_count}"
                     + (f" (last at {last_time})" if last_time else ""))
        lines.append(f"- **Running Notes:** {rn_lines} content lines"
                     + (f", carried over from {rn_dates} dates"
                        f" (oldest: {rn_oldest})" if rn_dates else ""))
        if rn_lines > 50:
            lines.append(f"- **Backlog alert:** Running Notes has {rn_lines} lines"
                         " — consider `/wb-journal-backlog`")
        lines.append("")

    if journal_entries:
        if range_since or range_until:
            date_range = f"{range_since or '...'} to {range_until or 'now'}"
            lines.append(f"## Journal ({date_range})")
        else:
            lines.append(f"## Journal (last {journal_days} days)")
        lines.append("")
        for entry in journal_entries:
            lines.append(f"### {entry['date']}")
            lines.append("")
            for sec_name, sec_body in entry["sections"].items():
                lines.append(f"**{sec_name}:**")
                lines.append("")
                lines.append(sec_body)
                lines.append("")
    else:
        lines.append("## Journal")
        lines.append("")
        lines.append("*No journal entries found.*")
        lines.append("")

    if recent_files:
        lines.append(f"## Recently Modified Files (last {recent_days} days)")
        lines.append("")
        for f in recent_files[:30]:  # Cap at 30 files
            lines.append(f"- `{f['path']}` — {f['modified']}")
        if len(recent_files) > 30:
            lines.append(f"- *...and {len(recent_files) - 30} more*")
        lines.append("")

    obsidian_md = "\n".join(lines)

    # Build tasks summary
    task_lines = [
        "# Tasks Summary",
        "",
        f"*Collected: {now.strftime('%Y-%m-%d %H:%M UTC')}*",
        f"*Source: `tasks/master-task-list.md`*",
        "",
    ]
    if tasks_md:
        task_lines.append("## Incomplete Tasks")
        task_lines.append("")
        task_lines.append(tasks_md)
        task_lines.append("")
    else:
        task_lines.append("*No incomplete tasks found.*")
        task_lines.append("")

    if task_events:
        if range_since or range_until:
            task_lines.append("## Task Events (in collection window)")
        else:
            task_lines.append(f"## Recent Task Events (last {event_lookback_hours}h)")
        task_lines.append("")
        for evt in task_events:
            ts = evt["changed_at"]
            # Format: HH:MM — task description (old_state → new_state)
            try:
                t = datetime.fromisoformat(ts).strftime("%H:%M")
            except (ValueError, TypeError):
                t = ts[:16] if ts else "??:??"
            desc = evt["task_id"]
            old = evt.get("old_state") or "new"
            new = evt["new_state"]
            reason = f" — {evt['reason']}" if evt.get("reason") else ""
            task_lines.append(f"- {t} — {desc} ({old} → {new}){reason}")
        task_lines.append("")

    return obsidian_md, "\n".join(task_lines)
