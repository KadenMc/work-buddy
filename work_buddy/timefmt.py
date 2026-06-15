"""Shared timestamp formatting for context collectors and activity rendering.

The single home for "render a UTC instant as the user's local wall-clock time".
Context bundles are read by the journal agent on one local-time timeline, so
every collector must agree on the timezone it prints. The convention is
**local-naive**: convert to ``config.USER_TZ`` then drop the tzinfo, matching the
journal's own naive-local Log entries.

``config.USER_TZ`` is read **inside** each function (call time), never at import,
so importing this module stays cheap and does not pull config off disk — the same
discipline ``config.py``'s lazy ``USER_TZ`` getter exists to preserve.
"""

from __future__ import annotations

from datetime import datetime


def parse_iso(value: str | datetime | None) -> datetime | None:
    """Parse an ISO timestamp (tolerating a trailing ``Z``) to a datetime.

    Passes a ``datetime`` through unchanged and maps falsy / unparseable input
    to ``None``, so callers can feed it raw cache values without pre-checking.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def to_local_naive(dt: datetime | None) -> datetime | None:
    """Convert a datetime to the user's local timezone, tzinfo stripped.

    ``None`` passes through. A naive datetime is assumed to already be UTC
    (every collector source is UTC-aware or UTC-derived), making this a total
    function. The result is naive local wall-clock time — consistent with the
    journal's Log entries and git's local commit timestamps.
    """
    if dt is None:
        return None
    from datetime import timezone

    from work_buddy.config import USER_TZ

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(USER_TZ).replace(tzinfo=None)


def format_local(
    value: str | datetime | None,
    fmt: str = "%Y-%m-%d %H:%M",
    *,
    fallback: str = "",
) -> str:
    """Render *value* (ISO string or datetime) as local-naive wall-clock time.

    Returns *fallback* when *value* is absent or unparseable.
    """
    local = to_local_naive(parse_iso(value))
    if local is None:
        return fallback
    return local.strftime(fmt)


def format_session_span(
    start: str | datetime | None,
    end: str | datetime | None,
    *,
    fallback: str = "",
    empty: str = "",
) -> str:
    """Render when a session happened, from its start/end instants, in local time.

    Same-day spans collapse to ``YYYY-MM-DD HH:MM–HH:MM``; cross-day spans show
    both dates. With only one endpoint, renders that instant. With neither,
    returns *fallback* if given, else *empty* — the only difference between the
    two historical renderers this replaces (chat used ``""``, the session
    summary used ``"—"``).
    """
    s = to_local_naive(parse_iso(start))
    e = to_local_naive(parse_iso(end))
    if s and e:
        if s.date() == e.date():
            return f"{s.strftime('%Y-%m-%d %H:%M')}–{e.strftime('%H:%M')}"
        return f"{s.strftime('%Y-%m-%d %H:%M')}–{e.strftime('%Y-%m-%d %H:%M')}"
    if s:
        return s.strftime("%Y-%m-%d %H:%M")
    if e:
        return e.strftime("%Y-%m-%d %H:%M")
    return fallback or empty
