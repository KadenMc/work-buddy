"""Match triage clusters against existing tasks.

Embeds active task descriptions and computes cosine similarity against
cluster centroids to find potential matches.

Runs in a **subprocess** (auto_run) — safe to import sqlite3.
"""

from __future__ import annotations

import re
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.ml.clustering import cosine_similarity
from work_buddy.triage.items import TaskMatch, TriageCluster, TriageItem

logger = get_logger(__name__)

# Match threshold — deliberately low since the LLM recommendation step
# does the final judgment on whether a match is real.
MATCH_THRESHOLD = 0.35
TOP_K = 3


def match_clusters_to_tasks(
    clusters: list[TriageCluster],
    task_states: list[str] | None = None,
) -> list[TriageCluster]:
    """Enrich clusters with matching existing tasks.

    Args:
        clusters: TriageClusters (must have centroids from clustering step).
        task_states: Task states to consider (default: inbox, mit, focused).

    Returns:
        Same clusters with ``task_matches`` populated.
    """
    task_states = task_states or ["inbox", "mit", "focused"]

    # Load active tasks
    tasks = _load_active_tasks(task_states)
    if not tasks:
        logger.info("No active tasks found for matching")
        return clusters

    # Embed task descriptions
    task_texts = [t["text"] for t in tasks]
    task_embeddings = _embed_texts(task_texts)
    if task_embeddings is None:
        logger.warning("Embedding service unavailable — skipping task matching")
        return clusters

    # Match each cluster centroid against task embeddings
    matched_count = 0
    for cluster in clusters:
        if cluster.centroid is None:
            continue

        matches = []
        for i, task in enumerate(tasks):
            score = cosine_similarity(cluster.centroid, task_embeddings[i])
            if score >= MATCH_THRESHOLD:
                matches.append(TaskMatch(
                    task_id=task["task_id"],
                    task_text=task["text"],
                    project=task.get("project"),
                    score=score,
                ))

        matches.sort(key=lambda m: m.score, reverse=True)
        cluster.task_matches = matches[:TOP_K]
        if cluster.task_matches:
            matched_count += 1

    logger.info(
        "Task matching: %d/%d clusters matched against %d active tasks",
        matched_count, len(clusters), len(tasks),
    )
    return clusters


def _load_active_tasks(states: list[str]) -> list[dict[str, Any]]:
    """Load active tasks with their text descriptions.

    Reads metadata from SQLite store, then reads task text from the
    master task list file.
    """
    from work_buddy.obsidian.tasks import store

    all_tasks: list[dict[str, Any]] = []
    for state in states:
        rows = store.query(state=state)
        all_tasks.extend(rows)

    if not all_tasks:
        return []

    # Read task lines from master file to get descriptions
    task_texts = _read_task_texts()

    result = []
    for task in all_tasks:
        tid = task["task_id"]
        text = task_texts.get(tid, "")
        if not text:
            continue
        result.append({
            "task_id": tid,
            "text": text,
            "state": task["state"],
            "project": task.get("contract") or "",
        })

    return result


def _read_task_texts() -> dict[str, str]:
    """Read task descriptions from master task list, keyed by task ID."""
    from work_buddy.obsidian import bridge

    content = bridge.read_file("tasks/master-task-list.md")
    if not content:
        return {}

    result: dict[str, str] = {}
    id_pattern = re.compile(r"🆔\s*(t-[a-f0-9]+)")
    for line in content.split("\n"):
        if not line.strip().startswith("- ["):
            continue
        match = id_pattern.search(line)
        if not match:
            continue
        task_id = match.group(1)
        # Strip markdown checkbox, tags, wikilinks, emoji fields
        desc = re.sub(r"^- \[.\]\s*", "", line)
        desc = re.sub(r"#\S+", "", desc)
        desc = re.sub(r"\[\[[^\]]+\]\]", "", desc)
        desc = re.sub(r"[🆔📅✅🔼⏫]\s*\S*", "", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        if desc:
            result[task_id] = desc

    return result


def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed texts via the embedding service."""
    from work_buddy.embedding.client import embed
    return embed(texts)


# ── auto_run entry point ────────────────────────────────────────


def match_clusters_to_tasks_from_raw(
    clusters_data: dict[str, Any],
) -> dict[str, Any]:
    """Auto_run entry point: enriches clusters with task matches.

    Expects the output of ``cluster_items_from_raw`` as input (with
    'clusters' and 'singletons' keys).
    """
    all_cluster_dicts = (
        clusters_data.get("clusters", [])
        + clusters_data.get("singletons", [])
    )
    clusters = [TriageCluster.from_dict(d) for d in all_cluster_dicts]

    clusters = match_clusters_to_tasks(clusters)

    # Re-split into multi and singletons
    multi = [c for c in clusters if c.size > 1]
    singletons = [c for c in clusters if c.size == 1]
    task_match_count = sum(1 for c in clusters if c.task_matches)

    return {
        "success": True,
        "clusters": [c.to_dict() for c in multi],
        "singletons": [c.to_dict() for c in singletons],
        "item_count": clusters_data.get("item_count", 0),
        "cluster_count": len(multi),
        "singleton_count": len(singletons),
        "task_match_count": task_match_count,
    }
