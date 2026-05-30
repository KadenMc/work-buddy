"""Stable identity for calendar events across calendars and adapters.

Exact analog of email's ``Message-ID``-first :func:`stable_key_for`.

The same logical meeting can appear on the user's primary calendar *and* on a
shared calendar (SickKids, UHN): it carries one RFC 5545 ``iCalUID`` but a
different ``provider_event_id`` on each calendar. So:

* ``provider_event_id`` addresses a **row** (one event on one calendar).
* ``ical_uid`` deduplicates the same meeting **across rows**.

Prefer the iCalUID when present (``ical:<uid>``); fall back to a provider-local
composite (``loc:<provider>:<calendar_id>:<event_id>``) when it's absent. The
Obsidian-bridge adapter's payload lacks ``iCalUID``, so bridge events use the
``loc:`` form; a future native adapter supplies real UIDs and gets ``ical:``.
"""

from __future__ import annotations


def stable_key_for(
    *,
    ical_uid: str | None,
    provider: str,
    calendar_id: str,
    provider_event_id: str,
) -> str:
    """Compute a durable cross-calendar key for one event.

    ``ical:<uid>`` when an iCalUID is present; otherwise the provider-local
    ``loc:<provider>:<calendar_id>:<event_id>`` composite. Deterministic:
    re-collecting the same event yields the same key.
    """
    if ical_uid and ical_uid.strip():
        return f"ical:{ical_uid.strip()}"
    return f"loc:{provider}:{calendar_id}:{provider_event_id}"
