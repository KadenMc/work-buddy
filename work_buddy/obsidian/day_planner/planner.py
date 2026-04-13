"""Gap-filling schedule generator for Day Planner.

Pure logic — no side effects. Takes calendar events and focused tasks,
returns time-blocked plan entries that fit into calendar gaps.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def _minutes(h: int, m: int = 0) -> int:
    return h * 60 + m


def _fmt(minutes: int) -> str:
    """Convert minutes-since-midnight to HH:mm string."""
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


def _snap(minutes: int, step: int, direction: str = "down") -> int:
    """Snap minutes to nearest step boundary."""
    if direction == "down":
        return (minutes // step) * step
    return ((minutes + step - 1) // step) * step


def _parse_iso_to_minutes(iso_str: str) -> int | None:
    """Parse an ISO datetime string to minutes-since-midnight (local time).

    Handles formats like '2026-04-05T14:00:00-04:00' and '2026-04-05T14:00:00'.
    """
    if not iso_str:
        return None
    # Strip timezone info for local time extraction
    # The calendar events are already in local time from the plugin
    m = re.match(r"\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2})", iso_str)
    if not m:
        return None
    return _minutes(int(m.group(1)), int(m.group(2)))


def _extract_calendar_slots(
    events: list[dict],
    calendar_prefix: str = "[Cal]",
) -> tuple[list[tuple[int, int]], list[dict]]:
    """Extract occupied time slots and plan entries from calendar events.

    Returns:
        slots: list of (start_min, end_min) tuples
        entries: list of plan entry dicts for calendar events
    """
    slots = []
    entries = []

    for ev in events:
        start = ev.get("start", {})
        end = ev.get("end", {})

        # Skip all-day events (they have 'date' not 'dateTime')
        if start.get("date") and not start.get("dateTime"):
            continue

        start_dt = start.get("dateTime", "")
        end_dt = end.get("dateTime", "")
        start_min = _parse_iso_to_minutes(start_dt)
        end_min = _parse_iso_to_minutes(end_dt)

        if start_min is None or end_min is None:
            continue

        # Skip past events
        time_status = ev.get("timeStatus", "")
        if time_status == "past":
            continue

        summary = ev.get("summary", "Event")
        slots.append((start_min, end_min))
        entries.append({
            "time_start": _fmt(start_min),
            "time_end": _fmt(end_min),
            "text": f"{calendar_prefix} {summary}",
            "checked": False,
            "is_calendar": True,
        })

    return slots, entries


def _find_gaps(
    occupied: list[tuple[int, int]],
    work_start: int,
    work_end: int,
    min_gap: int = 15,
) -> list[tuple[int, int]]:
    """Find free time gaps between occupied slots within work hours.

    Returns list of (start_min, end_min) tuples, sorted by size descending.
    """
    # Merge overlapping slots
    if not occupied:
        return [(work_start, work_end)]

    sorted_slots = sorted(occupied)
    merged = [sorted_slots[0]]
    for start, end in sorted_slots[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Find gaps
    gaps = []
    prev_end = work_start

    for slot_start, slot_end in merged:
        gap_start = max(prev_end, work_start)
        gap_end = min(slot_start, work_end)
        if gap_end - gap_start >= min_gap:
            gaps.append((gap_start, gap_end))
        prev_end = max(prev_end, slot_end)

    # Gap after last slot
    if prev_end < work_end:
        gap_size = work_end - prev_end
        if gap_size >= min_gap:
            gaps.append((prev_end, work_end))

    # Sort by size (largest first) for task placement
    gaps.sort(key=lambda g: g[1] - g[0], reverse=True)
    return gaps


def _clean_task_text(text: str) -> str:
    """Strip work-buddy metadata tokens from task text for display."""
    # Remove #todo, #projects/*, #tasker/*, 🆔 t-xxxxx
    cleaned = re.sub(r"#todo\s*", "", text)
    cleaned = re.sub(r"#projects/\S+\s*", "", cleaned)
    cleaned = re.sub(r"#tasker/\S+\s*", "", cleaned)
    cleaned = re.sub(r"🆔\s*\S+\s*", "", cleaned)
    cleaned = re.sub(r"[📅⏳🛫✅❌]\s*\d{4}-\d{2}-\d{2}\s*", "", cleaned)
    cleaned = re.sub(r"[⏫🔼]\s*", "", cleaned)
    return cleaned.strip()


def generate_plan(
    calendar_events: list[dict],
    focused_tasks: list[dict],
    cfg: dict[str, Any] | None = None,
) -> list[dict]:
    """Generate a time-blocked daily plan from calendar events and focused tasks.

    Args:
        calendar_events: Events from get_today_schedule()["events"].
        focused_tasks: Tasks with state="focused". Each should have at minimum
            a "text" or "description" key.
        cfg: Config dict from morning.day_planner. Keys:
            work_hours: [start_hour, end_hour] (default [9, 17])
            default_task_duration: minutes per task block (default 60)
            break_interval: max continuous work minutes (default 90)
            break_duration: break length minutes (default 15)
            include_calendar_events: bool (default True)
            calendar_prefix: str (default "[Cal]")

    Returns:
        List of plan entry dicts, sorted chronologically. Each has:
        - time_start: "HH:mm" or None (unscheduled)
        - time_end: "HH:mm" or None (unscheduled)
        - text: str
        - checked: False
        - is_calendar: bool (optional)
    """
    cfg = cfg or {}
    work_hours = cfg.get("work_hours", [9, 17])
    task_duration = cfg.get("default_task_duration", 60)
    break_interval = cfg.get("break_interval", 90)
    break_duration = cfg.get("break_duration", 15)
    include_cal = cfg.get("include_calendar_events", True)
    cal_prefix = cfg.get("calendar_prefix", "[Cal]")
    snap = 10  # matches plugin's snapStepMinutes

    work_start = _minutes(work_hours[0])
    work_end = _minutes(work_hours[1])

    # 1. Extract calendar slots and entries
    cal_slots, cal_entries = _extract_calendar_slots(calendar_events, cal_prefix)

    # 2. Find gaps
    gaps = _find_gaps(cal_slots, work_start, work_end)

    # 3. Place tasks into gaps
    scheduled = []
    unscheduled = []
    used_gaps: list[tuple[int, int]] = []  # track consumed portions

    for task in focused_tasks:
        text = task.get("description") or task.get("text", "Task")
        clean_text = _clean_task_text(text)
        if not clean_text:
            clean_text = text

        placed = False
        for i, (gap_start, gap_end) in enumerate(gaps):
            available = gap_end - gap_start

            # Check if this gap segment is still usable
            # (earlier tasks may have consumed part of it)
            effective_start = gap_start
            for used_start, used_end in used_gaps:
                if used_start <= effective_start < used_end:
                    effective_start = used_end
            available = gap_end - effective_start

            if available < 15:  # too small
                continue

            duration = min(task_duration, available)
            t_start = _snap(effective_start, snap, "up")
            t_end = _snap(t_start + duration, snap, "up")
            t_end = min(t_end, gap_end)

            if t_end - t_start < 10:
                continue

            scheduled.append({
                "time_start": _fmt(t_start),
                "time_end": _fmt(t_end),
                "text": clean_text,
                "checked": False,
            })
            used_gaps.append((t_start, t_end))
            placed = True
            break

        if not placed:
            unscheduled.append({
                "text": clean_text,
                "checked": False,
            })

    # 4. Combine calendar entries + task entries
    all_timed = []
    if include_cal:
        all_timed.extend(cal_entries)
    all_timed.extend(scheduled)

    # 5. Sort chronologically
    all_timed.sort(key=lambda e: e.get("time_start", "99:99"))

    # 6. Insert breaks where continuous work exceeds break_interval
    with_breaks = []
    continuous_minutes = 0
    prev_end_min = None

    for entry in all_timed:
        start_min = _parse_iso_to_minutes(
            f"2000-01-01T{entry['time_start']}:00"
        ) or 0
        end_min = _parse_iso_to_minutes(
            f"2000-01-01T{entry['time_end']}:00"
        ) or 0

        # Check gap from previous entry
        if prev_end_min is not None and start_min > prev_end_min:
            gap = start_min - prev_end_min
            if gap >= break_duration:
                continuous_minutes = 0  # natural break
            else:
                continuous_minutes += gap

        block_length = end_min - start_min
        if continuous_minutes + block_length > break_interval and not entry.get("is_calendar"):
            # Insert a break before this entry
            break_start = _snap(start_min - break_duration, snap, "down")
            break_start = max(break_start, prev_end_min or work_start)
            break_end = _snap(break_start + break_duration, snap, "up")

            if break_end <= start_min and break_end - break_start >= 10:
                with_breaks.append({
                    "time_start": _fmt(break_start),
                    "time_end": _fmt(break_end),
                    "text": "Break",
                    "checked": False,
                })
            continuous_minutes = 0

        with_breaks.append(entry)
        continuous_minutes += block_length
        prev_end_min = end_min

    # 7. Append unscheduled tasks at the end
    for task in unscheduled:
        with_breaks.append(task)

    logger.info(
        "Generated plan: %d timed + %d unscheduled entries",
        len(with_breaks) - len(unscheduled),
        len(unscheduled),
    )
    return with_breaks
