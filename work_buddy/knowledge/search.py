"""Federated search over the unified knowledge store.

Supports four navigation modes:
  - ``path``: exact unit lookup (with children for navigation)
  - ``scope``: browse a subtree (all descendants)
  - ``query``: natural language search across everything
  - ``scope`` + ``query``: search within a subtree

Uses the same hybrid BM25+semantic scoring as the MCP registry,
with keyword fallback when the embedding service is unavailable.

The ``knowledge_scope`` parameter controls which stores are searched:
  - ``"system"`` — system docs only (default, backward-compatible)
  - ``"personal"`` — personal vault knowledge only
  - ``"all"`` — merged view of both stores
"""

from __future__ import annotations

from typing import Any

from work_buddy.knowledge.model import KnowledgeUnit, VaultUnit
from work_buddy.knowledge.store import load_store, get_unit, get_subtree
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def search(
    query: str = "",
    path: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    depth: str = "summary",
    top_n: int = 8,
    knowledge_scope: str = "system",
    category: str | None = None,
    severity: str | None = None,
    dev: bool = False,
    recursive: str = "default",
) -> dict[str, Any]:
    """Unified search and navigation over the knowledge store.

    Args:
        query: Natural language search. Empty for browse mode.
        path: Exact unit path for direct lookup.
        scope: Path prefix to filter results to a subtree.
        kind: Filter by unit kind (directions, system, capability, workflow, personal).
        depth: Content depth: "index", "summary", or "full".
        top_n: Maximum results for search mode.
        knowledge_scope: Which store(s) to search: "system", "personal", or "all".
        category: Filter VaultUnits by category (work_pattern, self_regulation, etc.).
        severity: Filter VaultUnits by severity (HIGH, MODERATE, LOW).
        recursive: Placeholder recursion mode passed through to ``tier()`` at
            ``depth="full"``. See ``agent_docs`` and ``KnowledgeUnit.tier``.

    Returns:
        Result dict with mode, results, and metadata.
    """
    if depth not in ("index", "summary", "full"):
        return {"error": f"Invalid depth: {depth!r}. Must be 'index', 'summary', or 'full'."}

    # Mode 1: Exact path lookup
    if path is not None:
        return _lookup(path, depth, knowledge_scope, dev=dev, recursive=recursive)

    # Mode 2: Browse subtree (no query) or filter-only (category/severity without query)
    if not query and (scope is not None or category or severity):
        return _browse(
            scope or "", kind, depth, knowledge_scope,
            category, severity, dev=dev, recursive=recursive,
        )

    # Mode 3/4: Search (optionally scoped)
    return _search(
        query, scope, kind, depth, top_n, knowledge_scope,
        category, severity, dev=dev, recursive=recursive,
    )


def _lookup(
    path: str,
    depth: str,
    knowledge_scope: str,
    dev: bool = False,
    recursive: str = "default",
) -> dict[str, Any]:
    """Direct lookup by exact path."""
    # For path lookup, search across all scopes if not found in requested scope
    store = load_store(scope=knowledge_scope)
    unit = store.get(path)

    # Fallback: if not found and scope is specific, try the other scope
    if unit is None and knowledge_scope != "all":
        unit = get_unit(path)

    if unit is None:
        return {"error": f"Unit not found: {path!r}"}

    # Pass the full store for context chain resolution
    full_store = load_store(scope="all") if depth == "full" else None

    return {
        "mode": "lookup",
        "path": path,
        "unit": unit.tier(depth, store=full_store, dev=dev, recursive_mode=recursive),
    }


def _browse(
    scope: str,
    kind: str | None,
    depth: str,
    knowledge_scope: str,
    category: str | None,
    severity: str | None,
    dev: bool = False,
    recursive: str = "default",
) -> dict[str, Any]:
    """Browse all units under a path prefix, or all units if scope is empty."""
    store = load_store(scope=knowledge_scope)

    if scope:
        # Ensure scope ends with / for prefix matching
        if not scope.endswith("/"):
            scope += "/"
        units = {p: u for p, u in store.items() if p.startswith(scope)}
    else:
        # No scope = browse all (used for filter-only queries)
        units = dict(store)

    if kind:
        units = {p: u for p, u in units.items() if u.kind == kind}

    # Apply VaultUnit-specific filters
    units = _apply_vault_filters(units, category, severity)

    # Pass full store for chain resolution at full depth
    full_store = load_store(scope="all") if depth == "full" else None

    results = [
        {"path": p, **u.tier(depth, store=full_store, dev=dev, recursive_mode=recursive)}
        for p, u in sorted(units.items())
    ]

    return {
        "mode": "browse",
        "scope": scope,
        "count": len(results),
        "results": results,
    }


def _search(
    query: str,
    scope: str | None,
    kind: str | None,
    depth: str,
    top_n: int,
    knowledge_scope: str,
    category: str | None,
    severity: str | None,
    dev: bool = False,
    recursive: str = "default",
) -> dict[str, Any]:
    """Hybrid search over the store using the persistent knowledge index.

    The index searches full content (metadata + summary + body) of every
    unit using BM25 + pre-built dense vectors with RRF fusion.
    """
    store = load_store(scope=knowledge_scope)

    # Filter candidates
    candidates_units: dict[str, KnowledgeUnit] = {}
    for p, u in store.items():
        if scope and not p.startswith(scope.rstrip("/") + "/") and p != scope.rstrip("/"):
            continue
        if kind and u.kind != kind:
            continue
        candidates_units[p] = u

    # Apply VaultUnit-specific filters
    candidates_units = _apply_vault_filters(candidates_units, category, severity)

    if not candidates_units:
        return {
            "mode": "search",
            "query": query,
            "count": 0,
            "results": [],
        }

    # Exact path match
    exact = candidates_units.get(query) or candidates_units.get(
        query.replace("-", "_").replace(" ", "_")
    )
    if exact is not None:
        full_store = load_store(scope="all") if depth == "full" else None
        return {
            "mode": "search",
            "query": query,
            "count": 1,
            "results": [{
                "path": exact.path,
                "score": 1.0,
                **exact.tier(depth, store=full_store, dev=dev, recursive_mode=recursive),
            }],
        }

    # Pass full store for chain resolution
    full_store = load_store(scope="all") if depth == "full" else None

    # Use the persistent knowledge index for hybrid BM25 + dense search
    from work_buddy.knowledge.index import ensure_index

    idx = ensure_index(knowledge_scope=knowledge_scope)
    scored = idx.search(
        query=query,
        candidates=candidates_units,
        top_n=top_n,
    )

    if scored:
        results = []
        for item in scored:
            unit = candidates_units.get(item["path"])
            if unit is None:
                continue
            results.append({
                "path": unit.path,
                "score": item["score"],
                **unit.tier(depth, store=full_store, dev=dev, recursive_mode=recursive),
            })

        return {
            "mode": "search",
            "query": query,
            "count": len(results),
            "results": results,
        }

    # Fallback: keyword search if index returned nothing
    # (shouldn't happen normally, but covers edge cases)
    candidates_texts: dict[str, list[str]] = {
        p: u.search_phrases() for p, u in candidates_units.items()
    }
    return _keyword_search(
        query, candidates_units, candidates_texts, depth, top_n,
        full_store, dev=dev, recursive=recursive,
    )


def _keyword_search(
    query: str,
    units: dict[str, KnowledgeUnit],
    candidates: dict[str, list[str]],
    depth: str,
    top_n: int,
    full_store: dict[str, KnowledgeUnit] | None = None,
    dev: bool = False,
    recursive: str = "default",
) -> dict[str, Any]:
    """Keyword fallback when embedding service is unavailable."""
    query_lower = query.lower()
    terms = query_lower.split()

    scored: list[tuple[str, float]] = []
    for path, phrases in candidates.items():
        text = " ".join(phrases).lower()
        hits = sum(1 for t in terms if t in text)
        if hits > 0:
            scored.append((path, hits / len(terms)))

    scored.sort(key=lambda x: x[1], reverse=True)

    results = []
    for path, score in scored[:top_n]:
        unit = units[path]
        results.append({
            "path": path,
            "score": round(score, 4),
            **unit.tier(depth, store=full_store, dev=dev, recursive_mode=recursive),
        })

    return {
        "mode": "search",
        "query": query,
        "count": len(results),
        "results": results,
    }


def _apply_vault_filters(
    units: dict[str, KnowledgeUnit],
    category: str | None,
    severity: str | None,
) -> dict[str, KnowledgeUnit]:
    """Filter units by VaultUnit-specific fields. Non-VaultUnits pass through."""
    if not category and not severity:
        return units

    filtered: dict[str, KnowledgeUnit] = {}
    for p, u in units.items():
        if isinstance(u, VaultUnit):
            if category and u.category != category:
                continue
            if severity and u.severity != severity:
                continue
        filtered[p] = u

    return filtered


# ---------------------------------------------------------------------------
# Outer-layer RRF: fuse multiple ``search()`` result lists
# ---------------------------------------------------------------------------
#
# The hybrid index already combines BM25 + dense embeddings via
# ``KnowledgeIndex._rrf_fuse`` (numpy-array shape, internal). ``rrf_combine``
# is the *outer* layer for callers who run several independent ``search()``
# calls — e.g. one query from file paths, another from module docstrings —
# and want the same equal-voice rank fusion across those. Concatenating the
# queries into one string would dilute short structural signals under longer
# prosier ones; running them as separate searches and fusing the results
# preserves each signal's discriminative power.
#
# The default ``k=60`` matches ``_RRF_K`` in
# ``work_buddy/knowledge/index.py`` (Cormack/Clarke/Buettcher 2009). Keeping
# the constant in lockstep means inner and outer fusion behave identically;
# don't override unless you have a specific reason.

def rrf_combine(
    rankings: list[list[dict[str, Any]]],
    k: int = 60,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion across multiple ``search()`` result lists.

    Each ``ranking`` is the ``results`` list returned by ``search()`` — a
    sequence of ``{path, score, ...}`` dicts ordered by score descending.
    Each document's RRF score is the sum of ``1 / (k + rank)`` across every
    ranking it appears in (1-based ranks). Documents present in only some
    rankings still contribute via the rankings where they appear.

    Returns a single ranked list ordered by RRF score descending. Each
    output dict carries the metadata from its first occurrence (``path``,
    ``name``, ``description``, etc.) plus a new ``rrf_score`` field. Inputs
    are not mutated; output dicts are shallow copies.

    Empty input (``[]`` or any combination of empty rankings) returns
    ``[]``. Single-ranking input is idempotent — same paths in the same
    order, with ``rrf_score = 1/(k+1), 1/(k+2), …`` populated.

    Args:
        rankings: List of ranked result lists. Each inner list must already
            be sorted by relevance (higher first).
        k: RRF constant (Cormack/Clarke/Buettcher 2009). Default ``60``
            matches ``_RRF_K`` in ``work_buddy.knowledge.index`` so inner
            and outer fusion behave identically. Higher values flatten
            rank differences; lower values sharpen them.

    Returns:
        Fused result list with ``rrf_score`` populated on each entry.
    """
    rrf_scores: dict[str, float] = {}
    first_seen: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        for rank_idx, hit in enumerate(ranking):
            path = hit.get("path")
            if not path:
                continue
            rrf_scores[path] = rrf_scores.get(path, 0.0) + 1.0 / (k + rank_idx + 1)
            first_seen.setdefault(path, hit)

    out: list[dict[str, Any]] = []
    for path, score in sorted(rrf_scores.items(), key=lambda t: -t[1]):
        merged = dict(first_seen[path])
        merged["rrf_score"] = score
        out.append(merged)
    return out
