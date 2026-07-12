"""Disk-backed cache for :class:`ContextSection` payloads.

Each source writes one JSON file per ``(source, bucket)`` pair at
``<data_root>/context/<source>/<bucket>.json`` with a sibling ``.meta.json``
recording the fetch timestamp and the request fingerprint. The
collector checks ``mtime + max_age_seconds`` and ``source.is_stale()``
before deciding to re-fetch.

The bucket key is a short stable hash of the fetch-affecting parts of
a :class:`ContextRequest` (target_date / window_days / per-source
custom params). Rendering-only fields (depth, max_chars) are
deliberately excluded — they don't change what we fetch, only what
we render.

Writes are atomic — we write to ``.tmp`` and rename — so a crash
mid-fetch can't corrupt a cached section.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.context.types import ContextRequest, ContextSection
from work_buddy.logging_config import get_logger
from work_buddy.paths import data_dir

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Cache root
# ---------------------------------------------------------------------------


def _cache_root() -> Path:
    """``<data_root>/context/`` under the configured data dir."""
    root = data_dir("context")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _source_dir(source: str) -> Path:
    """``<data_root>/context/<source>/`` — ensured to exist."""
    p = _cache_root() / source
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Bucket key
# ---------------------------------------------------------------------------


def bucket_key(source: str, request: ContextRequest) -> str:
    """Stable short hash identifying a cache bucket for ``source``.

    Folds only the fetch-affecting parameters — target_date,
    window_days, the explicit since/until window, and this source's
    custom params. Rendering options (depth, max_chars) are irrelevant
    here.
    """
    payload = {
        "source": source,
        "target_date": request.target_date.isoformat() if request.target_date else None,
        "window_days": request.window_days,
        "since": request.since.isoformat() if request.since else None,
        "until": request.until.isoformat() if request.until else None,
        "custom": request.custom_for(source),
    }
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Meta sidecar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheMeta:
    """Sidecar metadata recording when a bucket was last fetched.

    We store ``written_at`` (server time) so age checks don't rely on
    filesystem mtime, which can skew across network mounts / VM
    clock-drift. ``request_fingerprint`` is the same hash used for the
    bucket key — stored so debugging tools can inspect what was cached.
    """

    written_at: float            # unix timestamp
    request_fingerprint: str
    version: int = 1


def _meta_path(section_path: Path) -> Path:
    return section_path.with_suffix(".meta.json")


def _read_meta(path: Path) -> CacheMeta | None:
    meta_file = _meta_path(path)
    if not meta_file.exists():
        return None
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return CacheMeta(
            written_at=float(data.get("written_at", 0.0)),
            request_fingerprint=str(data.get("request_fingerprint", "")),
            version=int(data.get("version", 1)),
        )
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.debug("Context cache meta unreadable at %s: %s", meta_file, exc)
        return None


def _write_meta(path: Path, fingerprint: str) -> None:
    meta_file = _meta_path(path)
    tmp = meta_file.with_suffix(".tmp")
    payload = {
        "written_at": time.time(),
        "request_fingerprint": fingerprint,
        "version": 1,
    }
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(meta_file)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def section_path(source: str, bucket: str) -> Path:
    """``<data_root>/context/<source>/<bucket>.json`` — ensured parent exists."""
    return _source_dir(source) / f"{bucket}.json"


def read_cached(source: str, bucket: str) -> ContextSection | None:
    """Return the cached section or ``None`` if none exists / is unreadable."""
    path = section_path(source, bucket)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Context cache unreadable at %s: %s", path, exc)
        return None
    try:
        return ContextSection.from_dict(data)
    except Exception as exc:
        logger.warning("Context cache malformed at %s: %s", path, exc)
        return None


def write_cached(
    section: ContextSection,
    bucket: str,
) -> Path:
    """Persist a section atomically. Returns the path written."""
    path = section_path(section.source, bucket)
    tmp = path.with_suffix(".tmp")
    payload = section.to_dict()
    tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    tmp.replace(path)
    _write_meta(path, fingerprint=bucket)
    return path


def cache_age_seconds(source: str, bucket: str) -> float | None:
    """Seconds since the cached section was written, or ``None`` if absent.

    Prefers the sidecar ``.meta.json`` written-at timestamp over
    filesystem mtime. Falls back to mtime when the sidecar is missing
    (older entries written before this module existed).
    """
    path = section_path(source, bucket)
    if not path.exists():
        return None
    meta = _read_meta(path)
    if meta is not None:
        return max(0.0, time.time() - meta.written_at)
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def is_fresh_enough(
    source: str,
    bucket: str,
    max_age_seconds: int | None,
) -> bool:
    """Does a cache entry exist and is it within ``max_age_seconds``?

    ``max_age_seconds=None`` → never fresh (always re-fetch).
    ``max_age_seconds=0`` → any existing entry is fresh enough.
    """
    if max_age_seconds is None:
        return False
    age = cache_age_seconds(source, bucket)
    if age is None:
        return False
    if max_age_seconds == 0:
        # Zero means "any cached entry is fresh enough" — an explicit
        # opt-in to use the cache regardless of age. Callers who need
        # "strictly fresh" should pass ``None`` instead.
        return True
    return age <= max_age_seconds


def evict(source: str, bucket: str | None = None) -> int:
    """Remove cached sections. Returns the number of files deleted.

    ``bucket=None`` removes every bucket under the source. Useful for
    schema migrations or after a source's render logic changes shape.
    """
    path = _source_dir(source)
    if not path.exists():
        return 0
    deleted = 0
    if bucket is None:
        # ``*.json`` matches both ``<bucket>.json`` and ``<bucket>.meta.json``;
        # the second glob filters the sidecars so we only count buckets.
        for f in path.glob("*.json"):
            if f.name.endswith(".meta.json"):
                # Sidecar — delete silently; not counted as a bucket.
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
    else:
        target = section_path(source, bucket)
        if target.exists():
            try:
                target.unlink()
                deleted += 1
            except OSError:
                pass
        meta = _meta_path(target)
        if meta.exists():
            try:
                meta.unlink()
            except OSError:
                pass
    return deleted


def _meta_written_at_iso(source: str, bucket: str) -> str | None:
    """Debug helper — ISO-formatted written-at from the sidecar, or None."""
    path = section_path(source, bucket)
    meta = _read_meta(path)
    if meta is None:
        return None
    return datetime.fromtimestamp(meta.written_at, tz=timezone.utc).isoformat()
