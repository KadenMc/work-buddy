"""Persistent LLM result cache with content-aware invalidation.

Stores results in a JSON file at ``<data_root>/cache/llm_cache.json``. Cache
entries are keyed by ``scoped_task_id`` and validated against an
``input_hash`` fingerprint of the prompt that produced them. When
``input_hash`` differs, the lookup falls back to a SimHash fuzzy-match
on the full input text — useful for tolerating trivial noise like
timestamp rotation without re-querying the model.

The cache is **content-aware by construction**: every caller must pass
an ``input_hash`` on both ``get`` and ``put``. The API intentionally has
no "skip the hash" path — that pattern shipped a silent-correctness bug
in an earlier iteration where task-id-only matching caused unrelated
calls at the same tier to share a cache slot.

### Scoping and provenance

The ``scoped_task_id`` passed by callers (typically built in
:mod:`work_buddy.llm.runner`) already carries backend + model + system
prompt hash. That means **system-prompt edits cleanly invalidate** the
cache by changing the scope: old entries become unreachable without
explicit invalidation. Each entry also stores ``system_hash`` and
``system_preview`` (first 500 chars of the system prompt) so an operator
tracing a stale result can identify which prompt revision produced it.

### Stored fields per entry

- ``result``: the cached value (caller-supplied; arbitrary JSON).
- ``input_hash``: SHA-256 of the user prompt. Exact-match check.
- ``input_simhash``: pre-computed SimHash of the user prompt (full text,
  not a truncated sample). Used for fuzzy matching when ``input_hash``
  disagrees.
- ``system_hash`` / ``system_preview``: provenance for the system prompt
  that produced this result.
- ``model`` / ``tokens`` / ``created_at`` / ``expires_at``: bookkeeping.

The input text itself is NOT persisted — only its hash and SimHash. The
SimHash is computed once at ``put`` time; lookup just needs an incoming
SimHash to Hamming-compare against the stored one.
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


# ---------------------------------------------------------------------------
# Fingerprint helpers (shared)
# ---------------------------------------------------------------------------


def _hamming_threshold() -> int:
    cfg = load_config()
    return cfg.get("llm", {}).get(
        "simhash_hamming_threshold", _DEFAULT_HAMMING_THRESHOLD,
    )


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


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _read_cache() -> dict[str, dict]:
    """Read the cache file. Returns ``{scoped_task_id: entry}``."""
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
        temp.write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8",
        )
        temp.replace(_CACHE_PATH)
    except OSError as e:
        logger.error("Failed to write LLM cache: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get(
    scoped_task_id: str,
    *,
    input_hash: str,
    input_text: str | None = None,
    hamming_threshold: int | None = None,
) -> dict | None:
    """Look up a cached result.

    Args:
        scoped_task_id: The fully-scoped cache key. Callers constructing
            these in the runner path should include backend, model,
            system-prompt hash, and their own task identifier so entries
            are partitioned correctly.
        input_hash: SHA-256 of the user prompt (required). Exact match
            against the stored hash is the fast path.
        input_text: Full user prompt text (optional). When provided and
            the exact-hash check misses, a SimHash fuzzy-match against
            the stored ``input_simhash`` is attempted. Omit only when
            the caller wants strict exact-match semantics.
        hamming_threshold: Override the config-level Hamming threshold
            for fuzzy matching. Rarely needed.

    Returns:
        The cache entry dict on hit, or ``None`` on miss (including
        "legacy-schema entry present — ignore").
    """
    cache = _read_cache()
    entry = cache.get(scoped_task_id)
    if entry is None:
        return None

    # Legacy-schema entries (pre-refactor): no ``input_hash`` field.
    # Treat as miss so old data ages out via TTL without polluting
    # the lookup path.
    if "input_hash" not in entry:
        return None

    # Check expiry. Boundary-inclusive (<=): an entry whose deadline is
    # exactly now() has used up its lifetime and should be treated as
    # expired. Strict < missed the boundary case where put-then-get
    # happened within a single clock tick (see t-96e45c67).
    expires_at = entry.get("expires_at", "")
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) <= datetime.now():
                return None
        except ValueError:
            pass

    # Exact input-hash match — fast path.
    if entry["input_hash"] == input_hash:
        return entry

    # Fuzzy SimHash match — only if caller supplied fresh input text.
    if input_text is not None:
        stored_simhash = entry.get("input_simhash")
        if stored_simhash is None:
            # Invariant violation — malformed entry. Treat as miss so
            # the next ``put`` overwrites with a well-formed record.
            return None
        incoming_simhash = _compute_simhash(input_text)
        threshold = (
            hamming_threshold
            if hamming_threshold is not None
            else _hamming_threshold()
        )
        distance = _hamming_distance(stored_simhash, incoming_simhash)
        if distance <= threshold:
            logger.debug(
                "Cache SimHash hit for %s (hamming=%d, threshold=%d)",
                scoped_task_id, distance, threshold,
            )
            return entry
        logger.debug(
            "Cache SimHash miss for %s (hamming=%d, threshold=%d)",
            scoped_task_id, distance, threshold,
        )

    return None


def put(
    scoped_task_id: str,
    *,
    result: dict,
    input_hash: str,
    input_text: str,
    system_hash: str,
    system_preview: str,
    ttl_minutes: int,
    model: str = "",
    tokens: dict | None = None,
) -> None:
    """Store a result in the cache.

    All fingerprint fields are required. The ``input_simhash`` is computed
    from ``input_text`` at put-time and stored — lookups never need the
    original text, only its precomputed fingerprint.

    Args:
        scoped_task_id: The fully-scoped cache key (see :func:`get`).
        result: The cached value.
        input_hash: SHA-256 of the user prompt.
        input_text: Full user prompt. Used only to compute the SimHash
            fingerprint; NOT persisted.
        system_hash: Short hash of the system prompt. Stored for
            provenance and typically also baked into ``scoped_task_id``
            by the caller.
        system_preview: First ~500 chars of the system prompt. Stored
            verbatim so operators can identify which prompt revision
            generated the cached result.
        ttl_minutes: Entry lifetime.
        model: The concrete model that produced the result.
        tokens: Optional ``{"input": N, "output": N}`` accounting.
    """
    cache = _read_cache()

    now = datetime.now()
    cache[scoped_task_id] = {
        "result": result,
        "input_hash": input_hash,
        "input_simhash": _compute_simhash(input_text),
        "system_hash": system_hash,
        "system_preview": system_preview,
        "model": model,
        "tokens": tokens or {},
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
    }

    _write_cache(cache)


def prune() -> int:
    """Remove expired and legacy-schema cache entries.

    Returns the count removed. Expired entries are any whose
    ``expires_at`` is in the past (or malformed). Legacy-schema entries
    are any missing the required ``input_hash`` field — these pre-date
    the content-aware refactor and are never reachable via :func:`get`,
    so they're safe to evict on sight.
    """
    cache = _read_cache()
    now = datetime.now()
    to_remove = []
    for scoped_task_id, entry in cache.items():
        # Legacy-schema eviction.
        if "input_hash" not in entry:
            to_remove.append(scoped_task_id)
            continue
        expires_at = entry.get("expires_at", "")
        if expires_at:
            try:
                # Boundary-inclusive (<=) to match get() — see t-96e45c67.
                if datetime.fromisoformat(expires_at) <= now:
                    to_remove.append(scoped_task_id)
            except ValueError:
                to_remove.append(scoped_task_id)

    for scoped_task_id in to_remove:
        del cache[scoped_task_id]

    if to_remove:
        _write_cache(cache)
        logger.info(
            "Pruned %d LLM cache entries (expired or legacy-schema)",
            len(to_remove),
        )

    return len(to_remove)
