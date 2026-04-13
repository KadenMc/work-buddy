"""Persistent LLM result cache with content-aware invalidation.

Stores results in a JSON file at repo root (``.llm_cache.json``).
Cache entries are keyed by ``task_id`` and optionally validated against
a ``content_hash``. When the hash differs, a SimHash comparison
(locality-sensitive hashing with Hamming distance) determines if the
content changed enough to warrant re-running the LLM call.

SimHash produces a 64-bit fingerprint where similar documents differ
in only a few bits. Hamming distance ≤ 3 = near-identical (timestamp
rotation, ad changes). Distance > 3 = meaningfully different content.
This is the same approach Google uses for web crawl deduplication.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from simhash import Simhash

from work_buddy.config import load_config
from work_buddy.paths import resolve

logger = logging.getLogger(__name__)

_CACHE_PATH = resolve("cache/llm")

# Default: Hamming distance ≤ 3 bits out of 64 = near-identical
_DEFAULT_HAMMING_THRESHOLD = 3


def _hamming_threshold() -> int:
    cfg = load_config()
    return cfg.get("llm", {}).get("simhash_hamming_threshold", _DEFAULT_HAMMING_THRESHOLD)


def _normalize_for_simhash(text: str) -> str:
    """Normalize text before fingerprinting to reduce noise.

    Strips isolated numbers (timestamps, view counts, ad impression IDs),
    collapses whitespace, and lowercases.
    """
    text = " ".join(text.split())
    text = re.sub(r"\b\d{1,6}\b", "", text)
    return text.lower()


def _compute_simhash(text: str) -> int:
    """Compute a 64-bit SimHash fingerprint using 3-word shingles."""
    normalized = _normalize_for_simhash(text)
    tokens = normalized.split()
    if len(tokens) < 3:
        return Simhash(tokens).value
    shingles = [" ".join(tokens[i : i + 3]) for i in range(len(tokens) - 2)]
    return Simhash(shingles).value


def _hamming_distance(h1: int, h2: int) -> int:
    """Count differing bits between two 64-bit hashes."""
    return bin(h1 ^ h2).count("1")


def _read_cache() -> dict[str, dict]:
    """Read the cache file. Returns {task_id: entry}."""
    if not _CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_cache(cache: dict[str, dict]) -> None:
    """Write cache atomically."""
    temp = _CACHE_PATH.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        temp.replace(_CACHE_PATH)
    except OSError as e:
        logger.error("Failed to write LLM cache: %s", e)


def get(
    task_id: str,
    content_hash: str | None = None,
    content_sample: str | None = None,
) -> dict | None:
    """Look up a cached result.

    Returns the cache entry dict if valid, or None on miss.

    Validation logic:
    1. Entry must exist and not be expired
    2. If ``content_hash`` matches: hit (exact match)
    3. If ``content_hash`` differs but ``content_sample`` is provided:
       compute SimHash of both samples and compare Hamming distance.
       If distance ≤ threshold (default 3 bits): hit (trivial change)
    4. Otherwise: miss (content changed meaningfully)
    """
    cache = _read_cache()
    entry = cache.get(task_id)
    if entry is None:
        return None

    # Check expiry
    expires_at = entry.get("expires_at", "")
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) < datetime.now():
                return None
        except ValueError:
            pass

    # No content_hash check needed (caller doesn't care about content changes)
    if content_hash is None:
        return entry

    # Exact hash match
    if entry.get("content_hash") == content_hash:
        return entry

    # SimHash fuzzy match: compare content fingerprints
    if content_sample and entry.get("content_sample"):
        cached_simhash = entry.get("simhash")
        if cached_simhash is not None:
            new_simhash = _compute_simhash(content_sample)
            distance = _hamming_distance(cached_simhash, new_simhash)
            threshold = _hamming_threshold()
            if distance <= threshold:
                logger.debug(
                    "Cache SimHash hit for %s (hamming=%d, threshold=%d)",
                    task_id, distance, threshold,
                )
                return entry
            else:
                logger.debug(
                    "Cache SimHash miss for %s (hamming=%d, threshold=%d)",
                    task_id, distance, threshold,
                )

    # Content changed meaningfully
    return None


def put(
    task_id: str,
    result: dict,
    content_hash: str | None,
    content_sample: str | None,
    ttl_minutes: int,
    model: str = "",
    tokens: dict | None = None,
) -> None:
    """Store a result in the cache, including SimHash fingerprint for fuzzy matching."""
    cache = _read_cache()

    sample = content_sample[:500] if content_sample else None
    simhash_value = _compute_simhash(sample) if sample else None

    now = datetime.now()
    cache[task_id] = {
        "result": result,
        "content_hash": content_hash,
        "content_sample": sample,
        "simhash": simhash_value,
        "model": model,
        "tokens": tokens or {},
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
    }

    _write_cache(cache)


def prune() -> int:
    """Remove expired cache entries. Returns count removed."""
    cache = _read_cache()
    now = datetime.now()
    to_remove = []
    for task_id, entry in cache.items():
        expires_at = entry.get("expires_at", "")
        if expires_at:
            try:
                if datetime.fromisoformat(expires_at) < now:
                    to_remove.append(task_id)
            except ValueError:
                to_remove.append(task_id)

    for task_id in to_remove:
        del cache[task_id]

    if to_remove:
        _write_cache(cache)
        logger.info("Pruned %d expired LLM cache entries", len(to_remove))

    return len(to_remove)
