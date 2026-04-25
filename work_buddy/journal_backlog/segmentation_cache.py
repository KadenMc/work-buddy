"""Content-addressable segmentation cache.

A layer above the generic LLM-prompt cache for the journal segmenter.
Stores groups as sets of per-line content hashes (not line numbers) so
the cache survives line reordering, blank-line insertion, and
whitespace-only edits — anything that doesn't change *what content is
present* in the section.

### Why a separate cache

The generic LLM cache at :mod:`work_buddy.llm.cache` keys on the prompt
text. A line-range segmenter response is ``{"groups": [[1, 2], [3]]}``
— integers tied to the *exact* original line numbering. When the user
edits the journal even slightly, the prompt text changes, the cache
misses (correct), but if it had hit it would have served line numbers
that mean different content now. Cache hit semantics + line-number
output are fundamentally incompatible.

This cache solves it by storing the segmentation as content sets:

  cached_groups = [
      [sha("- alpha"), sha("- beta")],   # cluster A by content
      [sha("- gamma")],                  # cluster B by content
  ]

On lookup, we hash the *current* lines and translate cached content
hashes back to current line numbers. If the current line set exactly
matches the cached line set (modulo whitespace + blank-line position),
it's a hit — and the line numbers we emit are correct for the *current*
input. If even one line was added, removed, or changed substantively,
we miss and fall through to a fresh LLM call.

### What this cache does NOT do

Partial-hit / partial-merge semantics. If the line sets differ at all,
the cache misses and the segmenter does a full re-run. A future
enhancement could reuse cached groups whose contents are still present
and only re-LLM the new lines, but that's a meaningful feature with
its own correctness considerations (which group does the new line
belong to?). Out of scope here.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.paths import resolve

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Default cache file location
# ---------------------------------------------------------------------------

_DEFAULT_CACHE_PATH = resolve("cache/segmentation")
_SEPARATOR_RE = re.compile(r"^-{3,}\s*$")
# Markdown code-fence delimiters — structural, not content. Mirrors the
# validator's treatment in :mod:`work_buddy.journal_backlog.segment`.
_CODE_FENCE_RE = re.compile(r"^(?:```|~~~)[a-zA-Z0-9_+-]*\s*$")


# ---------------------------------------------------------------------------
# Hashing — content-line normalization
# ---------------------------------------------------------------------------


def _normalize_line(line: str) -> str:
    """Reduce a line to its semantic content for hashing.

    Strips outer whitespace and collapses internal whitespace runs.
    Keeps case (so ``"alpha"`` and ``"ALPHA"`` are different — line
    text is user content; case usually carries meaning).
    """
    return re.sub(r"\s+", " ", line.strip())


def _is_content_line(line: str) -> bool:
    """A line is 'content' if it's non-blank, not a horizontal-rule separator,
    and not a markdown code-fence delimiter (``\\`\\`\\``` or ``~~~``)."""
    stripped = line.strip()
    if not stripped:
        return False
    if _SEPARATOR_RE.match(stripped):
        return False
    if _CODE_FENCE_RE.match(stripped):
        return False
    return True


def _hash_line(line: str) -> str:
    """Stable per-line SHA-256 hexdigest, applied to the normalized text."""
    return hashlib.sha256(_normalize_line(line).encode("utf-8")).hexdigest()


def _line_set_hash(content_hashes: list[str]) -> str:
    """Order-independent fingerprint of a set of per-line hashes.

    Sorting before joining makes the result invariant to original line
    ordering, so reordered-but-otherwise-identical inputs produce the
    same set hash.
    """
    sorted_hashes = sorted(content_hashes)
    return hashlib.sha256("|".join(sorted_hashes).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _read_cache(cache_path: Path) -> dict[str, dict]:
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_cache(cache_path: Path, cache: dict[str, dict]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8",
        )
        temp.replace(cache_path)
    except OSError as e:
        logger.error("Failed to write segmentation cache: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_cached_segmentation(
    *,
    original_lines: list[str],
    system_hash: str,
    cache_path: Path | None = None,
) -> list[list[int]] | None:
    """Look up a cached segmentation, translated to current line numbers.

    Args:
        original_lines: The cleaned text as a list of lines (1-based
            position in the input). May include blank lines and
            structural separators — those are excluded from the
            content set.
        system_hash: A short fingerprint of the segmenter system
            prompt. Editing the prompt changes the hash, scoping
            cache entries by prompt revision.
        cache_path: Override the cache file location (used by tests).

    Returns:
        On hit: a list of groups (each a list of 1-based line numbers
        in ``original_lines``) translated from the cached content
        hashes. On miss: ``None``.

    A hit requires the *content set* of ``original_lines`` to exactly
    match the cached entry's line set (modulo whitespace normalization
    and ignoring blank/separator lines). Any added, removed, or
    substantively-changed line yields a miss.
    """
    cache_path = cache_path or _DEFAULT_CACHE_PATH

    line_hashes_by_pos: dict[int, str] = {}
    line_positions: dict[str, list[int]] = {}
    for i, line in enumerate(original_lines, start=1):
        if not _is_content_line(line):
            continue
        h = _hash_line(line)
        line_hashes_by_pos[i] = h
        line_positions.setdefault(h, []).append(i)

    current_set_hash = _line_set_hash(list(line_hashes_by_pos.values()))
    key = f"{system_hash}:{current_set_hash}"

    cache = _read_cache(cache_path)
    entry = cache.get(key)
    if entry is None:
        return None

    expires_at = entry.get("expires_at", "")
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) < datetime.now():
                return None
        except ValueError:
            return None

    cached_groups: list[list[str]] = entry.get("groups_by_hash", []) or []

    # Translate content hashes back to the CURRENT line numbers.
    new_groups: list[list[int]] = []
    for cached_group in cached_groups:
        members: set[int] = set()
        for content_hash in cached_group:
            positions = line_positions.get(content_hash)
            if not positions:
                # A cached line is missing from the current input —
                # invariant violation given the line-set-hash key
                # already matched. Treat as miss to be safe.
                logger.warning(
                    "segmentation cache: stale entry for key %s "
                    "(content hash %s missing from current input)",
                    key, content_hash,
                )
                return None
            members.update(positions)
        new_groups.append(sorted(members))

    return new_groups


def put_segmentation(
    *,
    original_lines: list[str],
    system_hash: str,
    groups: list[list[int]],
    ttl_minutes: int = 60,
    cache_path: Path | None = None,
) -> None:
    """Store a segmentation result keyed by its content set.

    Args:
        original_lines: Same lines that were segmented.
        system_hash: Segmenter system-prompt fingerprint.
        groups: The line-number partition (1-based, into
            ``original_lines``).
        ttl_minutes: Entry lifetime. Default 60 min — long enough to
            help with tight edit/scan loops, short enough to cap
            staleness if anything goes wrong upstream.
        cache_path: Override cache file location (tests).

    The stored entry uses content hashes (not line numbers) so future
    lookups on the same content survive reordering and blank-line
    edits.
    """
    cache_path = cache_path or _DEFAULT_CACHE_PATH

    line_hashes_by_pos: dict[int, str] = {}
    for i, line in enumerate(original_lines, start=1):
        if not _is_content_line(line):
            continue
        line_hashes_by_pos[i] = _hash_line(line)

    line_set = list(line_hashes_by_pos.values())
    set_hash = _line_set_hash(line_set)
    key = f"{system_hash}:{set_hash}"

    # Translate line-number groups → content-hash groups. Skip group
    # entries that point at non-content lines (rare; would mean the
    # validator accepted a separator into a thread). Drop empty groups.
    groups_by_hash: list[list[str]] = []
    for group in groups:
        hashes = [
            line_hashes_by_pos[ln]
            for ln in group
            if ln in line_hashes_by_pos
        ]
        if hashes:
            groups_by_hash.append(hashes)

    if not groups_by_hash:
        # Nothing meaningful to cache — empty input or all-separator.
        return

    now = datetime.now()
    cache = _read_cache(cache_path)
    cache[key] = {
        "line_set": line_set,
        "groups_by_hash": groups_by_hash,
        "n_lines": len(line_set),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
    }
    _write_cache(cache_path, cache)


def prune(cache_path: Path | None = None) -> int:
    """Remove expired entries. Returns count removed."""
    cache_path = cache_path or _DEFAULT_CACHE_PATH
    cache = _read_cache(cache_path)
    now = datetime.now()
    to_remove = []
    for key, entry in cache.items():
        expires_at = entry.get("expires_at", "")
        if expires_at:
            try:
                if datetime.fromisoformat(expires_at) < now:
                    to_remove.append(key)
            except ValueError:
                to_remove.append(key)

    for key in to_remove:
        del cache[key]

    if to_remove:
        _write_cache(cache_path, cache)
        logger.info(
            "Pruned %d expired segmentation cache entries", len(to_remove),
        )
    return len(to_remove)
