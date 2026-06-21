"""The reconciling poll loop for pull sources.

`poll_source`: fetch → extract the watched value → diff its content hash vs the
stored cursor → on a **meaningful change** (and not the baseline first
observation, unless ``cursor.from == all``) publish an
``ai.workbuddy.source.<name>.changed`` Event and advance the cursor. The cursor
advances only *after* the poll completes, so a crash re-fetches (idempotent via
the spine's inbox dedup).

`dry_run` runs the whole path with **zero side effects** (no publish, no state
write) — the preview the `/wb-event-new` authoring loop shows before activating.

`poll_due_sources` is the tick the cron job fires: load all sources, poll each
enabled one whose interval has elapsed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from work_buddy.events.sources.definition import EventSourceDef
from work_buddy.events.sources.extract import content_hash, extract_value
from work_buddy.events.sources.http_poll import fetch_payload
from work_buddy.events.sources.state import load_state, save_state
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_due(last_polled_iso: str | None, interval_s: int, now: datetime | None = None) -> bool:
    if not last_polled_iso:
        return True
    try:
        last = datetime.fromisoformat(last_polled_iso)
    except (ValueError, TypeError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return ((now or _now()) - last).total_seconds() >= interval_s


def poll_source(
    source: EventSourceDef,
    *,
    fetch: Callable[[EventSourceDef], Any] = fetch_payload,
    publish: Callable[[Any], None] | None = None,
    state_directory=None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Poll one source once. See module docstring for semantics."""
    if publish is None:
        from work_buddy.events import dispatcher

        publish = dispatcher.publish

    state = load_state(source.name, state_directory)
    prev_value = state.get("last_value")
    prev_hash = state.get("last_hash")
    is_first = prev_hash is None

    try:
        payload = fetch(source)
    except Exception as exc:  # noqa: BLE001 — a fetch failure is non-fatal
        logger.warning("event source %s: fetch failed: %s", source.name, exc)
        return {"polled": True, "error": f"fetch failed: {exc}", "changed": False, "emitted": False}

    value = extract_value(source.extract_mode, payload, path=source.extract_path)
    new_hash = content_hash(value)
    changed = new_hash != prev_hash
    fire = changed and (not is_first or source.cursor_from == "all")

    result: dict[str, Any] = {
        "polled": True,
        "changed": changed,
        "is_first": is_first,
        "value": value,
        "prev": prev_value,
        "emitted": False,
    }

    if fire:
        from work_buddy.events.envelope import new_event

        evt = new_event(
            source.source_uri,
            source.event_type,
            data={"current": value, "prev": prev_value, "source_name": source.name},
            modality="pull",
        )
        result["would_emit"] = evt.projection_payload()
        if not dry_run:
            publish(evt)
            result["emitted"] = True
            result["event_id"] = evt.id

    if not dry_run:
        save_state(
            source.name,
            {"last_polled": _now().isoformat(), "last_value": value, "last_hash": new_hash},
            state_directory,
        )
    return result


def dry_run(source: EventSourceDef, **kwargs) -> dict[str, Any]:
    """Poll once with zero side effects — the `/wb-event-new` preview."""
    return poll_source(source, dry_run=True, **kwargs)


def poll_due_sources(now: datetime | None = None, **kwargs) -> dict[str, Any]:
    """Load all sources; poll each enabled one whose interval has elapsed.
    The tick the `event-source-poll` cron job fires."""
    from work_buddy.events.sources.loader import load_event_sources

    defs, errors = load_event_sources()
    now = now or _now()
    polled: dict[str, dict] = {}
    for source in defs:
        if not source.enabled:
            continue
        st = load_state(source.name)
        if not _is_due(st.get("last_polled"), source.interval_s, now):
            continue
        polled[source.name] = poll_source(source, **kwargs)
    return {"sources": len(defs), "errors": errors, "polled": polled}
