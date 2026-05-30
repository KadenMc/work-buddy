"""Collect calendar schedule for context bundles.

Renders over the provider-neutral calendar seam
(:func:`work_buddy.calendar.provider.get_calendar_provider`) rather than calling
the Obsidian plugin directly — so the same formatter works over any adapter
(bridge today, native later) and the test suite can drive it over the fake.

Respects the same time-range overrides as other collectors:
- ``cfg["since"]`` / ``cfg["until"]`` — explicit ISO datetime range
- Defaults to today (in the user's timezone) when no overrides are provided.

Single data path: everything renders from ``list_events()``. The today-only
view keeps its richer time-relative formatting (now/upcoming/past), but that
classification is now computed in Python from the events rather than fetched
from a second plugin endpoint. Degrades gracefully when the provider is
unavailable.
"""

from datetime import datetime
from typing import Any

from work_buddy import config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def _today_str() -> str:
    """Today's date (YYYY-MM-DD) in the user's configured timezone.

    Reads ``config.USER_TZ`` at call time (it is a process-cached ``ZoneInfo``
    that only refreshes on restart). Using a tz-aware ``now`` here fixes a
    latent DST/travel bug: a naive ``datetime.now()`` computes "today" in the
    *process* zone, which can disagree with the user's calendar day.
    """
    return datetime.now(config.USER_TZ).strftime("%Y-%m-%d")


def _resolve_date_range(cfg: dict[str, Any]) -> tuple[str, str]:
    """Derive start/end date strings (YYYY-MM-DD) from config overrides.

    Falls back to today's date (in the user's timezone) when no overrides
    are present.
    """
    range_since = cfg.get("since")
    range_until = cfg.get("until")

    if range_since:
        start_date = datetime.fromisoformat(range_since).strftime("%Y-%m-%d")
    else:
        start_date = _today_str()

    if range_until:
        end_date = datetime.fromisoformat(range_until).strftime("%Y-%m-%d")
    else:
        end_date = _today_str()

    return start_date, end_date


def collect(cfg: dict[str, Any]) -> str:
    """Collect calendar schedule and return a markdown summary.

    Respects ``cfg["since"]`` / ``cfg["until"]`` for date range. When the
    resolved range is today-only (or absent), renders the richer schedule view
    with current/upcoming/past classification; otherwise renders a grouped
    multi-day range. When ``cfg["include_coverage"]`` is truthy, appends a
    coverage footer (which calendars are visible / blacklisted / errored).

    Returns a formatted markdown string suitable for inclusion in context
    bundles, or a "not available" message when the provider is unreachable.
    """
    from work_buddy.calendar.errors import CalendarError
    from work_buddy.calendar.provider import get_calendar_provider

    try:
        provider = get_calendar_provider()
    except CalendarError as exc:
        logger.info("Calendar provider unavailable: %s", exc)
        return _unavailable_report(str(exc))

    try:
        status = provider.health()
    except CalendarError as exc:
        logger.info("Calendar provider not reachable: %s", exc)
        return _unavailable_report(str(exc))
    if not status.get("ready", True):
        reason = status.get("reason", "unknown")
        logger.info("Calendar provider not ready: %s", reason)
        return _unavailable_report(f"Calendar provider: {reason}")

    start_date, end_date = _resolve_date_range(cfg)
    today_str = _today_str()
    is_today_only = start_date == today_str and end_date == today_str

    try:
        events = provider.list_events(start=start_date, end=end_date)
    except CalendarError as exc:
        logger.warning("Calendar fetch failed: %s", exc)
        return _unavailable_report(f"Failed to fetch schedule: {exc}")

    if is_today_only:
        now = datetime.now(config.USER_TZ)
        body = _format_schedule(events, today_str, now)
    else:
        body = _format_range(events, start_date, end_date)

    if cfg.get("include_coverage"):
        body = body + "\n\n" + _coverage_footer(provider, start_date, end_date)
    return body


def _time_status(ev, now: datetime) -> str:
    """Classify a timed event relative to ``now``: upcoming / current / past.

    Mirrors the old plugin-side classification (now.isBefore(start) → upcoming,
    now.isAfter(end) → past, else current), but in Python over the canonical
    model. All-day events are ``"all-day"``.
    """
    if ev.is_all_day:
        return "all-day"
    start, end = ev.start.dt, ev.end.dt
    if start is not None and now < start:
        return "upcoming"
    if end is not None and now > end:
        return "past"
    return "current"


def _format_schedule(events: list, today_str: str, now: datetime) -> str:
    """Render the today schedule from a list of ``CalendarEvent``."""
    lines = ["# Calendar — Today's Schedule"]
    lines.append("")
    lines.append(f"**Date:** {today_str}  ")
    lines.append(f"**Current time:** {now.strftime('%H:%M')}  ")

    if not events:
        lines.append("")
        lines.append("No events scheduled today.")
        return "\n".join(lines)

    ordered = sorted(events, key=lambda e: e.start.sort_value)
    all_day = [e for e in ordered if e.is_all_day]
    timed = [e for e in ordered if not e.is_all_day]
    statuses = {id(e): _time_status(e, now) for e in timed}
    current_count = sum(1 for e in timed if statuses[id(e)] == "current")
    upcoming_count = sum(1 for e in timed if statuses[id(e)] == "upcoming")

    lines.append(
        f"**Events:** {len(events)} total "
        f"({len(all_day)} all-day, {len(timed)} timed)"
    )
    if current_count > 0:
        lines.append(f"**Now:** {current_count} event(s) in progress")
    if upcoming_count > 0:
        lines.append(f"**Upcoming:** {upcoming_count} remaining today")

    if all_day:
        lines.append("")
        lines.append("## All-Day Events")
        for e in all_day:
            cal_tag = f" `{e.calendar_name}`" if e.calendar_name else ""
            lines.append(f"- {e.summary}{cal_tag}")

    if timed:
        lines.append("")
        lines.append("## Schedule")
        lines.append("")
        for e in timed:
            status = statuses[id(e)]
            start = e.start.hhmm
            end = e.end.hhmm
            time_str = f"{start}–{end}" if start and end else start or "?"
            cal_tag = f" `{e.calendar_name}`" if e.calendar_name else ""
            location = f" @ {e.location}" if e.location else ""
            prefix = suffix = "~~" if status == "past" else ""
            marker = " **<-- NOW**" if status == "current" else ""
            lines.append(
                f"- {prefix}{time_str} — {e.summary}{suffix}"
                f"{location}{cal_tag}{marker}"
            )

    return "\n".join(lines)


def _format_range(events: list, start_date: str, end_date: str) -> str:
    """Render a grouped-by-day multi-day range from ``CalendarEvent``\\ s."""
    lines = [f"# Calendar — {start_date} to {end_date}"]
    lines.append("")

    if not events:
        lines.append("No events in this date range.")
        return "\n".join(lines)

    lines.append(f"**Events:** {len(events)}")
    lines.append("")

    by_date: dict[str, list] = {}
    for e in events:
        by_date.setdefault(e.start.date_key, []).append(e)

    for date_key in sorted(by_date.keys()):
        day_events = sorted(by_date[date_key], key=lambda e: e.start.sort_value)
        lines.append(f"## {date_key}")
        lines.append("")
        for e in day_events:
            cal_tag = f" `{e.calendar_name}`" if e.calendar_name else ""
            if e.is_all_day:
                lines.append(f"- (all day) {e.summary}{cal_tag}")
            else:
                start = e.start.hhmm
                end = e.end.hhmm
                time_str = f"{start}–{end}" if start and end else start or "?"
                location = f" @ {e.location}" if e.location else ""
                lines.append(f"- {time_str} — {e.summary}{location}{cal_tag}")
        lines.append("")

    return "\n".join(lines)


def _coverage_footer(provider, start_date: str, end_date: str) -> str:
    """Compact coverage footer (opt-in via ``cfg["include_coverage"]``)."""
    from work_buddy.calendar.capabilities import build_coverage_report

    rep = build_coverage_report(provider, start_date, end_date)
    lines = ["## Calendar coverage"]
    lines.append(
        f"- Subscribed: {len(rep.subscribed)} calendar(s); "
        f"{rep.total_events} event(s) in window"
    )
    if rep.blacklisted:
        lines.append(f"- Hidden (blacklisted): {len(rep.blacklisted)}")
    if rep.errored:
        lines.append(f"- Errored: {', '.join(sorted(rep.errored))}")
    return "\n".join(lines)


def _unavailable_report(reason: str) -> str:
    """Generate a minimal report when calendar data is not available."""
    return (
        "# Calendar\n\n"
        f"Calendar data not available: {reason}\n\n"
        "To enable: open Obsidian, ensure the Google Calendar plugin is "
        "installed and authenticated."
    )
