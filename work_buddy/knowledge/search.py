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

# Interactive agent_docs query → fall back to the live index fast if the consolidated
# service is cold/contended past this budget.
_CONSOLIDATED_TIMEOUT_S = 15

# knowledge_scope → consolidated metadata filter. The consolidated knowledge partition
# indexes BOTH scopes (metadata["scope"]); "all" omits the filter (both).
_CONSOLIDATED_SCOPE_FILTERS: dict[str, dict[str, str]] = {
    "system": {"scope": "system"},
    "personal": {"scope": "personal"},
    "all": {},
}


def _search_via_consolidated(
    query: str,
    knowledge_scope: str,
    top_n: int,
    *,
    scope: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    severity: str | None = None,
) -> list[dict[str, Any]] | None:
    """Route the knowledge search to the resident consolidated index when activated.

    All filters are PUSHED DOWN so the index filter-then-ranks and returns a full ``top_n``
    drawn from within the qualifying set (no over-fetch, no post-filter recall loss — a tight
    filter whose members rank low globally still yields ``top_n`` when ``top_n`` match).

    Returns ``[{path, score}]`` — the SAME shape ``KnowledgeIndex.search`` returns, so the
    caller's tier hydration is unchanged — or ``None`` to signal "use the live in-process
    index": gate off, service unreachable, or empty.
    """
    from work_buddy.index.config import load_index_config

    if not load_index_config().consumer_enabled("agent_docs"):
        return None

    # Push every filter down (pre-filter == filter-then-rank, which the index already does
    # when handed the predicates). knowledge_scope + kind/category/severity → metadata
    # equality; agent_docs subtree-scope → doc_id prefix (store does ``doc_id LIKE scope%``).
    filters: dict[str, Any] = dict(_CONSOLIDATED_SCOPE_FILTERS.get(knowledge_scope, {}))
    if kind:
        filters["kind"] = kind
    if category:
        filters["category"] = category
    if severity:
        filters["severity"] = severity
    doc_id_prefix = f"knowledge:{scope.rstrip('/')}/" if scope else None

    try:
        from work_buddy.embedding.client import index_search

        hits = index_search(
            query,
            top_k=top_n,
            partitions=["knowledge"],
            filters=filters or None,
            scope=doc_id_prefix,
            timeout_s=_CONSOLIDATED_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — live fallback is the recovery
        logger.debug("consolidated knowledge search failed (%s); using live index.", exc)
        return None
    if not hits:
        return None
    scored: list[dict[str, Any]] = []
    for h in hits:
        meta = h.get("metadata") or {}
        path = meta.get("path") or str(h.get("doc_id", "")).split(":", 1)[-1]
        if path:
            scored.append({"path": path, "score": h.get("score", 0.0)})
    return scored or None


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
    max_depth: int | None = None,
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
        max_depth: Optional depth cap for placeholder recursion. ``None`` /
            ``-1`` selects the mode default; non-negative ints override.

    Returns:
        Result dict with mode, results, and metadata.
    """
    if depth not in ("index", "summary", "full"):
        return {"error": f"Invalid depth: {depth!r}. Must be 'index', 'summary', or 'full'."}

    # Mode 1: Exact path lookup
    if path is not None:
        return _lookup(
            path, depth, knowledge_scope,
            dev=dev, recursive=recursive, max_depth=max_depth,
        )

    # Mode 2: Browse subtree (no query) or filter-only (category/severity without query)
    if not query and (scope is not None or category or severity):
        return _browse(
            scope or "", kind, depth, knowledge_scope,
            category, severity, dev=dev, recursive=recursive, max_depth=max_depth,
        )

    # Mode 3/4: Search (optionally scoped)
    return _search(
        query, scope, kind, depth, top_n, knowledge_scope,
        category, severity, dev=dev, recursive=recursive, max_depth=max_depth,
    )


def search_many(
    queries: list[str],
    *,
    scope: str | None = None,
    kind: str | None = None,
    depth: str = "index",
    top_n: int = 8,
    knowledge_scope: str = "system",
    category: str | None = None,
    severity: str | None = None,
    dev: bool = False,
    query_embed_timeout_s: int | None = None,
) -> list[dict[str, Any]]:
    """Batched multi-query search — one result dict per query, in order.

    Functionally equivalent to ``[search(q, ...) for q in queries]`` for the
    search (query) mode, but the query-side dense embeddings for every query
    are batched into a single round-trip per model (see
    ``KnowledgeIndex.search_many``). Use this instead of a loop over
    ``search()`` whenever you issue several queries against the same store in
    one pass — the per-round-trip embedding overhead, not the in-process
    BM25/fusion, is what dominates on weak or contended hardware.

    Each returned dict has the same shape as ``search()``'s search-mode
    response: ``{"mode": "search", "query", "count", "results"}`` where each
    result carries ``path``, ``score``, and the unit's ``depth``-tiered fields.

    Graceful degradation matches ``search()``: if the embedding service is
    unavailable (or a batched embed exceeds ``query_embed_timeout_s``), the
    dense signals drop and results fall back to BM25. ``query_embed_timeout_s``
    bounds each batch so a contended service degrades fast rather than
    stalling the whole call.
    """
    if depth not in ("index", "summary", "full"):
        return [
            {"error": f"Invalid depth: {depth!r}. Must be 'index', 'summary', or 'full'."}
            for _ in queries
        ]
    if not queries:
        return []

    store = load_store(scope=knowledge_scope)

    # Filter candidates once — shared across all queries.
    candidates_units: dict[str, KnowledgeUnit] = {}
    for p, u in store.items():
        if scope and not p.startswith(scope.rstrip("/") + "/") and p != scope.rstrip("/"):
            continue
        if kind and u.kind != kind:
            continue
        candidates_units[p] = u
    candidates_units = _apply_vault_filters(candidates_units, category, severity)

    if not candidates_units:
        return [
            {"mode": "search", "query": q, "count": 0, "results": []}
            for q in queries
        ]

    full_store = load_store(scope="all") if depth == "full" else None

    from work_buddy.knowledge.index import ensure_index

    idx = ensure_index(knowledge_scope=knowledge_scope)
    scored_lists = idx.search_many(
        queries=queries,
        candidates=candidates_units,
        top_n=top_n,
        query_embed_timeout_s=query_embed_timeout_s,
    )

    out: list[dict[str, Any]] = []
    for query, scored in zip(queries, scored_lists):
        results = []
        for item in scored:
            unit = candidates_units.get(item["path"])
            if unit is None:
                continue
            results.append({
                "path": unit.path,
                "score": item["score"],
                **unit.tier(depth, store=full_store, dev=dev),
            })
        out.append({
            "mode": "search",
            "query": query,
            "count": len(results),
            "results": results,
        })
    return out


def _lookup(
    path: str,
    depth: str,
    knowledge_scope: str,
    dev: bool = False,
    recursive: str = "default",
    max_depth: int | None = None,
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
        "unit": unit.tier(
            depth, store=full_store, dev=dev,
            recursive_mode=recursive, max_depth=max_depth,
        ),
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
    max_depth: int | None = None,
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
        {"path": p, **u.tier(
            depth, store=full_store, dev=dev,
            recursive_mode=recursive, max_depth=max_depth,
        )}
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
    max_depth: int | None = None,
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
                **exact.tier(
                    depth, store=full_store, dev=dev,
                    recursive_mode=recursive, max_depth=max_depth,
                ),
            }],
        }

    # Pass full store for chain resolution
    full_store = load_store(scope="all") if depth == "full" else None

    # Route to the resident consolidated index when the agent_docs consumer is activated;
    # on any failure/empty, fall through to the live in-process knowledge index. All filters
    # are pushed down so the consolidated path filter-then-ranks (full top_n within filter);
    # it returns the same [{path, score}] shape, so the hydration below is unchanged.
    scored = _search_via_consolidated(
        query, knowledge_scope, top_n,
        scope=scope, kind=kind, category=category, severity=severity,
    )
    if scored is None:
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
                **unit.tier(
                    depth, store=full_store, dev=dev,
                    recursive_mode=recursive, max_depth=max_depth,
                ),
            })
            if len(results) >= top_n:
                break  # defense-in-depth cap (both backends already return ≤ top_n)

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
        full_store, dev=dev, recursive=recursive, max_depth=max_depth,
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
    max_depth: int | None = None,
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
            **unit.tier(
                depth, store=full_store, dev=dev,
                recursive_mode=recursive, max_depth=max_depth,
            ),
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
