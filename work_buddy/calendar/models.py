"""Provider-agnostic calendar shapes.

Designed so a Google-native / Microsoft Graph / CalDAV / ICS backend could
replace the Obsidian-bridge adapter without changing any consumer of these
dataclasses (the calendar collector / morning bundle, coverage reporting).
Mirrors the structure of :mod:`work_buddy.email.models`.

Timezone discipline
-------------------
``EventTime`` carries one of two shapes, never both:

* **Timed** — ``dt`` is an *offset-aware* :class:`datetime`. The offset is the
  provider's authoritative offset for that instant; ``tz_name`` is the IANA
  zone string when the provider supplies one (the bridge does not, native
  will). We **never** call ``.astimezone()`` on load — the wall-clock time and
  offset the provider sent are preserved verbatim so display matches the
  source calendar.
* **All-day** — ``date`` is a ``YYYY-MM-DD`` string and ``dt`` is ``None``.
  All-day events *float*: they carry no timezone, by design.

Stable keys
-----------
``CalendarEvent.stable_key`` is the durable cross-calendar/-adapter identifier.
See :func:`work_buddy.calendar.identity.stable_key_for`: ``ical:<uid>`` when an
RFC 5545 iCalUID is present, else ``loc:<provider>:<calendar_id>:<event_id>``.
``provider_event_id`` addresses a *row* in one calendar; ``ical_uid``
deduplicates the same logical meeting across calendars/adapters.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class CalendarRef:
    """A handle to one calendar the provider exposes."""

    id: str
    name: str
    provider: str
    is_primary: bool = False
    access_role: str = ""        # "owner" | "writer" | "reader" | "freeBusyReader" | ""
    is_shared: bool = False      # not primary AND visible to others (escalates write consent)
    color: str | None = None     # provider background color, hex or None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CalendarRef:
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            provider=d.get("provider", ""),
            is_primary=bool(d.get("is_primary", False)),
            access_role=d.get("access_role", "") or "",
            is_shared=bool(d.get("is_shared", False)),
            color=d.get("color"),
        )


@dataclass
class EventTime:
    """One endpoint (start or end) of an event.

    Exactly one of ``dt`` (timed) or ``date`` (all-day) is set. See the module
    docstring for the timezone contract.
    """

    dt: datetime | None = None       # offset-aware; None for all-day
    date: str | None = None          # "YYYY-MM-DD"; None for timed
    tz_name: str | None = None       # IANA zone name when the provider supplies one

    @property
    def is_all_day(self) -> bool:
        return self.date is not None and self.dt is None

    @property
    def date_key(self) -> str:
        """The calendar day this endpoint falls on (``YYYY-MM-DD``).

        For timed events this is the *wall-clock* date in the provider's
        offset — not converted to any other zone — so grouping-by-day matches
        the source calendar.
        """
        if self.dt is not None:
            return self.dt.strftime("%Y-%m-%d")
        return self.date or "unknown"

    @property
    def hhmm(self) -> str | None:
        """``HH:MM`` for a timed endpoint; ``None`` for all-day."""
        if self.dt is not None:
            return self.dt.strftime("%H:%M")
        return None

    @property
    def sort_value(self) -> str:
        """A monotonic string key for ordering (all-day sorts before timed
        on the same day because a bare date < an ISO datetime)."""
        if self.dt is not None:
            return self.dt.isoformat()
        return self.date or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dt": self.dt.isoformat() if self.dt is not None else None,
            "date": self.date,
            "tz_name": self.tz_name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> EventTime:
        if not d:
            return cls()
        raw = d.get("dt")
        dt = datetime.fromisoformat(raw) if raw else None  # preserves offset; no astimezone
        return cls(dt=dt, date=d.get("date"), tz_name=d.get("tz_name"))


@dataclass
class CalendarEvent:
    """One event, normalized across providers — what the bundle/planner render."""

    stable_key: str
    provider: str
    calendar_id: str
    provider_event_id: str
    summary: str
    start: EventTime
    end: EventTime
    status: str = "confirmed"
    location: str = ""
    description: str = ""
    ical_uid: str = ""
    html_link: str = ""
    calendar_name: str = ""
    transparency: str = ""           # "" | "opaque" (busy) | "transparent" (free)
    wb_origin: bool = False          # event created by work-buddy (extendedProperties marker)

    @property
    def is_all_day(self) -> bool:
        return self.start.is_all_day

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = self.start.to_dict()
        d["end"] = self.end.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CalendarEvent:
        return cls(
            stable_key=d["stable_key"],
            provider=d.get("provider", ""),
            calendar_id=d.get("calendar_id", ""),
            provider_event_id=d.get("provider_event_id", ""),
            summary=d.get("summary", ""),
            start=EventTime.from_dict(d.get("start")),
            end=EventTime.from_dict(d.get("end")),
            status=d.get("status", "confirmed") or "confirmed",
            location=d.get("location", "") or "",
            description=d.get("description", "") or "",
            ical_uid=d.get("ical_uid", "") or "",
            html_link=d.get("html_link", "") or "",
            calendar_name=d.get("calendar_name", "") or "",
            transparency=d.get("transparency", "") or "",
            wb_origin=bool(d.get("wb_origin", False)),
        )


@dataclass
class CoverageReport:
    """Which calendars WB can actually see, and how many events each yielded.

    Distinguishes "the bridge is down" from "this calendar is blacklisted" from
    "this one calendar errored while the rest succeeded". ``errored`` carries
    per-calendar fetch failures so a single bad calendar degrades visibly
    rather than failing the whole window.
    """

    window: dict[str, str]                                  # {"start": ..., "end": ...}
    subscribed: list[CalendarRef] = field(default_factory=list)
    blacklisted: list[str] = field(default_factory=list)    # calendar ids the plugin hides
    per_calendar_counts: dict[str, int] = field(default_factory=dict)
    errored: dict[str, str] = field(default_factory=dict)   # calendar_id -> error message
    total_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": dict(self.window),
            "subscribed": [c.to_dict() for c in self.subscribed],
            "blacklisted": list(self.blacklisted),
            "per_calendar_counts": dict(self.per_calendar_counts),
            "errored": dict(self.errored),
            "total_events": self.total_events,
        }
