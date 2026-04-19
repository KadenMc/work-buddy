"""MCP-facing query functions for the unified knowledge system.

Three query surfaces:

* ``knowledge`` — unified search across system docs + personal knowledge
* ``knowledge_personal`` — personal vault knowledge only
* ``agent_docs`` — system documentation only (canonical name)

Plus the original ``agent_docs`` which is unchanged for backward compat.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Unified knowledge query (both stores)
# ---------------------------------------------------------------------------

def knowledge(
    *,
    query: str = "",
    path: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    severity: str | None = None,
    depth: str = "summary",
    top_n: int = 8,
    dev: bool = False,
) -> dict[str, Any]:
    """Search across both system docs and personal knowledge.

    Same modes as agent_docs (path lookup, scope browse, search),
    but returns results from both stores tagged with their source scope.

    Args:
        query: Natural language search.
        path: Exact unit path for direct lookup.
        scope: Path prefix to filter to a subtree.
        kind: Filter by unit kind.
        category: Filter personal units by category (work_pattern, etc.).
        severity: Filter personal units by severity (HIGH, MODERATE, LOW).
        depth: Content depth: "index", "summary", or "full".
        top_n: Max results for search mode.
        dev: Include dev_notes in full-depth results.
    """
    from work_buddy.knowledge.search import search

    if not query and path is None and scope is None:
        return _full_index(kind, depth, knowledge_scope="all", dev=dev)

    return search(
        query=query,
        path=path,
        scope=scope,
        kind=kind,
        depth=depth,
        top_n=top_n,
        knowledge_scope="all",
        category=category,
        severity=severity,
        dev=dev,
    )


def knowledge_personal(
    *,
    query: str = "",
    path: str | None = None,
    scope: str | None = None,
    category: str | None = None,
    severity: str | None = None,
    depth: str = "summary",
    top_n: int = 8,
    dev: bool = False,
) -> dict[str, Any]:
    """Search personal knowledge from the Obsidian vault only.

    Args:
        query: Natural language search.
        path: Exact unit path for direct lookup.
        scope: Path prefix to filter (e.g., "personal/metacognition/").
        category: Filter by category (work_pattern, self_regulation, etc.).
        severity: Filter by severity (HIGH, MODERATE, LOW).
        depth: Content depth: "index", "summary", or "full".
        top_n: Max results.
        dev: Include dev_notes in full-depth results.
    """
    from work_buddy.knowledge.search import search

    if not query and path is None and scope is None and not category and not severity:
        return _full_index(None, depth, knowledge_scope="personal", dev=dev)

    return search(
        query=query or "",
        path=path,
        scope=scope,
        kind="personal",
        depth=depth,
        top_n=top_n,
        knowledge_scope="personal",
        category=category,
        severity=severity,
        dev=dev,
    )


# ---------------------------------------------------------------------------
# Original agent_docs — unchanged for backward compatibility
# ---------------------------------------------------------------------------

def agent_docs(
    *,
    query: str = "",
    path: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    depth: str = "summary",
    top_n: int = 8,
    dev: bool = False,
) -> dict[str, Any]:
    """Unified search and navigation over all agent documentation.

    Modes:
      - path="journal/running-notes" → exact lookup (with children for nav)
      - scope="journal/" → browse all descendants
      - query="running notes backlog" → federated search across everything
      - scope="tasks/" + query="triage" → scoped search
      - empty query + no scope + no path → full Tier 1 index

    Args:
        query: Natural language search.
        path: Exact unit path for direct lookup.
        scope: Path prefix to filter to a subtree.
        kind: Filter: "directions", "system", "capability", "workflow".
        depth: Content depth: "index" (navigation), "summary" (default), "full".
        top_n: Max results for search mode.
        dev: Include dev_notes in full-depth results.
    """
    from work_buddy.knowledge.search import search

    # Empty everything = return full index
    if not query and path is None and scope is None:
        return _full_index(kind, depth, dev=dev)

    return search(
        query=query,
        path=path,
        scope=scope,
        kind=kind,
        depth=depth,
        top_n=top_n,
        dev=dev,
    )


def agent_docs_rebuild(*, force: bool = False) -> dict[str, Any]:
    """Reload the knowledge store from disk and rebuild the search index.

    Use after editing store JSON files or after registry changes
    that should be reflected in the unified index.
    """
    from work_buddy.knowledge.store import load_store, invalidate_store

    invalidate_store()  # also invalidates the search index
    store = load_store(force=True)

    kinds: dict[str, int] = {}
    for unit in store.values():
        kinds[unit.kind] = kinds.get(unit.kind, 0) + 1

    # Eagerly rebuild the search index with full embeddings
    from work_buddy.knowledge.index import rebuild_index

    index_stats = rebuild_index(knowledge_scope="all")

    return {
        "status": "ok",
        "total_units": len(store),
        "by_kind": kinds,
        "index": index_stats,
    }


def knowledge_index_rebuild(force: bool = False) -> dict[str, Any]:
    """Force rebuild the knowledge search index.

    Reloads both stores from disk and rebuilds BM25 + dense vector indices
    over all knowledge units. Uses the persistent on-disk cache by default —
    unchanged units keep their cached vectors and only new/changed units
    re-embed. Typical warm rebuild completes in <1s.

    Args:
        force: If True, wipe the dense-vector cache first and re-embed every
            unit. Use when the cache seems corrupted, after a model change
            that didn't bump the cache version, or to measure cold rebuild
            performance. Normal rebuilds should leave this False.
    """
    from work_buddy.knowledge.index import rebuild_index

    return rebuild_index(knowledge_scope="all", force=force)


def knowledge_index_status() -> dict[str, Any]:
    """Return the current knowledge index status.

    Shows whether the index is built, unit count, and whether
    dense vectors are available plus on-disk cache file sizes.
    """
    from work_buddy.knowledge.index import get_index
    from work_buddy.knowledge.persistence import cache_status

    result = get_index().status()
    result["cache"] = cache_status()
    return result


def _full_index(
    kind: str | None,
    depth: str,
    knowledge_scope: str = "system",
    dev: bool = False,
) -> dict[str, Any]:
    """Return the complete Tier 1 index for browsing."""
    from work_buddy.knowledge.store import load_store

    store = load_store(scope=knowledge_scope)
    units = store.values()

    if kind:
        units = [u for u in units if u.kind == kind]

    # For full index, default to index depth for compactness
    effective_depth = depth if depth != "summary" else "index"

    results = [
        {"path": u.path, **u.tier(effective_depth, dev=dev)}
        for u in sorted(units, key=lambda u: u.path)
    ]

    return {
        "mode": "index",
        "count": len(results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Legacy compatibility — keep old names working during migration
# ---------------------------------------------------------------------------

def docs_query(
    *,
    query: str = "",
    category: str | None = None,
    depth: str = "summary",
    top_n: int = 5,
) -> dict[str, Any]:
    """Legacy wrapper — delegates to agent_docs."""
    return agent_docs(query=query, kind=category, depth=depth, top_n=top_n)


def docs_get(*, name: str, depth: str = "full") -> dict[str, Any]:
    """Legacy wrapper — delegates to agent_docs path lookup."""
    return agent_docs(path=name, depth=depth)


def docs_index_build(*, force: bool = False) -> dict[str, Any]:
    """Legacy wrapper — delegates to agent_docs_rebuild."""
    return agent_docs_rebuild(force=force)
