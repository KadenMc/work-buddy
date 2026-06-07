"""Opt-in, artifact-managed search cache.

**Default is no-store.** The agent-facing ``web_search`` capability never
caches — results go straight back to the caller (ephemeral). Only a *reuse*
consumer (a poll-diff watcher, or rate-limit relief) passes ``cache=True`` to
:func:`work_buddy.websearch.router.search`, which stores the **structured
``SearchHit``s** (never raw Markdown / page text) keyed by normalized query, with
a short TTL.

Storage is artifact-managed (mirrors ``work_buddy/llm/cache.py``): a
``JsonRecordsStorage(DICT) + Lifecycle(PerRecordTtl, Delete)`` registered as
``websearch-cache`` so it rides the single artifact cleanup tick — no bespoke
pruner. Lookups also enforce the TTL inline so an expired entry is never served
between sweeps.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from work_buddy.paths import resolve
from work_buddy.websearch.models import SearchHit

log = logging.getLogger(__name__)

_CACHE_PATH = resolve("cache/websearch")
_DEFAULT_TTL_HOURS = 12


# ---------------------------------------------------------------------------
# Key + persistence
# ---------------------------------------------------------------------------


def _normalize_query(q: str) -> str:
    return " ".join((q or "").lower().split())


def cache_key(query: str, *, max_results: int, time_range: str | None) -> str:
    """Stable key. Includes max_results + time_range so a smaller/older cached
    result set isn't served for a request that asked for more / a different
    window."""
    return f"{_normalize_query(query)}|n={int(max_results)}|t={time_range or ''}"


def _read() -> dict[str, dict]:
    if not _CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write(cache: dict[str, dict]) -> None:
    tmp = _CACHE_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except OSError as exc:
        log.error("Failed to write websearch cache: %s", exc)


def _expired(entry: dict, now: datetime) -> bool:
    exp = entry.get("expires_at", "")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(exp) <= now
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get(query: str, *, max_results: int, time_range: str | None) -> list[SearchHit] | None:
    """Return cached hits for this query, or ``None`` on miss/expiry."""
    key = cache_key(query, max_results=max_results, time_range=time_range)
    entry = _read().get(key)
    if entry is None or _expired(entry, datetime.now(timezone.utc)):
        return None
    try:
        return [SearchHit(**h) for h in entry.get("hits", [])]
    except (TypeError, ValueError):
        return None  # malformed record — treat as miss


def put(
    query: str,
    hits: list[SearchHit],
    *,
    provider: str,
    max_results: int,
    time_range: str | None,
    ttl_hours: int | None = None,
) -> None:
    """Store structured hits under the query key with a TTL."""
    ttl = _DEFAULT_TTL_HOURS if ttl_hours is None else int(ttl_hours)
    now = datetime.now(timezone.utc)
    key = cache_key(query, max_results=max_results, time_range=time_range)
    cache = _read()
    cache[key] = {
        "hits": [asdict(h) for h in hits],
        "provider": provider,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=ttl)).isoformat(),
    }
    _write(cache)


# ---------------------------------------------------------------------------
# Lifecycle registration — websearch-cache artifact (spec step 12)
# ---------------------------------------------------------------------------


def _register_websearch_cache_artifact() -> None:
    try:
        from work_buddy.artifacts import (
            Artifact,
            Delete,
            JsonRecordsShape,
            JsonRecordsStorage,
            Lifecycle,
            PerRecordTtl,
            register_artifact,
        )

        register_artifact(Artifact(
            name="websearch-cache",
            storage=JsonRecordsStorage(
                path=_CACHE_PATH,
                shape=JsonRecordsShape.DICT,
                artifact_name="websearch-cache",
            ),
            lifecycle=Lifecycle(
                trigger=PerRecordTtl(ttl_field="expires_at"),
                action=Delete(),
            ),
        ))
    except Exception as exc:  # pragma: no cover — defensive, mirrors llm/cache.py
        log.warning("Failed to register websearch-cache artifact: %s", exc)


_register_websearch_cache_artifact()
