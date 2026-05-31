"""Read-only capability callables for the calendar integration.

Registered as ops in :mod:`work_buddy.mcp_server.ops.calendar_ops` and declared
as ``kind: capability`` knowledge units (gated by the ``google_calendar`` tool
probe). All callables instantiate the configured provider on demand and return
JSON-serialisable dicts, mirroring :mod:`work_buddy.email.capabilities`.

Read-only surface:
  - ``calendar_health``       Provider readiness payload.
  - ``calendar_list_events``  Events in a date window (optionally per calendar).
  - ``calendar_get_event``    One event by id.
  - ``calendar_coverage``     Which calendars are visible / blacklisted / errored.

Write surface (heavy, per-change consent):
  - ``create_calendar_event``  Create an event on a real calendar.
  - ``update_calendar_event``  Modify an event (before→after diff in the prompt).
  - ``delete_calendar_event``  Delete an event (always re-prompts).

Consent lives here — one layer above the adapter — so every provider
(obsidian_bridge, google_native, …) inherits identical gating. The four
heavy-consent properties: per-write ``consent_weight="high"`` (never carried by a
workflow grant), a change-specific prompt body rendered from the actual pending
change, ``default_ttl=0`` on the destructive ops (always re-prompt), and a
shared-calendar escalation line.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from work_buddy.calendar.errors import CalendarError, CalendarEventNotFound
from work_buddy.calendar.models import CoverageReport
from work_buddy.calendar.provider import get_calendar_provider
from work_buddy.consent import requires_consent

log = logging.getLogger(__name__)


def _provider_or_error() -> tuple[Any, dict | None]:
    try:
        return get_calendar_provider(), None
    except CalendarError as exc:
        return None, {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


def _err(exc: CalendarError) -> dict:
    return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


# ---------------------------------------------------------------------------
# Read-only diagnostics
# ---------------------------------------------------------------------------


def calendar_health() -> dict:
    """Liveness probe — return the configured provider's readiness payload.

    Distinguishes "bridge down" from "no calendars" the way email_health does
    for the mail bridge."""
    provider, err = _provider_or_error()
    if err:
        return err
    try:
        return {"ok": True, "provider": provider.name, **provider.health()}
    except CalendarError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_calendar_events(
    *,
    start: str,
    end: str,
    calendar_ids: list[str] | None = None,
) -> dict:
    """Return events overlapping ``[start, end]`` (ISO ``YYYY-MM-DD`` dates).

    Optionally restrict to ``calendar_ids``. Each event is the canonical
    ``CalendarEvent.to_dict()`` shape (offset-aware times preserved)."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not start or not end:
        return {"ok": False, "error": "start and end (YYYY-MM-DD) are required",
                "error_kind": "bad_request"}
    try:
        events = provider.list_events(start=start, end=end, calendar_ids=calendar_ids)
        return {
            "ok": True,
            "provider": provider.name,
            "window": {"start": start, "end": end},
            "count": len(events),
            "events": [e.to_dict() for e in events],
        }
    except CalendarError as exc:
        return _err(exc)


def get_calendar_event(*, calendar_id: str, event_id: str) -> dict:
    """Fetch one event by its provider-local id (within the adapter's lookup
    window, since the plugin's getEvent is broken)."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not calendar_id or not event_id:
        return {"ok": False, "error": "calendar_id and event_id are required",
                "error_kind": "bad_request"}
    try:
        ev = provider.get_event(calendar_id=calendar_id, event_id=event_id)
        return {"ok": True, "provider": provider.name, "event": ev.to_dict()}
    except CalendarEventNotFound as exc:
        return _err(exc)
    except CalendarError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def build_coverage_report(provider, start: str, end: str) -> CoverageReport:
    """Compute coverage from a **single bulk fetch** and count per calendar.

    The bridge's ``get_events`` returns every subscribed calendar's events in
    one round-trip, so fetching per calendar would be N redundant calls. We
    bulk-fetch once and tally by ``calendar_id``; calendars with no events
    still appear (count 0). ``errored`` only populates on a *total* window
    failure — per-calendar error isolation becomes meaningful with the native
    adapter (separate API calls per calendar) and is wired then. ``subscribed``
    / ``blacklisted`` are best-effort and degrade independently.
    """
    try:
        subscribed = provider.list_calendars()
    except CalendarError:
        subscribed = []
    try:
        blacklisted = provider.blacklisted_calendar_ids()
    except CalendarError:
        blacklisted = []

    bl = set(blacklisted)
    per_calendar: dict[str, int] = {ref.id: 0 for ref in subscribed if ref.id not in bl}
    errored: dict[str, str] = {}
    total = 0
    try:
        events = provider.list_events(start=start, end=end)
        for e in events:
            if e.calendar_id in bl:
                continue
            per_calendar[e.calendar_id] = per_calendar.get(e.calendar_id, 0) + 1
            total += 1
    except CalendarError as exc:
        # Whole-window fetch failed — attribute to each visible calendar so the
        # report shows the gap rather than silently reporting zero events.
        for ref in subscribed:
            if ref.id not in bl:
                errored[ref.id] = str(exc)

    return CoverageReport(
        window={"start": start, "end": end},
        subscribed=subscribed,
        blacklisted=blacklisted,
        per_calendar_counts=per_calendar,
        errored=errored,
        total_events=total,
    )


def calendar_coverage(
    *,
    start: str | None = None,
    end: str | None = None,
    days: int = 7,
) -> dict:
    """Report which calendars WB can see and how many events each yields.

    Defaults to a ``today .. today+days`` window in the user's timezone when
    ``start`` / ``end`` are omitted."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not start or not end:
        from work_buddy import config
        today = datetime.now(config.USER_TZ).date()
        start = start or today.strftime("%Y-%m-%d")
        end = end or (today + timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        rep = build_coverage_report(provider, start, end)
        return {"ok": True, "provider": provider.name, **rep.to_dict()}
    except CalendarError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Writes — heavy, per-change consent
# ---------------------------------------------------------------------------
#
# The change-specific prompt body is rendered by the public capability (which has
# the call arguments), stashed in ``_PENDING_BODY``, and read no-arg by
# ``body_extras`` when the gateway builds the consent prompt. This works because
# the write capabilities declare **no** ``consent_operations`` in their knowledge
# units, so the gateway uses the *runtime* ``ConsentRequired`` path: the
# capability runs (stashing the body) and the inner ``@requires_consent`` raises,
# and only then is the prompt built — by which point the body is set. (Pre-flight
# consent, which fires before the capability runs, would render only the static
# reason.) ``_PENDING_BODY`` is process-global, mirroring the archive-consent
# precedent that re-derives its body from process state; the single-user sidecar
# makes cross-call clobber a non-issue, and each call overwrites before raising.

_PENDING_BODY: dict[str, str] = {}


def _body_for(op: str):
    def _extras() -> str:
        return _PENDING_BODY.get(op, "")
    return _extras


@requires_consent(
    operation="calendar.create_event",
    reason="Create a new event on one of your real calendars.",
    risk="high",
    consent_weight="high",
    default_ttl=10,
    body_extras=_body_for("calendar.create_event"),
)
def _gated_create(provider, **kw) -> dict:
    return provider.create_event(**kw)


@requires_consent(
    operation="calendar.update_event",
    reason="Modify an existing event on one of your real calendars.",
    risk="high",
    consent_weight="high",
    default_ttl=0,  # always re-prompt — an update can cancel or move an event
    body_extras=_body_for("calendar.update_event"),
)
def _gated_update(provider, *, calendar_id, event_id, changes, notify) -> dict:
    return provider.update_event(
        calendar_id=calendar_id, event_id=event_id, changes=changes, notify=notify
    )


@requires_consent(
    operation="calendar.delete_event",
    reason="Permanently delete an event from one of your real calendars.",
    risk="high",
    consent_weight="high",
    default_ttl=0,  # always re-prompt — deletion is irreversible
    body_extras=_body_for("calendar.delete_event"),
)
def _gated_delete(provider, *, calendar_id, event_id, notify) -> dict:
    return provider.delete_event(
        calendar_id=calendar_id, event_id=event_id, notify=notify
    )


# --- consent-body rendering -------------------------------------------------


def _resolve_calendar(provider, calendar_id: str | None):
    """Return the target ``CalendarRef`` (or the primary when ``calendar_id`` is
    None), or ``None`` if calendars can't be listed."""
    try:
        cals = provider.list_calendars()
    except CalendarError:
        return None
    if calendar_id is None:
        return next((c for c in cals if c.is_primary), cals[0] if cals else None)
    return next((c for c in cals if c.id == calendar_id), None)


def _zone_label() -> tuple[Any, str]:
    from work_buddy import config
    tz = config.USER_TZ
    return tz, getattr(tz, "key", str(tz))


def _fmt_when_args(start: str, end: str, all_day: bool) -> str:
    """Render a start/end pair (ISO strings from the caller) in ``USER_TZ``."""
    if all_day:
        return f"{start} → {end} (all day)"
    tz, zone = _zone_label()

    def _one(s: str):
        try:
            dt = datetime.fromisoformat(s)
            return (dt.replace(tzinfo=tz) if dt.tzinfo is None else dt).astimezone(tz)
        except (ValueError, TypeError):
            return None

    a, b = _one(start), _one(end)
    if a and b:
        return f"{a:%Y-%m-%d %H:%M}–{b:%H:%M} {zone}"
    return f"{start} → {end}"


def _fmt_when_event(ev) -> str:
    """Render a ``CalendarEvent``'s start/end in ``USER_TZ``."""
    if ev.start.is_all_day:
        return f"{ev.start.date} (all day)"
    tz, zone = _zone_label()
    a, b = ev.start.dt, ev.end.dt
    if a and b:
        return f"{a.astimezone(tz):%Y-%m-%d %H:%M}–{b.astimezone(tz):%H:%M} {zone}"
    return "(time unknown)"


def _event_time_iso(et) -> str:
    if et.is_all_day:
        return et.date or ""
    return et.dt.isoformat() if et.dt else ""


def _shared_line(ref) -> str:
    return "\n  ⚠ others can see this calendar" if (ref and ref.is_shared) else ""


def _create_body(summary: str, when: str, cal_name: str, ref) -> str:
    return (
        f"  → Create \"{summary}\" on **{cal_name}**\n"
        f"     When: {when}"
        + _shared_line(ref)
    )


def _delete_body(current, event_id: str, cal_name: str, ref) -> str:
    if current is not None:
        head = f"  → Delete \"{current.summary}\" from **{cal_name}**\n     When: {_fmt_when_event(current)}"
    else:
        head = f"  → Delete event `{event_id}` from **{cal_name}**"
    return head + _shared_line(ref)


def _update_body(current, changes: dict, cal_name: str, ref) -> str:
    title = f'"{current.summary}"' if current is not None else "this event"
    lines = [f"  → Modify {title} on **{cal_name}**"]
    if "summary" in changes and (current is None or changes["summary"] != current.summary):
        old = f'"{current.summary}"' if current is not None else "(current)"
        lines.append(f"     Title: {old} → \"{changes['summary']}\"")
    if "start" in changes or "end" in changes:
        before = _fmt_when_event(current) if current is not None else "(current)"
        new_start = changes.get("start") or (_event_time_iso(current.start) if current else "")
        new_end = changes.get("end") or (_event_time_iso(current.end) if current else "")
        all_day = bool(current and current.start.is_all_day) and "T" not in (new_start or "")
        after = _fmt_when_args(new_start, new_end, all_day)
        lines.append(f"     When: {before} → {after}")
    if "location" in changes:
        old = (current.location if current is not None else "") or "(none)"
        lines.append(f"     Location: {old} → {changes['location'] or '(none)'}")
    if "description" in changes:
        lines.append("     Description: (changed)")
    return "\n".join(lines) + _shared_line(ref)


# --- public write capabilities ----------------------------------------------


def create_calendar_event(
    *,
    summary: str,
    start: str,
    end: str,
    calendar_id: str | None = None,
    description: str = "",
    location: str = "",
    all_day: bool = False,
    timezone: str | None = None,
) -> dict:
    """Create an event on a real calendar (heavy per-change consent).

    ``start``/``end`` are ISO strings — date (``YYYY-MM-DD``) for all-day, else
    datetime. ``calendar_id`` defaults to the primary calendar. The consent
    prompt shows the target calendar, title, and start–end in ``USER_TZ``."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not summary or not start or not end:
        return {"ok": False, "error": "summary, start, and end are required",
                "error_kind": "bad_request"}
    ref = _resolve_calendar(provider, calendar_id)
    cal_name = ref.name if ref else (calendar_id or "your primary calendar")
    _PENDING_BODY["calendar.create_event"] = _create_body(
        summary, _fmt_when_args(start, end, all_day), cal_name, ref
    )
    try:
        result = _gated_create(
            provider, summary=summary, start=start, end=end, calendar_id=calendar_id,
            description=description, location=location, all_day=all_day, timezone=timezone,
        )
    except CalendarError as exc:
        return _err(exc)
    return {"ok": True, "provider": provider.name, "event": result}


def update_calendar_event(
    *,
    calendar_id: str,
    event_id: str,
    changes: dict,
    notify: bool = False,
) -> dict:
    """Modify an event on a real calendar (heavy per-change consent).

    ``changes`` is a partial dict (any of ``summary``/``start``/``end``/
    ``location``/``description``). The consent prompt shows a before→after diff;
    this op always re-prompts (an update can cancel or move an event)."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not calendar_id or not event_id:
        return {"ok": False, "error": "calendar_id and event_id are required",
                "error_kind": "bad_request"}
    if not isinstance(changes, dict) or not changes:
        return {"ok": False, "error": "changes must be a non-empty dict",
                "error_kind": "bad_request"}
    ref = _resolve_calendar(provider, calendar_id)
    cal_name = ref.name if ref else calendar_id
    try:
        current = provider.get_event(calendar_id=calendar_id, event_id=event_id)
    except CalendarError:
        current = None  # best-effort; the prompt degrades but still gates
    _PENDING_BODY["calendar.update_event"] = _update_body(current, changes, cal_name, ref)
    try:
        result = _gated_update(
            provider, calendar_id=calendar_id, event_id=event_id, changes=changes, notify=notify,
        )
    except CalendarError as exc:
        return _err(exc)
    return {"ok": True, "provider": provider.name, "event": result}


def delete_calendar_event(
    *,
    calendar_id: str,
    event_id: str,
    notify: bool = False,
) -> dict:
    """Delete an event from a real calendar (heavy per-change consent).

    The consent prompt shows the event being deleted; this op always re-prompts
    (deletion is irreversible)."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not calendar_id or not event_id:
        return {"ok": False, "error": "calendar_id and event_id are required",
                "error_kind": "bad_request"}
    ref = _resolve_calendar(provider, calendar_id)
    cal_name = ref.name if ref else calendar_id
    try:
        current = provider.get_event(calendar_id=calendar_id, event_id=event_id)
    except CalendarError:
        current = None
    _PENDING_BODY["calendar.delete_event"] = _delete_body(current, event_id, cal_name, ref)
    try:
        result = _gated_delete(
            provider, calendar_id=calendar_id, event_id=event_id, notify=notify,
        )
    except CalendarError as exc:
        return _err(exc)
    return {"ok": True, "provider": provider.name, "result": result}
