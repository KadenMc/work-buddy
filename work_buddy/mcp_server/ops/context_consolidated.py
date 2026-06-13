"""Route `context_search` to the consolidated index when its consumer flag is on.

The consumer-layer bridge from the IR-engine `context_search` op (`context_ops.py`) to
the consolidated index. Mirrors the knowledge re-point (`knowledge/search.py::
_search_via_consolidated`): gate on `index.consumers.context_search`, push the request
down to the matching partition, adapt hits back into the IR result-dict shape so
`result_format.format_results` renders identically — and on ANY miss return ``None`` so
the caller falls back to the live IR engine.

Deliberately scoped to the unambiguous subset: a SINGLE, named ``source`` (so the
partition — hence the result's ``source`` tag and its preserved metadata — is known) with
NO ``scope`` narrowing (the IR and consolidated doc-id schemes differ, so a doc-id-prefix
scope can't be mapped 1:1 yet) and a hybrid-mappable ``method``. All-source federation,
scope-narrowed queries, and the no-embedding ``substring`` mode stay on the IR engine.
Carries no top-level domain imports, so importing it (e.g. by ``load_builtin_ops``) is a
cheap no-op.
"""
from __future__ import annotations

from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# index.consumers.<name> gate for this consumer.
CONSUMER = "context_search"

# context_search method tokens → the consolidated index's Query.method. ``substring`` is an
# exact, no-embedding mode the consolidated index has no equivalent for → it stays on IR.
_METHOD_MAP = {"keyword": "lexical", "semantic": "dense"}

_TIMEOUT_S = 30


def adapt_consolidated_hit(hit: dict, source: str) -> dict:
    """Consolidated `/index/search` hit → IR result-dict shape for ``format_results``.

    The consolidated `signals` dict carries ``fused`` (== score), ``lexical`` (bm25), and
    one entry per dense projection. Map lexical → ``bm25_score``; a lone dense projection →
    ``dense_score``; multiple (e.g. task_note's line/body) → ``projection_scores`` so the
    per-projection breakdown still renders.
    """
    sig = hit.get("signals") or {}
    proj_scores = {k: v for k, v in sig.items() if k not in ("fused", "lexical")}
    if len(proj_scores) == 1:
        dense_score: float | None = next(iter(proj_scores.values()))
        projection_scores: dict[str, float] | None = None
    else:
        dense_score = None
        projection_scores = proj_scores or None
    return {
        "doc_id": hit.get("doc_id", ""),
        "score": hit.get("score", 0.0),
        "source": source,
        "display_text": hit.get("display_text", ""),
        "metadata": hit.get("metadata") or {},
        "bm25_score": sig.get("lexical"),
        "dense_score": dense_score,
        "projection_scores": projection_scores,
    }


def search_context_via_consolidated(
    query: str,
    *,
    top_k: int = 10,
    source: str | None = None,
    scope: str | None = None,
    method: str = "keyword,semantic",
    recency: bool | None = None,
) -> list[dict[str, Any]] | None:
    """Try the consolidated index; return IR-shaped results, or ``None`` to use live IR.

    ``None`` (→ caller falls back to the IR engine) on any of: gate off; no ``source`` or
    a ``scope`` (out of the supported subset); ``substring``/unknown method; the embedding
    service failing; or zero hits.
    """
    try:
        from work_buddy.index.config import load_index_config

        if not load_index_config().consumer_enabled(CONSUMER):
            return None
    except Exception:  # noqa: BLE001 — never let config errors break search
        return None

    # Supported subset only: a single named source, no scope narrowing.
    if not source or scope:
        return None

    parts = [m.strip().lower() for m in (method or "").split(",") if m.strip()]
    if not parts or any(m not in _METHOD_MAP for m in parts):
        return None  # substring / unknown → IR engine
    cmethod = "hybrid" if len(parts) >= 2 else _METHOD_MAP[parts[0]]

    try:
        from work_buddy.embedding.client import index_search

        hits = index_search(
            query,
            top_k=top_k,
            method=cmethod,
            partitions=[source],
            recency=bool(recency),
            timeout_s=_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — IR fallback is the recovery
        logger.debug("consolidated context_search failed (%s); using IR engine.", exc)
        return None

    if not hits:
        return None
    return [adapt_consolidated_hit(h, source) for h in hits]
