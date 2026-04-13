"""Omnisearch lexical search wrapper.

Omnisearch is a separate plugin by scambier (NOT part of Smart Connections
ecosystem) that provides BM25/MiniSearch-based full-text search via a
public API registered on globalThis.omnisearch.
"""

from pathlib import Path
from typing import Any

from work_buddy.obsidian import bridge

_JS_DIR = Path(__file__).parent / "_js"


def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """BM25 lexical search over the vault via Omnisearch.

    Returns keyword-matched results with scores, excerpts, and match positions.
    Complements semantic_search — good for exact terms, file names, tags.

    Args:
        query: Search query (supports quotes, exclusions, file-type filters).
        limit: Maximum results (default 20).

    Returns:
        List of dicts with 'path', 'score', 'excerpt', 'matches', 'foundWords'.
    """
    bridge.require_available()
    js = (_JS_DIR / "omnisearch.js").read_text(encoding="utf-8")
    escaped = query.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    js = js.replace("__TEXT__", escaped).replace("__LIMIT__", str(limit))
    result = bridge.eval_js(js, timeout=15)
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"Omnisearch error: {result['error']}")
    return result["results"]
