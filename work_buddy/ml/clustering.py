"""General-purpose multi-signal graph clustering.

Extracted from ``work_buddy.journal_backlog.similarity`` — the algorithms
here are source-agnostic.  Any module that needs to cluster items by
embedding similarity, categorical overlap, and/or positional proximity
should use this module rather than reimplementing.

Pure Python + networkx only.  Safe to import from subprocesses.

Signal functions
----------------
- ``cosine_similarity`` — vector dot-product similarity
- ``categorical_similarity`` — Jaccard coefficient on label sets
- ``position_proximity`` — exponential positional decay

Clustering pipeline
-------------------
1. ``compute_pairwise_similarity`` — fused multi-signal scores
2. ``build_similarity_graph`` — networkx weighted graph
3. ``cluster_items`` — Louvain community detection
4. ``suggest_merges`` — threshold-based merge candidates

Reporting
---------
- ``generate_cluster_report`` — markdown for agent review
- ``generate_agent_context`` — structured dict for LLM consumption
"""

from __future__ import annotations

import math
from typing import Any, Callable

import networkx as nx

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ── Signal Functions ─────────────────────────────────────────────


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def categorical_similarity(
    set_a: set[str] | list[str],
    set_b: set[str] | list[str],
    exclude: set[str] | None = None,
) -> float:
    """Jaccard similarity between two label/tag sets.

    Returns 0.0 (no overlap) to 1.0 (identical).

    Args:
        set_a: First set of labels.
        set_b: Second set of labels.
        exclude: Labels to ignore (e.g. structural tags like ``#multi``).
    """
    exclude = exclude or set()
    a = {t for t in set_a if t not in exclude}
    b = {t for t in set_b if t not in exclude}
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def position_proximity(
    index_a: int,
    index_b: int,
    total: int,
    sigma: float = 0.4,
) -> float:
    """Proximity score based on position in a sequence.

    Uses Gaussian decay: adjacent items score ~1.0, distant items ~0.0.

    Args:
        index_a: Position of first item.
        index_b: Position of second item.
        total: Total number of items.
        sigma: Decay rate (smaller = tighter decay).
            Common values: 0.2 for temporal sequences (journal),
            0.4 for spatial sequences (document sections).
    """
    if total <= 1:
        return 1.0
    distance = abs(index_a - index_b) / (total - 1)
    return math.exp(-(distance ** 2) / (2 * sigma ** 2))


# ── Multi-Signal Fusion ─────────────────────────────────────────


def compute_pairwise_similarity(
    items: list[dict[str, Any]],
    embeddings: list[list[float]],
    weights: dict[str, float] | None = None,
    *,
    sigma: float = 0.4,
    proximity_fn: Callable[[dict, dict, int], float] | None = None,
) -> list[dict[str, Any]]:
    """Compute fused pairwise similarity between all item pairs.

    Items must have ``id`` and ``tags`` (list[str]) keys.  Order in list
    determines positional proximity (unless ``proximity_fn`` overrides).

    Args:
        items: List of item dicts (must have 'id', 'tags').
        embeddings: Parallel list of embedding vectors.
        weights: Signal weights.
            Default: ``{embedding: 0.5, tag: 0.3, proximity: 0.2}``.
            Set a weight to 0 to disable that signal.
        sigma: Positional decay parameter (passed to ``position_proximity``).
            Ignored when ``proximity_fn`` is provided.
        proximity_fn: Optional callable ``(item_a, item_b, total) -> float``
            that returns a proximity score in [0, 1].  When provided, replaces
            the default list-order ``position_proximity``.  Use this when items
            have non-linear spatial semantics (e.g. multi-window tab layouts).

    Returns:
        Sorted list of pair dicts (descending by fused score).
    """
    w = weights or {"embedding": 0.5, "tag": 0.3, "proximity": 0.2}
    n = len(items)
    pairs = []

    for i in range(n):
        for j in range(i + 1, n):
            emb_sim = cosine_similarity(embeddings[i], embeddings[j])
            tag_sim = categorical_similarity(
                items[i].get("tags", []),
                items[j].get("tags", []),
            )
            if proximity_fn is not None:
                prox = proximity_fn(items[i], items[j], n)
            else:
                prox = position_proximity(i, j, n, sigma=sigma)

            fused = (
                w.get("embedding", 0) * emb_sim
                + w.get("tag", 0) * tag_sim
                + w.get("proximity", 0) * prox
            )

            pairs.append({
                "id_a": items[i]["id"],
                "id_b": items[j]["id"],
                "fused": round(fused, 4),
                "embedding_sim": round(emb_sim, 4),
                "tag_sim": round(tag_sim, 4),
                "proximity": round(prox, 4),
            })

    pairs.sort(key=lambda p: p["fused"], reverse=True)
    return pairs


def suggest_merges(
    pairs: list[dict[str, Any]],
    items: list[dict[str, Any]],
    threshold: float = 0.65,
) -> list[dict[str, Any]]:
    """Identify merge candidates from pairwise similarity.

    Uses greedy assignment: highest-scoring pairs consumed first, each item
    appears in at most one merge group.
    """
    item_map = {t["id"]: t for t in items}
    consumed: set[str] = set()
    merges: list[dict[str, Any]] = []

    for pair in pairs:
        if pair["fused"] < threshold:
            break

        a, b = pair["id_a"], pair["id_b"]
        if a in consumed or b in consumed:
            continue

        reasons = []
        if pair["embedding_sim"] > 0.7:
            reasons.append(f"semantically similar ({pair['embedding_sim']:.2f})")
        if pair["tag_sim"] > 0.5:
            reasons.append(f"shared tags ({pair['tag_sim']:.2f})")
        if pair["proximity"] > 0.7:
            reasons.append(f"close in sequence ({pair['proximity']:.2f})")

        merges.append({
            "ids": [a, b],
            "fused_score": pair["fused"],
            "embedding_sim": pair["embedding_sim"],
            "tag_sim": pair["tag_sim"],
            "proximity": pair["proximity"],
            "summaries": [
                item_map[a].get("summary", ""),
                item_map[b].get("summary", ""),
            ],
            "tags_a": item_map[a].get("tags", []),
            "tags_b": item_map[b].get("tags", []),
            "reason": "; ".join(reasons) if reasons else "combined signal",
        })

        consumed.add(a)
        consumed.add(b)

    return merges


# ── Graph Building & Community Detection ─────────────────────────


def build_similarity_graph(
    items: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    edge_threshold: float = 0.5,
) -> nx.Graph:
    """Build a weighted graph from items and pairwise similarities.

    Nodes = items (with metadata).  Edges = fused similarity above threshold.
    """
    G = nx.Graph()

    for i, t in enumerate(items):
        G.add_node(t["id"], **{
            "summary": t.get("summary", ""),
            "tags": t.get("tags", []),
            "position": i,
            "multi": t.get("multi", False),
        })

    for p in pairs:
        if p["fused"] >= edge_threshold:
            G.add_edge(
                p["id_a"], p["id_b"],
                weight=p["fused"],
                embedding_sim=p["embedding_sim"],
                tag_sim=p["tag_sim"],
                proximity=p["proximity"],
            )

    return G


def cluster_items(
    items: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    edge_threshold: float = 0.5,
    resolution: float = 1.0,
) -> list[dict[str, Any]]:
    """Cluster items into coherent groups using Louvain community detection.

    Singletons (items with no strong connections) form their own cluster.

    Args:
        items: Item dicts (must have 'id'; optionally 'tags', 'summary').
        pairs: Output of ``compute_pairwise_similarity``.
        edge_threshold: Minimum fused score for graph edges.
        resolution: Louvain resolution (higher = more/smaller clusters).

    Returns:
        List of cluster dicts with cluster_id, thread_ids, size, threads,
        internal_cohesion, shared_tags, label, cross_cluster_edges.
    """
    item_map = {t["id"]: t for t in items}
    G = build_similarity_graph(items, pairs, edge_threshold)

    # Detect communities via Louvain
    if G.number_of_edges() == 0:
        communities = {t["id"]: i for i, t in enumerate(items)}
    else:
        detected = nx.community.louvain_communities(
            G, weight="weight", resolution=resolution, seed=42
        )
        node_to_cluster: dict[str, int] = {}
        for cid, members in enumerate(detected):
            for node in members:
                node_to_cluster[node] = cid
        max_cid = len(detected)
        for t in items:
            if t["id"] not in node_to_cluster:
                node_to_cluster[t["id"]] = max_cid
                max_cid += 1
        communities = node_to_cluster

    # Build cluster objects
    cluster_members: dict[int, list[str]] = {}
    for tid, cid in communities.items():
        cluster_members.setdefault(cid, []).append(tid)

    pair_lookup: dict[tuple[str, str], dict] = {}
    for p in pairs:
        key = tuple(sorted([p["id_a"], p["id_b"]]))
        pair_lookup[key] = p

    clusters = []
    for cid, member_ids in sorted(cluster_members.items()):
        member_ids.sort(
            key=lambda tid: next(
                (i for i, t in enumerate(items) if t["id"] == tid), 0
            )
        )

        member_items = [item_map[tid] for tid in member_ids if tid in item_map]

        # Internal cohesion
        internal_weights = []
        for i, a in enumerate(member_ids):
            for b in member_ids[i + 1:]:
                key = tuple(sorted([a, b]))
                if key in pair_lookup:
                    internal_weights.append(pair_lookup[key]["fused"])
        cohesion = (
            sum(internal_weights) / len(internal_weights)
            if internal_weights else 0.0
        )

        # Shared tags
        if member_items:
            tag_sets = [set(t.get("tags", [])) for t in member_items]
            shared = tag_sets[0]
            for ts in tag_sets[1:]:
                shared &= ts
        else:
            shared = set()

        # Auto-label
        if shared:
            label = ", ".join(sorted(shared))
        elif member_items:
            label = member_items[0].get("summary", "")[:60]
        else:
            label = f"cluster-{cid}"

        # Cross-cluster edges
        cross_edges = []
        for tid in member_ids:
            for p in pairs:
                if p["fused"] < edge_threshold * 0.8:
                    continue
                other = None
                if p["id_a"] == tid and p["id_b"] not in member_ids:
                    other = p["id_b"]
                elif p["id_b"] == tid and p["id_a"] not in member_ids:
                    other = p["id_a"]
                if other:
                    other_cid = communities.get(other)
                    cross_edges.append({
                        "from": tid,
                        "to": other,
                        "to_cluster": other_cid,
                        "fused": p["fused"],
                        "embedding_sim": p["embedding_sim"],
                    })

        best_cross: dict[int, dict] = {}
        for ce in cross_edges:
            tc = ce["to_cluster"]
            if tc is not None and (
                tc not in best_cross or ce["fused"] > best_cross[tc]["fused"]
            ):
                best_cross[tc] = ce
        cross_edges = sorted(
            best_cross.values(), key=lambda x: x["fused"], reverse=True
        )

        clusters.append({
            "cluster_id": cid,
            "thread_ids": member_ids,
            "size": len(member_ids),
            "threads": [
                {
                    "id": t["id"],
                    "summary": t.get("summary", ""),
                    "tags": t.get("tags", []),
                }
                for t in member_items
            ],
            "internal_cohesion": round(cohesion, 3),
            "shared_tags": sorted(shared),
            "label": label,
            "cross_cluster_edges": cross_edges[:5],
        })

    clusters.sort(key=lambda c: (-c["size"], c["cluster_id"]))
    return clusters


# ── Reporting ───────────────────────────────────────────────────


def generate_cluster_report(
    clusters: list[dict[str, Any]],
    top_pairs: list[dict[str, Any]] | None = None,
) -> str:
    """Generate a markdown report for agent review of clustered items.

    Two sections: hard clusters ("best guess at information units")
    and fuzzy edges ("cross-cluster connections that might indicate errors").
    """
    multi_clusters = [c for c in clusters if c["size"] > 1]
    singletons = [c for c in clusters if c["size"] == 1]

    lines = [
        "# Cluster Analysis",
        "",
        f"**Clusters:** {len(multi_clusters)} multi-item + {len(singletons)} singletons",
        f"**Total items:** {sum(c['size'] for c in clusters)}",
        "",
    ]

    if multi_clusters:
        lines.append("---")
        lines.append("## Clusters (multi-item)")
        lines.append("")

        for c in multi_clusters:
            cohesion_label = (
                "strong" if c["internal_cohesion"] > 0.7
                else "moderate" if c["internal_cohesion"] > 0.5
                else "weak"
            )

            lines.append(f"### Cluster {c['cluster_id']} — {c['label']}")
            lines.append(
                f"**{c['size']} items** | "
                f"Cohesion: {c['internal_cohesion']:.2f} ({cohesion_label})"
            )
            if c["shared_tags"]:
                lines.append(f"Shared tags: {' '.join(c['shared_tags'])}")
            lines.append("")

            for t in c["threads"]:
                tags = " ".join(t["tags"]) if t["tags"] else ""
                lines.append(f"- `{t['id']}` {tags}")
                lines.append(f"  > {t['summary']}")
            lines.append("")

            if c["cross_cluster_edges"]:
                lines.append("**Fuzzy edges** (connections to other clusters):")
                for ce in c["cross_cluster_edges"]:
                    lines.append(
                        f"  - `{ce['from']}` ↔ `{ce['to']}` "
                        f"(cluster {ce['to_cluster']}, "
                        f"fused={ce['fused']:.2f}, emb={ce['embedding_sim']:.2f})"
                    )
                lines.append("")

    if singletons:
        lines.append("---")
        lines.append("## Singletons (no strong connections)")
        lines.append("")

        for c in singletons:
            t = c["threads"][0] if c["threads"] else {}
            tags = " ".join(t.get("tags", []))
            lines.append(f"- `{t.get('id', '?')}` {tags}")
            lines.append(f"  > {t.get('summary', '')}")

            if c["cross_cluster_edges"]:
                best = c["cross_cluster_edges"][0]
                lines.append(
                    f"  *Nearest: `{best['to']}` in cluster {best['to_cluster']} "
                    f"(fused={best['fused']:.2f})*"
                )
        lines.append("")

    return "\n".join(lines)


def generate_agent_context(
    clusters: list[dict[str, Any]],
    content_map: dict[str, str] | None = None,
    include_content: bool = False,
    max_content_chars: int = 500,
) -> dict[str, Any]:
    """Generate structured context for an agent to review and act on clusters.

    Returns a dict designed for LLM consumption: compact enough to fit in
    context, structured enough for programmatic reasoning.
    """
    multi = [c for c in clusters if c["size"] > 1]
    singletons = [c for c in clusters if c["size"] == 1]

    def confidence(c: dict) -> str:
        if c["internal_cohesion"] >= 0.7:
            return "high"
        elif c["internal_cohesion"] >= 0.5:
            return "medium"
        return "low"

    cluster_items_out = []
    for c in multi:
        threads = []
        for t in c["threads"]:
            thread_data = {
                "id": t["id"],
                "summary": t["summary"],
                "tags": t["tags"],
            }
            if include_content and content_map and t["id"] in content_map:
                thread_data["content_snippet"] = content_map[t["id"]][:max_content_chars]
            threads.append(thread_data)

        cluster_items_out.append({
            "cluster_id": c["cluster_id"],
            "label": c["label"],
            "size": c["size"],
            "shared_tags": c["shared_tags"],
            "confidence": confidence(c),
            "cohesion": c["internal_cohesion"],
            "threads": threads,
            "fuzzy_edges": [
                {
                    "from_thread": ce["from"],
                    "to_thread": ce["to"],
                    "to_cluster": ce["to_cluster"],
                    "strength": ce["fused"],
                }
                for ce in c["cross_cluster_edges"][:3]
            ],
        })

    uncertain = []
    for c in clusters:
        for ce in c.get("cross_cluster_edges", []):
            if ce["fused"] > 0.5:
                uncertain.append({
                    "thread_a": ce["from"],
                    "cluster_a": c["cluster_id"],
                    "thread_b": ce["to"],
                    "cluster_b": ce["to_cluster"],
                    "strength": ce["fused"],
                })
    seen_edges: set[tuple[str, str]] = set()
    unique_uncertain = []
    for u in sorted(uncertain, key=lambda x: x["strength"], reverse=True):
        edge_key = tuple(sorted([u["thread_a"], u["thread_b"]]))
        if edge_key not in seen_edges:
            seen_edges.add(edge_key)
            unique_uncertain.append(u)

    singleton_items = []
    for c in singletons:
        t = c["threads"][0] if c["threads"] else {}
        item = {
            "id": t.get("id", "?"),
            "summary": t.get("summary", ""),
            "tags": t.get("tags", []),
        }
        if c["cross_cluster_edges"]:
            nearest = c["cross_cluster_edges"][0]
            item["nearest_cluster"] = nearest["to_cluster"]
            item["nearest_strength"] = nearest["fused"]
        if include_content and content_map and t.get("id") in (content_map or {}):
            item["content_snippet"] = content_map[t["id"]][:max_content_chars]
        singleton_items.append(item)

    return {
        "summary": {
            "total_items": sum(c["size"] for c in clusters),
            "multi_clusters": len(multi),
            "singletons": len(singletons),
            "high_confidence_clusters": sum(
                1 for c in multi if confidence(c) == "high"
            ),
            "uncertain_edges": len(unique_uncertain),
        },
        "clusters": cluster_items_out,
        "singletons": singleton_items,
        "uncertain_edges": unique_uncertain[:10],
    }
