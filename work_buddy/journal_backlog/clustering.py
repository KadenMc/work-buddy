"""Tag-based thread clustering and linearization for review documents.

Given threads with tag sets, computes pairwise similarity, clusters
by tag overlap, and produces a linear ordering where related threads
are adjacent. Section breaks are inserted where similarity drops.
"""

from __future__ import annotations

from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two tag sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def compute_similarity_matrix(
    entries: list[dict[str, Any]],
) -> dict[tuple[str, str], float]:
    """Compute pairwise Jaccard similarity between thread tag sets.

    Args:
        entries: Manifest entries, each with ``id`` and ``tags``.

    Returns:
        Dict mapping ``(id_a, id_b)`` to similarity score.
    """
    tag_sets = {e["id"]: set(e.get("tags", [])) for e in entries}
    ids = list(tag_sets.keys())
    matrix: dict[tuple[str, str], float] = {}

    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i < j:
                sim = jaccard_similarity(tag_sets[a], tag_sets[b])
                matrix[(a, b)] = sim
                matrix[(b, a)] = sim
            elif i == j:
                matrix[(a, a)] = 1.0

    return matrix


def linearize_threads(
    entries: list[dict[str, Any]],
    break_threshold: float = 0.15,
) -> list[list[dict[str, Any]]]:
    """Cluster and linearize threads by tag similarity.

    Delegates to ``work_buddy.ml.seriation.seriate`` for the core
    greedy nearest-neighbor algorithm. This function handles the
    tag-specific similarity matrix and cluster-break grouping.

    Args:
        entries: Manifest entries with ``id`` and ``tags``.
        break_threshold: Similarity below which a cluster break
            is inserted between adjacent threads.

    Returns:
        List of clusters, where each cluster is a list of manifest
        entries in linearized order.
    """
    if not entries:
        return []
    if len(entries) == 1:
        return [entries]

    from work_buddy.ml.seriation import seriate

    matrix = compute_similarity_matrix(entries)
    entry_map = {e["id"]: e for e in entries}
    ids = [e["id"] for e in entries]

    result = seriate(matrix, ids, break_threshold=break_threshold)

    # Group ordered entries by breaks
    clusters: list[list[dict[str, Any]]] = []
    current_cluster: list[dict[str, Any]] = []
    break_set = set(result["breaks"])

    for i, tid in enumerate(result["order"]):
        if i in break_set and current_cluster:
            clusters.append(current_cluster)
            current_cluster = []
        current_cluster.append(entry_map[tid])

    if current_cluster:
        clusters.append(current_cluster)

    logger.info(
        f"Linearized {len(entries)} threads into {len(clusters)} clusters"
    )
    return clusters


def cluster_label(entries: list[dict[str, Any]]) -> str:
    """Generate a human-readable label for a cluster.

    Finds the tags shared by the most entries in the cluster
    and builds a label from them.
    """
    if not entries:
        return "Empty"

    # Count tag frequency across cluster members
    tag_counts: dict[str, int] = {}
    for e in entries:
        for tag in e.get("tags", []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    if not tag_counts:
        return "Untagged"

    # Find tags shared by majority (>50%) of cluster members
    threshold = len(entries) / 2
    shared = [t for t, c in tag_counts.items() if c > threshold]

    if shared:
        # Sort by frequency descending, then alphabetically
        shared.sort(key=lambda t: (-tag_counts[t], t))
        return " + ".join(shared)

    # No majority tags — use the most common one
    most_common = max(tag_counts, key=lambda t: tag_counts[t])
    return most_common


def generate_clustered_review(
    threads: list[dict[str, Any]],
    manifest_entries: list[dict[str, Any]],
    journal_date: str,
    source_dates: list[str],
    break_threshold: float = 0.15,
) -> str:
    """Generate a review document with tag-similarity-based clustering.

    Threads are ordered so that related items appear adjacent, with
    visual cluster breaks where topic similarity drops.

    Args:
        threads: Thread objects from ``extract_threads()``.
        manifest_entries: Parsed manifest entries.
        journal_date: Source journal date.
        source_dates: Carried-over date list.
        break_threshold: Jaccard similarity threshold for cluster breaks.

    Returns:
        Formatted markdown string.
    """
    thread_map = {t["id"]: t for t in threads}
    clusters = linearize_threads(manifest_entries, break_threshold)

    total_threads = sum(len(c) for c in clusters)
    lines = [
        "# Segmentation Review",
        "",
        f"**Source:** `journal/{journal_date}.md` Running Notes",
        f"**Threads:** {total_threads} across {len(clusters)} clusters",
        f"**Dates spanned:** {source_dates[0] if source_dates else '?'}"
        f" to {journal_date}",
        "",
        "## How to review",
        "",
        "Threads are ordered by tag similarity — related items appear",
        "together. Cluster headings show shared tags. For each thread:",
        "",
        "- **Looks correct?** No action needed",
        "- **Should merge** with another? Note: `MERGE t_xxx + t_yyy`",
        "- **Should split?** Note: `SPLIT t_xxx`",
        "- **Wrong tags?** Note corrections",
        "",
    ]

    for cluster_idx, cluster in enumerate(clusters, 1):
        label = cluster_label(cluster)
        lines.append("---")
        lines.append(f"## Cluster {cluster_idx}: {label}")
        lines.append(f"*{len(cluster)} thread(s)*")
        lines.append("")

        for entry in cluster:
            tid = entry["id"]
            t = thread_map.get(tid)
            if not t:
                continue

            # Thread header
            multi = " `MULTI`" if entry.get("multi") else ""
            lines.append(f"### `{tid}`{multi}")

            # Tags as inline badges
            tags_str = " ".join(f"`{tag}`" for tag in entry.get("tags", []))
            lines.append(f"**Tags:** {tags_str}")

            # Agent summary as blockquote
            lines.append(f"> {entry.get('summary', '(no summary)')}")
            lines.append("")

            # Raw content
            content = t["raw_text"].strip()
            content_lines = content.split("\n")

            if len(content_lines) > 12:
                # Show first 8 lines + count
                lines.append("```markdown")
                lines.extend(content_lines[:8])
                lines.append(f"  ...")
                lines.append(f"  ({len(content_lines)} lines total)")
                lines.append("```")
            else:
                lines.append("```markdown")
                lines.append(content)
                lines.append("```")

            lines.append("")

    # Footer with merge/split instructions
    lines.extend([
        "---",
        "",
        "## Notes",
        "",
        "Record merge/split decisions below:",
        "",
        "```",
        "MERGE t_xxx + t_yyy  (reason)",
        "SPLIT t_xxx          (what to split out)",
        "TAG   t_xxx +#newtag (add a tag)",
        "TAG   t_xxx -#oldtag (remove a tag)",
        "```",
        "",
    ])

    return "\n".join(lines)
