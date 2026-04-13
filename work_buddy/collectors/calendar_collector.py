"""Collect Google Calendar schedule via the Obsidian plugin.

Respects the same time-range overrides as other collectors:
- ``cfg["since"]`` / ``cfg["until"]`` — explicit ISO datetime range
- Defaults to today when no overrides are provided.

Requires Obsidian running with the Google Calendar plugin authenticated.
Degrades gracefully if unavailable.
"""

from datetime import datetime, timezone
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def _resolve_date_range(cfg: dict[str, Any]) -> tuple[str, str]:
    """Derive start/end date strings (YYYY-MM-DD) from config overrides.

    Falls back to today's date when no overrides are present.
    """
    range_since = cfg.get("since")
    range_until = cfg.get("until")

    if range_since:
        start_date = datetime.fromisoformat(range_since).strftime("%Y-%m-%d")
    else:
        start_date = datetime.now().strftime("%Y-%m-%d")

    if range_until:
        end_date = datetime.fromisoformat(range_until).strftime("%Y-%m-%d")
    else:
        end_date = datetime.now().strftime("%Y-%m-%d")

    return start_date, end_date


def collect(cfg: dict[str, Any]) -> str:
    """Collect calendar schedule and return a markdown summary.

    Respects ``cfg["since"]`` / ``cfg["until"]`` for date range. When
    these match today (or are absent), uses ``get_today_schedule()`` for
    richer time-relative metadata (current/upcoming classification).
    Otherwise falls back to ``get_events()`` for arbitrary date ranges.

    Returns a formatted markdown string suitable for inclusion in context bundles.
    Returns a "not available" message if Obsidian or the plugin is unreachable.
    """
    from work_buddy.obsidian import bridge

    try:
        available = bridge.is_available()
    except Exception:
        available = False

    if not available:
        logger.info("Obsidian bridge not available — skipping calendar collection")
        return _unavailable_report("Obsidian bridge not reachable")

    try:
        from work_buddy.calendar import check_ready
        status = check_ready()
        if not status.get("ready"):
            reason = status.get("reason", "unknown")
            logger.info("Google Calendar not ready: %s", reason)
            return _unavailable_report(f"Google Calendar plugin: {reason}")
    except Exception as e:
        logger.warning("Calendar check_ready failed: %s", e)
        return _unavailable_report(f"check_ready error: {e}")

    start_date, end_date = _resolve_date_range(cfg)
    today_str = datetime.now().strftime("%Y-%m-%d")
    is_today_only = start_date == today_str and end_date == today_str

    try:
        if is_today_only:
            # Use the richer today-specific endpoint with time classification
            from work_buddy.calendar import get_today_schedule
            schedule = get_today_schedule()
            return _format_schedule(schedule)
        else:
            # Arbitrary date range — use get_events
            from work_buddy.calendar import get_events
            result = get_events(start_date, end_date)
            if result is None:
                return _format_range({"count": 0, "events": []}, start_date, end_date)
            return _format_range(result, start_date, end_date)
    except Exception as e:
        logger.warning("Calendar fetch failed: %s", e)
        return _unavailable_report(f"Failed to fetch schedule: {e}")


def _format_schedule(schedule: dict) -> str:
    """Format the schedule dict into a readable markdown summary."""
    lines = ["# Calendar — Today's Schedule"]
    lines.append("")
    lines.append(f"**Date:** {schedule.get('date', '?')}  ")
    lines.append(f"**Current time:** {schedule.get('currentTime', '?')}  ")

    count = schedule.get("count", 0)
    if count == 0:
        lines.append("")
        lines.append("No events scheduled today.")
        return "\n".join(lines)

    lines.append(
        f"**Events:** {count} total "
        f"({schedule.get('allDayCount', 0)} all-day, "
        f"{schedule.get('timedCount', 0)} timed)"
    )

    current_count = schedule.get("currentCount", 0)
    upcoming_count = schedule.get("upcomingCount", 0)
    if current_count > 0:
        lines.append(f"**Now:** {current_count} event(s) in progress")
    if upcoming_count > 0:
        lines.append(f"**Upcoming:** {upcoming_count} remaining today")

    events = schedule.get("events", [])

    # All-day events
    all_day = [e for e in events if e.get("isAllDay")]
    if all_day:
        lines.append("")
        lines.append("## All-Day Events")
        for e in all_day:
            cal = e.get("calendarName") or ""
            cal_tag = f" `{cal}`" if cal else ""
            lines.append(f"- {e['summary']}{cal_tag}")

    # Timed events
    timed = [e for e in events if not e.get("isAllDay")]
    if timed:
        lines.append("")
        lines.append("## Schedule")
        lines.append("")
        for e in timed:
            status_icon = {
                "current": ">",
                "upcoming": " ",
                "past": "~",
            }.get(e.get("timeStatus", ""), " ")

            start = _format_time(e.get("start"))
            end = _format_time(e.get("end"))
            time_str = f"{start}–{end}" if start and end else start or "?"

            cal = e.get("calendarName") or ""
            cal_tag = f" `{cal}`" if cal else ""
            location = f" @ {e['location']}" if e.get("location") else ""

            prefix = "~~" if e.get("timeStatus") == "past" else ""
            suffix = "~~" if e.get("timeStatus") == "past" else ""
            marker = " **<-- NOW**" if e.get("timeStatus") == "current" else ""

            lines.append(
                f"- {prefix}{time_str} — {e['summary']}{suffix}"
                f"{location}{cal_tag}{marker}"
            )

    return "\n".join(lines)


def _format_range(result: dict, start_date: str, end_date: str) -> str:
    """Format a multi-day event range into markdown."""
    lines = [f"# Calendar — {start_date} to {end_date}"]
    lines.append("")

    count = result.get("count", 0)
    if count == 0:
        lines.append("No events in this date range.")
        return "\n".join(lines)

    lines.append(f"**Events:** {count}")
    lines.append("")

    events = result.get("events", [])

    # Group by date
    by_date: dict[str, list] = {}
    for e in events:
        is_all_day = e.get("isAllDay", False)
        if is_all_day:
            date_key = e.get("start", {}).get("date", "unknown")
        else:
            dt = e.get("start", {}).get("dateTime", "")
            date_key = dt.split("T")[0] if "T" in dt else "unknown"
        by_date.setdefault(date_key, []).append(e)

    for date_key in sorted(by_date.keys()):
        day_events = by_date[date_key]
        lines.append(f"## {date_key}")
        lines.append("")
        for e in day_events:
            is_all_day = e.get("isAllDay", False)
            if is_all_day:
                cal = e.get("calendarName") or ""
                cal_tag = f" `{cal}`" if cal else ""
                lines.append(f"- (all day) {e['summary']}{cal_tag}")
            else:
                start = _format_time(e.get("start"))
                end = _format_time(e.get("end"))
                time_str = f"{start}–{end}" if start and end else start or "?"
                cal = e.get("calendarName") or ""
                cal_tag = f" `{cal}`" if cal else ""
                location = f" @ {e['location']}" if e.get("location") else ""
                lines.append(f"- {time_str} — {e['summary']}{location}{cal_tag}")
        lines.append("")

    return "\n".join(lines)


def _format_time(time_obj: dict | None) -> str | None:
    """Extract a display time (HH:MM) from a Google Calendar start/end object."""
    if not time_obj:
        return None
    dt = time_obj.get("dateTime")
    if not dt:
        return time_obj.get("date")
    # dateTime is like "2026-04-04T09:30:00-04:00"
    # Extract HH:MM from the time portion
    try:
        time_part = dt.split("T")[1]
        return time_part[:5]
    except (IndexError, TypeError):
        return dt


def _unavailable_report(reason: str) -> str:
    """Generate a minimal report when calendar data is not available."""
    return (
        "# Calendar\n\n"
        f"Calendar data not available: {reason}\n\n"
        "To enable: open Obsidian, ensure the Google Calendar plugin is "
        "installed and authenticated."
    )
