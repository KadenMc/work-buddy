"""Search over MCP gateway capabilities via the shared search service.

The MCP server subprocess does NOT import numpy, rank_bm25, or
sentence-transformers. All scoring is delegated to the shared
search & embedding service (localhost:5124). If the service is
offline, falls back to simple substring matching.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

from work_buddy.paths import resolve

_SEARCH_LOG = resolve("logs/search-debug")


def _get_search_log() -> Path:
    _SEARCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    return _SEARCH_LOG


def _log_to_file(path: Path, msg: str) -> None:
    import time
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        f.flush()


def _get_search_texts(entry: Any) -> list[str]:
    """Extract search phrases from a registry entry."""
    from work_buddy.mcp_server.registry import Capability

    name = entry.name
    desc = entry.description
    phrases = [
        name.replace("-", " ").replace("_", " "),
        desc,
    ]
    if isinstance(entry, Capability) and entry.search_aliases:
        phrases.extend(entry.search_aliases)
    phrases.append(f"{name} {desc}")
    return phrases


def hybrid_search(
    query: str,
    entries: dict[str, Any],
    top_n: int = 10,
    bm25_weight: float = 0.4,
    embed_weight: float = 0.6,
) -> list[dict[str, Any]]:
    """Search via the shared service. Falls back to substring matching."""
    import time
    _lf = _get_search_log()
    _log_to_file(_lf, f"hybrid_search called: {query!r}")

    # Build candidates
    candidates = []
    for name, entry in entries.items():
        candidates.append({"name": name, "texts": _get_search_texts(entry)})

    # Try the shared service
    from work_buddy.embedding.client import hybrid_search as service_search, is_available

    t = time.time()
    if is_available():
        _log_to_file(_lf, "Service available, delegating...")
        results = service_search(query, candidates, bm25_weight, embed_weight)
        _log_to_file(_lf, f"Service returned {len(results)} results in {time.time()-t:.2f}s")
        return results[:top_n]

    # Fallback: simple substring matching (no numpy needed)
    _log_to_file(_lf, "Service offline, falling back to substring matching")
    query_lower = query.lower()
    matched = []
    for cand in candidates:
        for text in cand["texts"]:
            if query_lower in text.lower():
                matched.append({"name": cand["name"], "score": 1.0})
                break
    _log_to_file(_lf, f"Substring fallback: {len(matched)} results")
    return matched[:top_n]
