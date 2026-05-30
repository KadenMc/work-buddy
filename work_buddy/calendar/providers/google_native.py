"""Native Google Calendar adapter (own-OAuth, direct REST via httpx).

Implements the read half of :class:`work_buddy.calendar.provider.CalendarProvider`
against the Google Calendar v3 REST API, mapping Google JSON to the canonical
models. Auth/refresh is delegated to :mod:`work_buddy.calendar.google_auth`
(``google-auth``); the only third-party HTTP surface is ``httpx``, already a core
dependency.

Why httpx-light over ``google-api-python-client``: the adapter hand-maps
JSON↔models either way, the REST surface is a handful of stable methods, and the
security-critical OAuth/refresh stays on Google's maintained libs. The cost is a
small bespoke request helper (below) — its one real footgun is that Google
overloads HTTP ``403``: ``rateLimitExceeded`` / ``userRateLimitExceeded`` are
*retryable*, while ``forbidden`` / insufficient-scope are *terminal*. We
disambiguate on ``error.errors[].reason``, not the status code. The repo's
asyncio resilience framework is off-limits (these methods are sync), so the retry
logic here is hand-rolled.

Writes (``events.insert`` / ``patch`` / ``delete``) mark WB-created events with
an ``extendedProperties.private.wb_origin`` flag; the heavy per-change consent
that gates them lives in :mod:`work_buddy.calendar.capabilities`, one layer up,
so this adapter is a dumb mechanism like the bridge.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from work_buddy.calendar.errors import (
    CalendarBridgeUnreachable,
    CalendarError,
    CalendarEventNotFound,
    CalendarProviderError,
)
from work_buddy.calendar.identity import stable_key_for
from work_buddy.calendar.models import CalendarEvent, CalendarRef, EventTime

_API_BASE = "https://www.googleapis.com/calendar/v3"
_MAX_RETRIES = 4
_SHARED_ROLES = {"reader", "writer", "freeBusyReader"}

# Indirection so tests can patch out real sleeping during backoff.
_sleep = time.sleep


class GoogleNativeCalendarProvider:
    """Read adapter over the Google Calendar v3 REST API."""

    name = "google_native"

    def __init__(self, cfg: dict[str, Any] | None = None, *, client=None, credentials=None):
        self._cfg = cfg or {}
        self._client = client            # injectable httpx.Client (tests use MockTransport)
        self._creds = credentials        # injectable credentials (tests use a fake)

    # --- plumbing ----------------------------------------------------------

    def _credentials(self):
        if self._creds is not None and getattr(self._creds, "valid", True):
            return self._creds
        from work_buddy.calendar import google_auth

        self._creds = google_auth.load_credentials(self._cfg)
        return self._creds

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=20.0)
        return self._client

    def _force_refresh(self) -> None:
        from google.auth.transport.requests import Request

        creds = self._creds
        if creds is not None and getattr(creds, "refresh_token", None):
            try:
                creds.refresh(Request())
                return
            except Exception:  # fall through to a full reload
                pass
        from work_buddy.calendar import google_auth

        self._creds = google_auth.load_credentials(self._cfg)

    @staticmethod
    def _error_reasons(resp: httpx.Response) -> list[str]:
        try:
            errs = resp.json().get("error", {}).get("errors", [])
            return [e.get("reason", "") for e in errs]
        except Exception:
            return []

    def _is_retryable_403(self, resp: httpx.Response) -> bool:
        return any(
            r in ("rateLimitExceeded", "userRateLimitExceeded")
            for r in self._error_reasons(resp)
        )

    def _error_from(self, resp: httpx.Response) -> CalendarError:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            detail = resp.text[:200]
        return CalendarProviderError(
            f"google_native: HTTP {resp.status_code} {detail}".strip()
        )

    def _backoff(self, resp: httpx.Response, attempt: int) -> None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = min(2 ** attempt + random.random(), 30.0)
        _sleep(delay)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        attempt: int = 0,
    ) -> dict:
        creds = self._credentials()
        client = self._get_client()
        headers = {"Authorization": f"Bearer {getattr(creds, 'token', '')}"}
        try:
            resp = client.request(
                method, _API_BASE + path, params=params, json=json_body, headers=headers,
            )
        except httpx.HTTPError as exc:
            raise CalendarBridgeUnreachable(
                f"google_native: could not reach the Calendar API: {exc}"
            ) from exc

        code = resp.status_code
        if code == 401 and attempt == 0:
            self._force_refresh()
            return self._request(method, path, params=params, json_body=json_body, attempt=attempt + 1)
        if code == 404:
            raise CalendarEventNotFound("google_native: not found")
        retryable = code in (429, 500, 502, 503, 504) or (code == 403 and self._is_retryable_403(resp))
        if retryable:
            if attempt < _MAX_RETRIES:
                self._backoff(resp, attempt)
                return self._request(method, path, params=params, json_body=json_body, attempt=attempt + 1)
            raise self._error_from(resp)
        if code >= 400:
            raise self._error_from(resp)
        if code == 204 or not resp.content:
            return {}
        return resp.json()

    def _paginate(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        page = dict(params)
        while True:
            data = self._request("GET", path, params=page)
            items.extend(data.get("items", []))
            token = data.get("nextPageToken")
            if not token:
                return items
            page = dict(params)
            page["pageToken"] = token

    # --- CalendarProvider: discovery + reads -------------------------------

    def health(self) -> dict:
        try:
            data = self._request("GET", "/users/me/calendarList", params={"maxResults": 250})
            return {"ready": True, "calendar_count": len(data.get("items", []))}
        except CalendarError as exc:
            return {"ready": False, "reason": str(exc)}

    def list_calendars(self) -> list[CalendarRef]:
        items = self._paginate("/users/me/calendarList", {"maxResults": 250})
        refs: list[CalendarRef] = []
        for c in items:
            primary = bool(c.get("primary"))
            role = c.get("accessRole", "") or ""
            refs.append(CalendarRef(
                id=c["id"],
                name=c.get("summaryOverride") or c.get("summary") or c["id"],
                provider=self.name,
                is_primary=primary,
                access_role=role,
                is_shared=(not primary) and role in _SHARED_ROLES,
                color=c.get("backgroundColor"),
            ))
        return refs

    def blacklisted_calendar_ids(self) -> list[str]:
        # Native lists exactly the calendars the user subscribes to; deselection
        # is a client-side concept the API doesn't expose, so nothing is hidden.
        return []

    def _window_rfc3339(self, start: str, end: str) -> tuple[str, str]:
        from work_buddy import config

        tz = config.USER_TZ
        t_min = datetime.fromisoformat(f"{start}T00:00:00").replace(tzinfo=tz)
        t_max = datetime.fromisoformat(f"{end}T00:00:00").replace(tzinfo=tz) + timedelta(days=1)
        return t_min.isoformat(), t_max.isoformat()

    def list_events(
        self,
        *,
        start: str,
        end: str,
        calendar_ids: list[str] | None = None,
    ) -> list[CalendarEvent]:
        wanted = set(calendar_ids) if calendar_ids is not None else None
        refs = self.list_calendars()
        names = {r.id: r.name for r in refs}
        t_min, t_max = self._window_rfc3339(start, end)
        out: list[CalendarEvent] = []
        for ref in refs:
            if wanted is not None and ref.id not in wanted:
                continue
            params = {
                "timeMin": t_min,
                "timeMax": t_max,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 2500,
            }
            from urllib.parse import quote

            path = f"/calendars/{quote(ref.id, safe='')}/events"
            for ev in self._paginate(path, params):
                if ev.get("status") == "cancelled":
                    continue
                out.append(self._to_event(ev, ref.id, names.get(ref.id, "")))
        out.sort(key=lambda e: e.start.sort_value)
        return out

    def get_event(self, *, calendar_id: str, event_id: str) -> CalendarEvent:
        from urllib.parse import quote

        path = f"/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}"
        ev = self._request("GET", path)
        name = next((r.name for r in self.list_calendars() if r.id == calendar_id), "")
        return self._to_event(ev, calendar_id, name)

    # --- mapping -----------------------------------------------------------

    @staticmethod
    def _event_time(node: dict | None) -> EventTime:
        if not node:
            return EventTime()
        if node.get("date"):
            return EventTime(date=node["date"])
        raw = node.get("dateTime")
        dt = datetime.fromisoformat(raw) if raw else None
        return EventTime(dt=dt, tz_name=node.get("timeZone"))

    def _to_event(self, ev: dict, calendar_id: str, calendar_name: str) -> CalendarEvent:
        ical_uid = ev.get("iCalUID", "") or ""
        event_id = ev.get("id", "")
        wb_origin = bool(
            (ev.get("extendedProperties", {}) or {}).get("private", {}).get("wb_origin")
        )
        return CalendarEvent(
            stable_key=stable_key_for(
                ical_uid=ical_uid, provider=self.name,
                calendar_id=calendar_id, provider_event_id=event_id,
            ),
            provider=self.name,
            calendar_id=calendar_id,
            provider_event_id=event_id,
            summary=ev.get("summary", "") or "",
            start=self._event_time(ev.get("start")),
            end=self._event_time(ev.get("end")),
            status=ev.get("status", "confirmed") or "confirmed",
            location=ev.get("location", "") or "",
            description=ev.get("description", "") or "",
            ical_uid=ical_uid,
            html_link=ev.get("htmlLink", "") or "",
            calendar_name=calendar_name,
            transparency=ev.get("transparency", "") or "",
            wb_origin=wb_origin,
        )

    # --- writes ------------------------------------------------------------

    def _default_timezone(self) -> str:
        from work_buddy import config

        return str(config.USER_TZ)

    @staticmethod
    def _time_body(value: str, all_day: bool, tz: str) -> dict:
        """Build a Google start/end node from an ISO string."""
        if all_day or (value and "T" not in value):
            return {"date": value}
        return {"dateTime": value, "timeZone": tz}

    def _primary_id(self) -> str:
        primary = next((r for r in self.list_calendars() if r.is_primary), None)
        if primary is None:
            raise CalendarProviderError("google_native: no primary calendar found")
        return primary.id

    def create_event(
        self,
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
        from urllib.parse import quote

        cal = calendar_id or self._primary_id()
        tz = timezone or self._default_timezone()
        body: dict[str, Any] = {
            "summary": summary,
            "start": self._time_body(start, all_day, tz),
            "end": self._time_body(end, all_day, tz),
            # Marker so WB-created events stay filterable later.
            "extendedProperties": {"private": {"wb_origin": "1"}},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        path = f"/calendars/{quote(cal, safe='')}/events"
        ev = self._request("POST", path, json_body=body)
        return {
            "success": True,
            "id": ev.get("id"),
            "summary": ev.get("summary", summary),
            "htmlLink": ev.get("htmlLink"),
            "calendarId": cal,
        }

    def update_event(
        self,
        *,
        calendar_id: str,
        event_id: str,
        changes: dict,
        notify: bool = False,
    ) -> dict:
        from urllib.parse import quote

        tz = self._default_timezone()
        body: dict[str, Any] = {}
        for key in ("summary", "location", "description"):
            if key in changes:
                body[key] = changes[key]
        if "start" in changes:
            body["start"] = self._time_body(changes["start"], False, tz)
        if "end" in changes:
            body["end"] = self._time_body(changes["end"], False, tz)
        path = f"/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}"
        # patch = merge (only the sent fields change), not a full replace.
        ev = self._request(
            "PATCH", path, params={"sendUpdates": "all" if notify else "none"}, json_body=body,
        )
        return {
            "success": True,
            "id": ev.get("id", event_id),
            "summary": ev.get("summary"),
            "htmlLink": ev.get("htmlLink"),
            "notified": notify,
        }

    def delete_event(
        self,
        *,
        calendar_id: str,
        event_id: str,
        notify: bool = False,
    ) -> dict:
        from urllib.parse import quote

        path = f"/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}"
        self._request(
            "DELETE", path, params={"sendUpdates": "all" if notify else "none"},
        )
        return {"success": True, "deleted_id": event_id, "notified": notify}
