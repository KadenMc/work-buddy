"""Triage-specific embedding + clustering orchestration.

Thin layer over ``work_buddy.ml.clustering`` — embeds TriageItems via
the embedding HTTP service, then delegates to the shared Louvain pipeline.

Runs in a **subprocess** (auto_run) since it imports networkx indirectly.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.ml.clustering import (
    cluster_items as _cluster_items_raw,
    compute_pairwise_similarity,
    cosine_similarity,
    position_proximity,
)
from work_buddy.triage.items import TriageCluster, TriageItem

logger = get_logger(__name__)


def embed_items(
    items: list[TriageItem],
    model: str | None = None,
    use_ir_model: bool = False,
) -> list[TriageItem]:
    """Populate embedding vectors for each item via the embedding service.

    Args:
        items: TriageItems (embedding field may be None).
        model: Embedding model key (default: service default, usually leaf-mt).
            Ignored when ``use_ir_model=True``.
        use_ir_model: If True, use the asymmetric document tower
            (leaf-ir, 768d) designed for long text.  Better for full
            page content.  If False, use the symmetric model (leaf-mt,
            1024d) which works well for short text like titles.
            **Never mix** — all items must use the same model.

    Returns:
        Same items list with embedding fields populated.
        Items whose embedding fails get a zero vector.
    """
    from work_buddy.embedding.client import embed, embed_for_ir

    texts = [item.text for item in items]

    if use_ir_model:
        vectors = embed_for_ir(texts, role="document")
        fallback_dim = 768
    else:
        vectors = embed(texts, model=model)
        fallback_dim = 1024

    if vectors is None:
        logger.warning("Embedding service unavailable — using zero vectors")
        vectors = [[0.0] * fallback_dim] * len(items)

    for item, vec in zip(items, vectors):
        item.embedding = vec

    return items


def _spatial_tags(item: TriageItem) -> list[str]:
    """Build categorical tags from Chrome tab spatial metadata.

    Maps Chrome group membership → the generic 'tag' signal slot.
    Jaccard({chrome_group:42}, {chrome_group:42}) = 1.0 (same group).
    Jaccard({chrome_group:42}, {chrome_group:99}) = 0.0 (different groups).
    Ungrouped tabs (group_id=-1 or None) get no tags → Jaccard = 0.0.
    """
    tags = []
    gid = item.metadata.get("group_id", -1)
    if gid not in (None, -1):
        tags.append(f"chrome_group:{gid}")
    return tags


def _tab_proximity(item_a: dict, item_b: dict, total: int) -> float:
    """Window-gated tab proximity for the generic 'proximity' signal slot.

    Returns 0.0 for cross-window pairs.  For same-window pairs, uses
    Gaussian decay over normalized index distance within that window.
    """
    win_a = item_a.get("window_id")
    win_b = item_b.get("window_id")
    if win_a is None or win_b is None or win_a != win_b:
        return 0.0
    idx_a = item_a.get("tab_index", 0)
    idx_b = item_b.get("tab_index", 0)
    window_size = item_a.get("window_size", total)
    return position_proximity(idx_a, idx_b, window_size, sigma=0.4)


def cluster_items(
    items: list[TriageItem],
    edge_threshold: float = 0.45,
    resolution: float = 1.2,
) -> list[TriageCluster]:
    """Embed items and cluster them by similarity with spatial signals.

    Uses three fused signals (80/10/10 weighting):
      - **embedding** (0.80): semantic similarity (dominant)
      - **tag** (0.10): Chrome group membership via Jaccard
      - **proximity** (0.10): window-gated tab index decay

    Args:
        items: TriageItems to cluster.
        edge_threshold: Minimum similarity for a graph edge. Default 0.45
            (lower than journal's 0.5 to accommodate heterogeneous content).
        resolution: Louvain resolution. Default 1.2 (lower than journal's
            1.5 to produce slightly larger clusters).

    Returns:
        List of TriageCluster objects (multi-item first, then singletons).
    """
    if not items:
        return []

    # Ensure embeddings exist
    needs_embedding = any(item.embedding is None for item in items)
    if needs_embedding:
        embed_items(items)

    embeddings = [item.embedding or [] for item in items]

    # Pre-compute per-window tab counts for proximity normalization
    window_counts = Counter(
        item.metadata.get("window_id") for item in items
        if item.metadata.get("window_id") is not None
    )

    # Convert to dicts for ml.clustering (expects {id, tags, summary}).
    #
    # Signal slot mapping for Chrome tabs:
    #   tag   → Chrome group membership (binary same-group signal)
    #   proximity → window-gated index decay (0 cross-window, Gaussian same-window)
    item_dicts = [
        {
            "id": item.id,
            "tags": _spatial_tags(item),
            "summary": item.label,
            # Extra fields consumed by _tab_proximity (transparent to ml.clustering)
            "window_id": item.metadata.get("window_id"),
            "tab_index": item.metadata.get("index", 0),
            "window_size": window_counts.get(item.metadata.get("window_id"), 1),
        }
        for item in items
    ]

    # Embedding-dominant with weak spatial signals:
    #   embedding = semantic similarity (dominant)
    #   tag       = Chrome group membership via Jaccard (rare but intentional)
    #   proximity = window-gated tab index decay (weak positional hint)
    weights = {"embedding": 0.80, "tag": 0.10, "proximity": 0.10}
    pairs = compute_pairwise_similarity(
        item_dicts, embeddings,
        weights=weights,
        proximity_fn=_tab_proximity,
    )

    raw_clusters = _cluster_items_raw(
        item_dicts, pairs,
        edge_threshold=edge_threshold,
        resolution=resolution,
    )

    # Convert raw cluster dicts to TriageCluster objects
    item_by_id = {item.id: item for item in items}
    result: list[TriageCluster] = []

    for rc in raw_clusters:
        cluster_items_list = [
            item_by_id[tid]
            for tid in rc["thread_ids"]
            if tid in item_by_id
        ]

        # Compute centroid (mean of member embeddings)
        centroid = _compute_centroid(cluster_items_list)

        # Auto-label from items
        label = _auto_label(cluster_items_list) or rc["label"]

        result.append(TriageCluster(
            cluster_id=rc["cluster_id"],
            items=cluster_items_list,
            label=label,
            cohesion=rc["internal_cohesion"],
            centroid=centroid,
            cross_cluster_edges=rc["cross_cluster_edges"],
        ))

    return result


def _compute_centroid(items: list[TriageItem]) -> list[float] | None:
    """Mean embedding vector across cluster members."""
    vecs = [item.embedding for item in items if item.embedding]
    if not vecs:
        return None
    dim = len(vecs[0])
    centroid = [0.0] * dim
    for v in vecs:
        for i, val in enumerate(v):
            centroid[i] += val
    n = len(vecs)
    return [c / n for c in centroid]


def _auto_label(items: list[TriageItem]) -> str:
    """Generate a cluster label from member items."""
    if not items:
        return ""

    # If all items share the same source domain, use it
    domains = set()
    for item in items:
        domain = item.metadata.get("domain")
        if domain:
            domains.add(domain)

    if len(domains) == 1:
        domain = domains.pop()
        if len(items) == 1:
            return items[0].label
        return f"{domain} ({len(items)} items)"

    # Fall back to first item's label
    if len(items) == 1:
        return items[0].label
    return items[0].label[:40] + f" (+{len(items) - 1} more)"


# ── auto_run entry point ────────────────────────────────────────


def cluster_items_from_raw(items_data: list[dict[str, Any]]) -> dict[str, Any]:
    """Auto_run entry point: JSON dicts in, JSON dict out.

    Args:
        items_data: List of TriageItem dicts (from chrome adapter or similar).

    Returns:
        Dict with 'clusters' (list of TriageCluster dicts) and metadata.
    """
    items = [TriageItem.from_dict(d) for d in items_data]
    logger.info("Clustering %d triage items", len(items))

    clusters = cluster_items(items)

    multi = [c for c in clusters if c.size > 1]
    singletons = [c for c in clusters if c.size == 1]

    logger.info(
        "Clustering complete: %d multi-item clusters, %d singletons",
        len(multi), len(singletons),
    )

    return {
        "success": True,
        "clusters": [c.to_dict() for c in multi],
        "singletons": [c.to_dict() for c in singletons],
        "item_count": len(items),
        "cluster_count": len(multi),
        "singleton_count": len(singletons),
    }
