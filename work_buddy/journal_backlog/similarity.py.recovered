"""Multi-signal similarity analysis for segmented journal threads.

Orchestrates embedding (via Smart Connections) and clustering (via
``work_buddy.ml.clustering``) for journal-specific workflows.

The general-purpose clustering algorithms live in ``work_buddy.ml.clustering``.
This module adds journal-specific concerns:
- Embedding via Obsidian Smart Connections
- JSONL manifest loading
- Journal-aware default weights and decay parameters
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.ml.clustering import (
    categorical_similarity as tag_similarity,  # back-compat alias
    cluster_items as cluster_threads,
    compute_pairwise_similarity,
    cosine_similarity,
    generate_agent_context,
    generate_cluster_report,
    position_proximity as _position_proximity,
    suggest_merges,
    build_similarity_graph,
)

logger = get_logger(__name__)

# Re-export for existing callers
__all__ = [
    "tag_similarity",
    "position_proximity",
    "cosine_similarity",
    "compute_pairwise_similarity",
    "suggest_merges",
    "build_similarity_graph",
    "cluster_threads",
    "generate_cluster_report",
    "generate_agent_context",
    "embed_threads",
    "analyze_threads",
]


def position_proximity(
    index_a: int,
    index_b: int,
    total: int,
    is_journal: bool = False,
) -> float:
    """Journal-aware wrapper around ``ml.clustering.position_proximity``.

    Journal sources get tighter decay (sigma=0.2) because position ≈ time.
    """
    sigma = 0.2 if is_journal else 0.4
    return _position_proximity(index_a, index_b, total, sigma=sigma)


# ── Embedding Integration ────────────────────────────────────────


def embed_threads(
    threads: list[dict[str, Any]],
    use_content: bool = True,
    content_map: dict[str, str] | None = None,
) -> list[list[float]]:
    """Embed thread texts using Smart Connections' model.

    Args:
        threads: Thread manifest entries (must have 'id', 'summary').
        use_content: If True and content_map provided, embed full content.
            Otherwise embed summaries only.
        content_map: Optional {thread_id: raw_text} for full content embedding.

    Returns:
        Parallel list of embedding vectors.
    """
    from work_buddy.obsidian.smart import embed_batch

    texts = []
    for t in threads:
        if use_content and content_map and t["id"] in content_map:
            text = content_map[t["id"]][:2000]
        else:
            text = t.get("summary", t["id"])
        texts.append(text)

    results = embed_batch(texts)
    return [r["vec"] for r in results]


# ── Convenience: Full Pipeline ───────────────────────────────────


def analyze_threads(
    manifest_path: str | Path,
    content_map: dict[str, str] | None = None,
    is_journal: bool = True,
    threshold: float = 0.55,
    weights: dict[str, float] | None = None,
    resolution: float = 1.5,
) -> dict[str, Any]:
    """Full analysis pipeline: load manifest → embed → pairwise → cluster → report.

    Default weights are tuned via parameter sweep on real 35-thread data:
    embedding=0.55, tag=0.35, proximity=0.1. Scored 6/6 on known merge/separation
    test cases with max cluster size 5 and mean cohesion 0.62.

    Args:
        manifest_path: Path to thread_manifest.jsonl.
        content_map: Optional {thread_id: raw_text} for content-based embedding.
        is_journal: Whether source is a journal file (boosts proximity weight).
        threshold: Fused score threshold for pairwise merge suggestions.
        weights: Override signal weights.
            Default: {embedding: 0.55, tag: 0.35, proximity: 0.1}
        resolution: Louvain resolution (higher = more/smaller clusters). Default 1.5.

    Returns:
        Dict with 'threads', 'pairs' (top 20), 'merges', 'clusters', 'report'.
    """
    manifest_path = Path(manifest_path)
    threads = []
    for line in manifest_path.read_text(encoding="utf-8").strip().split("\n"):
        line = line.strip()
        if line:
            threads.append(json.loads(line))

    logger.info(f"Analyzing {len(threads)} threads from {manifest_path.name}")

    # Embed
    embeddings = embed_threads(threads, use_content=bool(content_map), content_map=content_map)

    # Pairwise similarity (tuned defaults)
    w = weights or {"embedding": 0.55, "tag": 0.35, "proximity": 0.1}
    sigma = 0.2 if is_journal else 0.4
    pairs = compute_pairwise_similarity(
        threads, embeddings, weights=w, sigma=sigma,
    )

    # Merge suggestions
    merges = suggest_merges(pairs, threads, threshold=threshold)

    # Stats
    above_threshold = sum(1 for p in pairs if p["fused"] >= threshold)

    logger.info(
        f"Analysis complete: {len(pairs)} pairs, "
        f"{above_threshold} above threshold, {len(merges)} merge suggestions"
    )

    # Graph clustering (tuned resolution)
    clusters = cluster_threads(
        threads, pairs, edge_threshold=threshold * 0.85, resolution=resolution
    )

    logger.info(
        f"Analysis complete: {len(pairs)} pairs, "
        f"{above_threshold} above threshold, {len(merges)} pair merges, "
        f"{len(clusters)} clusters"
    )

    # Generate agent report
    report = generate_cluster_report(clusters, pairs[:20])

    return {
        "thread_count": len(threads),
        "pair_count": len(pairs),
        "above_threshold": above_threshold,
        "merge_count": len(merges),
        "merges": merges,
        "clusters": clusters,
        "top_pairs": pairs[:20],
        "threads": threads,
        "report": report,
    }
