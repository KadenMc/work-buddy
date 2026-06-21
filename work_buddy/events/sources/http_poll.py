"""Fetch the raw payload for a source.

``http_poll`` GETs the URL with ``httpx`` (mirroring the websearch fetch
pattern); ``fake`` returns a fixture from the source's frontmatter
(``fake_payload``) for tests / dry runs with no network.
"""

from __future__ import annotations

from typing import Any

from work_buddy.events.sources.definition import EventSourceDef

_TIMEOUT_S = 15.0


def fetch_payload(source: EventSourceDef) -> Any:
    """Return the raw payload: parsed JSON for ``json_path`` extraction, else
    the response text. Raises on a network/HTTP error (the poller catches it)."""
    if source.type == "fake":
        return source.raw.get("fake_payload")
    if source.type == "http_poll":
        import httpx

        with httpx.Client(
            timeout=_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "work-buddy-events/1.0"},
        ) as client:
            resp = client.get(source.url)
        resp.raise_for_status()
        if source.extract_mode == "json_path":
            return resp.json()
        return resp.text
    raise ValueError(f"source type {source.type!r} cannot be polled")
