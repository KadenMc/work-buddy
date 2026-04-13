"""Seriation — optimal linear ordering by similarity.

Given items with pairwise similarity scores, find a linear ordering such
that similar items are adjacent.  This is the 1D layout problem:
high-dimensional similarity relationships projected onto a single axis.

The core algorithm is **greedy nearest-neighbor**: start with the most
centrally connected item, then repeatedly pick the most-similar unvisited
item.  O(n²) — fine for n < 1000.

Two entry points:

- ``seriate(similarity_matrix, ids)`` — from a precomputed similarity dict
- ``seriate_by_embeddings(items, embeddings, ...)`` — from raw items +
  embedding vectors, fusing multiple signals before seriating

Both return an ordered list of IDs.  Optionally insert break indices
where similarity drops below a threshold.

Extracted from ``work_buddy.journal_backlog.clustering.linearize_threads``
and generalized to work with any similarity source — embeddings, tags,
or fused multi-signal scores.
"""

from __future__ import annotations

from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.ml.clustering import (
    compute_pairwise_similarity,
    cosine_similarity,
)

logger = get_logger(__name__)


def seriate(
    similarity: dict[tuple[str, str], float],
    ids: list[str],
    break_threshold: float | None = None,
) -> dict[str, Any]:
    """Seriate items into a linear order by similarity.

    Uses greedy nearest-neighbor: starts with the item that has the
    highest average similarity to all others (the "centroid"), then
    repeatedly picks the most-similar unvisited item.

    Args:
        similarity: Pairwise similarity scores ``{(id_a, id_b): score}``.
            Must contain both directions: ``(a, b)`` and ``(b, a)``.
        ids: Item identifiers (order doesn't matter).
        break_threshold: If set, identify break points where adjacent
            similarity drops below this value.

    Returns:
        Dict with:
        - ``order``: list of IDs in seriated order
        - ``breaks``: list of indices where breaks occur (empty if
          no threshold given)
        - ``adjacent_similarities``: list of similarity scores between
          consecutive items in the order
    """
    if not ids:
        return {"order": [], "breaks": [], "adjacent_similarities": []}
    if len(ids) == 1:
        return {"order": list(ids), "breaks": [], "adjacent_similarities": []}

    # Find starting item: highest average similarity to all others
    avg_sim: dict[str, float] = {}
    for tid in ids:
        total = sum(similarity.get((tid, other), 0.0) for other in ids if other != tid)
        avg_sim[tid] = total / (len(ids) - 1)

    start = max(ids, key=lambda x: avg_sim[x])

    # Greedy nearest-neighbor traversal
    ordered: list[str] = [start]
    remaining = set(ids) - {start}

    while remaining:
        current = ordered[-1]
        best = max(remaining, key=lambda x: similarity.get((current, x), 0.0))
        ordered.append(best)
        remaining.remove(best)

    # Compute adjacent similarities
    adj_sims: list[float] = []
    for i in range(1, len(ordered)):
        adj_sims.append(similarity.get((ordered[i - 1], ordered[i]), 0.0))

    # Identify breaks
    breaks: list[int] = []
    if break_threshold is not None:
        for i, sim in enumerate(adj_sims):
            if sim < break_threshold:
                breaks.append(i + 1)  # break BEFORE this index

    logger.debug(
        "Seriated %d items (start=%s, %d breaks)",
        len(ids), start, len(breaks),
    )

    return {
        "order": ordered,
        "breaks": breaks,
        "adjacent_similarities": [round(s, 4) for s in adj_sims],
    }


def seriate_by_embeddings(
    items: list[dict[str, Any]],
    embeddings: list[list[float]],
    weights: dict[str, float] | None = None,
    sigma: float = 0.4,
    break_threshold: float | None = None,
) -> dict[str, Any]:
    """Seriate items using multi-signal similarity (embeddings + tags + proximity).

    Convenience wrapper: computes pairwise similarity via
    ``ml.clustering.compute_pairwise_similarity``, then seriates.

    Args:
        items: List of item dicts (must have ``id``; optionally ``tags``).
        embeddings: Parallel list of embedding vectors.
        weights: Signal weights (default: ``{embedding: 0.5, tag: 0.3, proximity: 0.2}``).
        sigma: Positional decay for proximity signal.
        break_threshold: Optional similarity threshold for break insertion.

    Returns:
        Same as ``seriate()`` plus ``pairs`` (the raw pairwise scores).
    """
    pairs = compute_pairwise_similarity(
        items, embeddings, weights=weights, sigma=sigma,
    )

    # Build symmetric lookup
    sim_matrix: dict[tuple[str, str], float] = {}
    for p in pairs:
        sim_matrix[(p["id_a"], p["id_b"])] = p["fused"]
        sim_matrix[(p["id_b"], p["id_a"])] = p["fused"]
    for item in items:
        sim_matrix[(item["id"], item["id"])] = 1.0

    ids = [item["id"] for item in items]
    result = seriate(sim_matrix, ids, break_threshold=break_threshold)
    result["pairs"] = pairs
    return result


def seriate_by_cosine(
    ids: list[str],
    embeddings: list[list[float]],
    break_threshold: float | None = None,
) -> dict[str, Any]:
    """Seriate using only cosine similarity on embeddings.

    Simpler than ``seriate_by_embeddings`` — no tags or proximity,
    just raw embedding similarity.  Use when items don't have tag
    metadata (e.g., triage intent groups).

    Args:
        ids: Item identifiers (parallel to embeddings).
        embeddings: Embedding vectors.
        break_threshold: Optional similarity threshold for breaks.

    Returns:
        Same as ``seriate()``.
    """
    sim_matrix: dict[tuple[str, str], float] = {}
    for i in range(len(ids)):
        for j in range(len(ids)):
            if i == j:
                sim_matrix[(ids[i], ids[j])] = 1.0
            elif i < j:
                sim = cosine_similarity(embeddings[i], embeddings[j])
                sim_matrix[(ids[i], ids[j])] = sim
                sim_matrix[(ids[j], ids[i])] = sim

    return seriate(sim_matrix, ids, break_threshold=break_threshold)
