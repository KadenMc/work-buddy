"""Disk persistence for the knowledge search index.

The knowledge index stores two dense signals:

- **Content vectors** (768-d, asymmetric ``leaf-ir`` document encoder) — one
  vector per unit. Keyed on unit path; change-detected by hashing the unit's
  ``content_text`` (name + description + tags + summary + full body, EXCLUDING
  aliases, matching ``IndexDoc.content_text``).

- **Alias vectors** (1024-d, symmetric ``leaf-mt``) — one vector per
  ``(path, alias_text)`` row. Keyed on the pair; change-detected by the pair
  itself (alias text IS the hash input). Stored flat; the caller is
  responsible for computing per-doc slices at build time.

## Why not a database

Unlike the IR conversation index (which has tens of thousands of docs,
filtering, source-scoping, metadata queries) this cache is tiny (~220 units,
~700 aliases) and accessed in one batch at startup. Two compressed ``.npz``
files + a tiny meta header is enough — no SQLite complexity.

## Invalidation

The cache includes a ``model_key`` string and a ``CACHE_VERSION`` integer.
Either mismatch causes the whole cache to be treated as empty — safer than
trying to migrate. Bump ``CACHE_VERSION`` when the hashing inputs or
normalization scheme changes.

## Concurrency

Writes use a temp-file-then-rename pattern so a crash mid-write can't
corrupt an existing cache. Reads are idempotent.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.paths import resolve

logger = get_logger(__name__)


# Bump this when the cache format or hashing inputs change — old caches will
# be discarded rather than migrated.
CACHE_VERSION = 1

# 16 hex chars = 8 bytes of SHA-256 prefix. Plenty of collision margin for a
# corpus of a few hundred units.
_HASH_LEN = 16


def _content_cache_path() -> Path:
    return resolve("cache/knowledge-content")


def _alias_cache_path() -> Path:
    return resolve("cache/knowledge-aliases")


def content_hash(text: str) -> str:
    """16-char SHA-256 prefix of the given text. Deterministic, fast."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LEN]


# ---------------------------------------------------------------------------
# Content cache — keyed on unit path
# ---------------------------------------------------------------------------

def load_content_cache(model_key: str) -> dict[str, tuple[str, "np.ndarray"]]:
    """Load content vector cache. Returns ``{path: (hash, vector)}``.

    Returns an empty dict if the cache is missing, corrupted, or tagged with
    a different model_key / version. In all error paths, the caller can
    treat the result as "cold cache, embed everything".
    """
    import numpy as np

    path = _content_cache_path()
    if not path.exists():
        return {}

    try:
        data = np.load(path, allow_pickle=True)
        cache_model = str(data["model_key"]) if "model_key" in data else ""
        cache_version = int(data["version"]) if "version" in data else 0
        if cache_model != model_key or cache_version != CACHE_VERSION:
            logger.info(
                "Knowledge content cache header mismatch "
                "(model=%r vs %r, version=%d vs %d). Treating as empty.",
                cache_model, model_key, cache_version, CACHE_VERSION,
            )
            return {}

        paths = data["paths"].tolist()
        hashes = data["hashes"].tolist()
        vectors = data["vectors"].astype(np.float32)  # upcast from float16
        if not (len(paths) == len(hashes) == vectors.shape[0]):
            logger.warning(
                "Knowledge content cache shape mismatch (paths=%d hashes=%d "
                "vectors=%d). Treating as empty.",
                len(paths), len(hashes), vectors.shape[0],
            )
            return {}

        return {p: (h, vectors[i]) for i, (p, h) in enumerate(zip(paths, hashes))}

    except Exception as e:
        logger.warning(
            "Failed to load knowledge content cache (%s). Treating as empty.", e,
        )
        return {}


def save_content_cache(
    cache: dict[str, tuple[str, "np.ndarray"]],
    model_key: str,
) -> None:
    """Persist ``{path: (hash, vector)}``. Atomic via temp-file rename.

    Empty cache is still written — recording an explicit empty snapshot is
    useful so we don't re-embed if the store is legitimately empty.
    """
    import numpy as np

    path = _content_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    paths_arr = np.array(list(cache.keys()), dtype=object)
    hashes_arr = np.array([cache[p][0] for p in cache], dtype=object)
    if cache:
        vectors_arr = np.stack([cache[p][1] for p in cache]).astype(np.float16)
    else:
        # Empty 0-row array, but shape out the dim so load doesn't barf
        vectors_arr = np.zeros((0, 1), dtype=np.float16)

    # savez_compressed auto-appends '.npz' when the path doesn't already
    # end with it. Giving it a tmp name that ends in '.npz' (e.g.
    # 'content.tmp.npz') keeps the on-disk name predictable so the rename
    # targets the right file.
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(
        tmp,
        paths=paths_arr,
        hashes=hashes_arr,
        vectors=vectors_arr,
        model_key=np.array(model_key),
        version=np.array(CACHE_VERSION),
    )
    tmp.replace(path)
    logger.debug(
        "Saved knowledge content cache: %d units, %.2f MB",
        len(cache), path.stat().st_size / 1024 / 1024,
    )


# ---------------------------------------------------------------------------
# Alias cache — keyed on (path, alias_text)
# ---------------------------------------------------------------------------

def load_alias_cache(model_key: str) -> dict[tuple[str, str], "np.ndarray"]:
    """Load alias vector cache. Returns ``{(path, alias_text): vector}``.

    Alias text is both the hash input AND half the cache key — if the alias
    is edited (even preserving the path), the new (path, new_alias_text) key
    simply misses and we embed. Old keys pointing at deleted aliases fall
    out naturally when we rewrite the cache.
    """
    import numpy as np

    path = _alias_cache_path()
    if not path.exists():
        return {}

    try:
        data = np.load(path, allow_pickle=True)
        cache_model = str(data["model_key"]) if "model_key" in data else ""
        cache_version = int(data["version"]) if "version" in data else 0
        if cache_model != model_key or cache_version != CACHE_VERSION:
            logger.info(
                "Knowledge alias cache header mismatch "
                "(model=%r vs %r, version=%d vs %d). Treating as empty.",
                cache_model, model_key, cache_version, CACHE_VERSION,
            )
            return {}

        paths = data["paths"].tolist()
        texts = data["alias_texts"].tolist()
        vectors = data["vectors"].astype(np.float32)
        if not (len(paths) == len(texts) == vectors.shape[0]):
            logger.warning(
                "Knowledge alias cache shape mismatch. Treating as empty.",
            )
            return {}

        return {(p, t): vectors[i] for i, (p, t) in enumerate(zip(paths, texts))}

    except Exception as e:
        logger.warning(
            "Failed to load knowledge alias cache (%s). Treating as empty.", e,
        )
        return {}


def save_alias_cache(
    cache: dict[tuple[str, str], "np.ndarray"],
    model_key: str,
) -> None:
    """Persist ``{(path, alias_text): vector}``. Atomic via temp-file rename."""
    import numpy as np

    path = _alias_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    keys = list(cache.keys())
    paths_arr = np.array([p for p, _ in keys], dtype=object)
    texts_arr = np.array([t for _, t in keys], dtype=object)
    if cache:
        vectors_arr = np.stack([cache[k] for k in keys]).astype(np.float16)
    else:
        vectors_arr = np.zeros((0, 1), dtype=np.float16)

    # savez_compressed auto-appends '.npz' when the path doesn't already
    # end with it. Giving it a tmp name that ends in '.npz' (e.g.
    # 'content.tmp.npz') keeps the on-disk name predictable so the rename
    # targets the right file.
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(
        tmp,
        paths=paths_arr,
        alias_texts=texts_arr,
        vectors=vectors_arr,
        model_key=np.array(model_key),
        version=np.array(CACHE_VERSION),
    )
    tmp.replace(path)
    logger.debug(
        "Saved knowledge alias cache: %d aliases, %.2f MB",
        len(cache), path.stat().st_size / 1024 / 1024,
    )


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def clear_caches() -> dict[str, Any]:
    """Delete both cache files. Used by `rebuild(force=True)`."""
    removed = {"content": False, "aliases": False}
    for name, p in (("content", _content_cache_path()), ("aliases", _alias_cache_path())):
        if p.exists():
            try:
                p.unlink()
                removed[name] = True
            except OSError as e:
                logger.warning("Failed to delete %s cache at %s: %s", name, p, e)
    return removed


def cache_status() -> dict[str, Any]:
    """Return on-disk cache stats for the status endpoint."""
    result: dict[str, Any] = {}
    for name, p in (("content", _content_cache_path()), ("aliases", _alias_cache_path())):
        if p.exists():
            result[name] = {
                "path": str(p),
                "size_mb": round(p.stat().st_size / 1024 / 1024, 2),
            }
        else:
            result[name] = {"path": str(p), "size_mb": 0.0, "missing": True}
    return result
