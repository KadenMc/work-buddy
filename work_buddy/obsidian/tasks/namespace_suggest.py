"""Namespace-tag lookup helpers for task inference.

Two capabilities live here:

- ``task_namespace_suggest(task_text, contract=None, project=None, limit=3)``
  Ranks existing namespace tags by semantic + lexical similarity to the
  given task text. Used by agents (and the triage scan) to *propose* tags
  for a task being created or edited.

- ``namespace_lookup(query, limit=5)``
  Narrower: given a single query string (usually a candidate namespace name
  the agent is about to mint), returns the closest existing namespaces.
  Used by the agent before introducing a brand-new namespace — "did you
  mean X?" — so near-duplicates don't proliferate.

The intelligence lives in the *calling agent*, not here. These capabilities
are lookups: they answer "what exists in the universe, and which are close
to this query?" using the shared embedding service
(``work_buddy.embedding.client.hybrid_search``), with a pure-Python token
overlap scorer as the fallback when the service is unavailable.
"""

from __future__ import annotations

import re
from typing import Any

from work_buddy.embedding import client as embedding_client
from work_buddy.logging_config import get_logger
from work_buddy.obsidian.tasks import store

logger = get_logger(__name__)

# Stopwords kept tiny — we want token overlap, not stemming.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "of", "for", "to", "with",
        "in", "on", "at", "by", "from", "is", "are", "was", "were", "be",
        "do", "does", "did", "have", "has", "had", "this", "that", "these",
        "those", "it", "its", "as", "if", "not", "no", "yes", "re", "via",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {
        tok
        for tok in _TOKEN_RE.findall((text or "").lower())
        if tok not in _STOPWORDS and len(tok) > 1
    }


# ── Candidate construction ──────────────────────────────────────


def _tag_to_candidate_texts(tag: str) -> list[str]:
    """Build the list of phrases describing a namespace tag.

    The hybrid-search endpoint is happiest with multiple short phrases per
    candidate — each segment alone, plus the full path with separators
    turned into spaces (so the embedding sees the words, not punctuation).
    """
    segments = [s for s in tag.split("/") if s]
    texts: list[str] = []
    # Full path with separators as spaces — lets the embedder see
    # "paper ecg classifier" rather than treat the path as opaque.
    joined = tag.replace("/", " ").replace("-", " ").replace("_", " ")
    texts.append(joined)
    # Each individual segment, hyphen-/underscore-split, so the embedder
    # matches "ecg" against "ecg-classifier".
    for seg in segments:
        clean = seg.replace("-", " ").replace("_", " ")
        if clean and clean != joined:
            texts.append(clean)
    return texts


def _build_candidates(universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn the namespace universe into hybrid_search candidate dicts."""
    return [
        {"name": row["tag"], "texts": _tag_to_candidate_texts(row["tag"])}
        for row in universe
        if row.get("tag")
    ]


# ── Token-overlap fallback (used when embedding service is down) ──


def _score_tag(tag: str, query_tokens: set[str]) -> float:
    """Token-overlap score between a tag and a query token set."""
    segments = [s for s in tag.split("/") if s]
    if not segments:
        return 0.0
    score = 0.0
    for depth, seg in enumerate(segments):
        seg_tokens = _tokens(seg.replace("-", " ").replace("_", " "))
        overlap = seg_tokens & query_tokens
        if not overlap:
            continue
        score += len(overlap) * (1.0 + 0.4 * depth)
    return score


def _fallback_rank(
    query_text: str,
    universe: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Rank candidates by token overlap. Used when embedding is unavailable."""
    qt = _tokens(query_text)
    if not qt:
        return []
    scored: list[dict[str, Any]] = []
    for row in universe:
        tag = row["tag"]
        s = _score_tag(tag, qt)
        if s > 0:
            scored.append({
                "tag": tag,
                "score": round(s, 3),
                "count": int(row.get("count", 0)),
                "recent_count": int(row.get("recent_count", 0)),
                "method": "tokens",
                "exists": True,
            })
    scored.sort(
        key=lambda d: (-d["score"], -d["count"], d["tag"].count("/"), d["tag"]),
    )
    return scored[:limit]


# ── Public capabilities ────────────────────────────────────────


def task_namespace_suggest(
    task_text: str,
    contract: str | None = None,
    project: str | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    """Rank existing namespace tags by relevance to a task text.

    Returns up to ``limit`` candidates drawn from the current namespace
    universe (the registered namespacey tags in the task-tag cache).
    The calling agent decides whether to apply them, add others, or
    coin a new namespace — this capability is a *lookup*, not the
    decision-maker.

    Args:
        task_text: Description of the task being created or edited.
        contract: Optional contract slug; appended to the query to boost
                  namespaces whose segments overlap the contract name.
        project: Optional project slug; same boost as ``contract``.
        limit: Max suggestions (default 3).

    Returns:
        ``{
            "suggestions": [
                {"tag": str, "score": float, "count": int,
                 "recent_count": int, "method": "hybrid"|"tokens",
                 "exists": True},
                ...
            ],
            "universe_size": int,
            "service_used": "hybrid" | "tokens" | "none",
        }``
    """
    try:
        universe = store.distinct_namespace_tags()
    except Exception as exc:
        logger.warning("task_namespace_suggest: universe unavailable: %s", exc)
        return {"suggestions": [], "universe_size": 0, "service_used": "none",
                "error": str(exc)}

    if not universe:
        return {"suggestions": [], "universe_size": 0, "service_used": "none"}

    # Enriched query: task text plus slug tokens for contract/project.
    parts = [task_text or ""]
    if contract:
        parts.append(contract.replace("-", " ").replace("_", " "))
    if project:
        parts.append(project.replace("-", " ").replace("_", " "))
    query = " ".join(p for p in parts if p).strip()

    if not query:
        return {"suggestions": [], "universe_size": len(universe), "service_used": "none"}

    # Try the shared embedding service first (standard pattern across the
    # repo — dashboard task search, command palette, MCP search, knowledge
    # index). Fall back to the pure-Python token scorer if it's not up.
    service_used = "none"
    suggestions: list[dict[str, Any]] = []

    if embedding_client.is_available():
        candidates = _build_candidates(universe)
        try:
            results = embedding_client.hybrid_search(query, candidates)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("task_namespace_suggest: hybrid_search failed: %s", exc)
            results = []
        if results:
            by_tag = {row["tag"]: row for row in universe}
            for r in results[: max(0, int(limit))]:
                tag = r.get("name")
                if not tag or tag not in by_tag:
                    continue
                row = by_tag[tag]
                suggestions.append({
                    "tag": tag,
                    "score": round(float(r.get("score", 0.0)), 3),
                    "count": int(row.get("count", 0)),
                    "recent_count": int(row.get("recent_count", 0)),
                    "method": "hybrid",
                    "exists": True,
                })
            service_used = "hybrid"

    if not suggestions:
        suggestions = _fallback_rank(query, universe, max(0, int(limit)))
        if suggestions:
            service_used = "tokens"

    return {
        "suggestions": suggestions,
        "universe_size": len(universe),
        "service_used": service_used,
    }


def namespace_lookup(
    query: str,
    limit: int = 5,
) -> dict[str, Any]:
    """Return the closest existing namespace tags to a single query.

    Designed for the "did you mean?" moment — when the agent is about to
    mint a brand-new namespace and wants to confirm it isn't a near-
    duplicate of one that already exists.

    Args:
        query: Candidate namespace path the agent is considering
               (e.g. ``"wellness/sleep"`` or a free-form label).
        limit: Max results (default 5).

    Returns:
        ``{
            "query": str,
            "exact_match": bool,
            "matches": [{"tag": ..., "score": ..., "count": ...,
                         "recent_count": ..., "method": ..., "exists": True}],
            "universe_size": int,
            "service_used": "hybrid"|"tokens"|"none",
        }``

        ``exact_match`` is true when the query (normalized — lowercased,
        leading ``#`` stripped) matches an existing namespace tag exactly.
        When true, the calling agent should not mint a new namespace — just
        use the existing one.
    """
    q = (query or "").strip().lstrip("#").strip()
    if not q:
        return {
            "query": "",
            "exact_match": False,
            "matches": [],
            "universe_size": 0,
            "service_used": "none",
        }

    try:
        universe = store.distinct_namespace_tags()
    except Exception as exc:
        logger.warning("namespace_lookup: universe unavailable: %s", exc)
        return {
            "query": q,
            "exact_match": False,
            "matches": [],
            "universe_size": 0,
            "service_used": "none",
            "error": str(exc),
        }

    by_tag = {row["tag"]: row for row in universe}
    exact = q.lower() in {t.lower() for t in by_tag}

    # Reuse the suggester — same ranking, with the tag-path itself as the
    # query text (no contract/project boost).
    result = task_namespace_suggest(
        task_text=q.replace("/", " ").replace("-", " ").replace("_", " "),
        limit=limit,
    )

    return {
        "query": q,
        "exact_match": exact,
        "matches": result.get("suggestions", []),
        "universe_size": result.get("universe_size", len(universe)),
        "service_used": result.get("service_used", "none"),
    }


# ── Workflow adapter ───────────────────────────────────────────


def enrich_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Workflow adapter: enrich a task-creation plan with tag-universe context.

    Called from the ``task-new`` workflow's ``enrich`` auto_run step. Takes
    the ``plan`` dict emitted by the prior reasoning step and returns it
    unchanged alongside three enrichments the next reasoning step needs:

    - ``suggestions`` — ranked existing namespaces relevant to ``task_text``
    - ``tag_status`` — for each tag in ``proposed_tags``, whether it exists
      in the universe; if not, the closest existing matches (so the
      confirmation step can surface "did you mean?" options)
    - ``universe_size`` — how many namespaces exist today

    Args:
        plan: A dict with at least ``task_text``; may include
              ``urgency``, ``project``, ``contract``, ``due_date``,
              ``summary``, ``proposed_tags`` (list[str]).

    Returns:
        ``{"plan": <original>, "suggestions": [...], "tag_status": {...},
           "universe_size": int}``.
    """
    if not isinstance(plan, dict):
        return {
            "plan": {},
            "suggestions": [],
            "tag_status": {},
            "universe_size": 0,
            "error": f"plan must be a dict, got {type(plan).__name__}",
        }

    task_text = str(plan.get("task_text") or "").strip()
    proposed = plan.get("proposed_tags") or []
    if not isinstance(proposed, list):
        proposed = []

    # Ranked suggestions from the existing universe.
    suggestions_result = task_namespace_suggest(
        task_text=task_text,
        contract=plan.get("contract"),
        project=plan.get("project"),
        limit=5,
    )

    # Per-proposed-tag existence + did-you-mean check.
    tag_status: dict[str, Any] = {}
    for raw in proposed:
        if not isinstance(raw, str):
            continue
        tag = raw.strip().lstrip("#").strip()
        if not tag:
            continue
        lookup = namespace_lookup(query=tag, limit=3)
        tag_status[tag] = {
            "exists": bool(lookup.get("exact_match")),
            "near_matches": lookup.get("matches", []),
        }

    return {
        "plan": plan,
        "suggestions": suggestions_result.get("suggestions", []),
        "tag_status": tag_status,
        "universe_size": suggestions_result.get("universe_size", 0),
        "service_used": suggestions_result.get("service_used", "none"),
    }
