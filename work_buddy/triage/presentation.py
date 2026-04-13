"""Build a triage presentation object for UI consumption.

Transforms the contextualize step output into a clean, flat structure
that a modal (Obsidian, Telegram, or any other UI) can render directly.

Source-agnostic — works for Chrome tabs, journal entries, conversations.
The modal doesn't need to know what kind of items these are beyond
``source`` and ``label``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.triage.items import TRIAGE_ACTIONS

logger = get_logger(__name__)

# Where the presentation is written for the modal to pick up
from work_buddy.paths import data_dir

_PRESENTATION_DIR = data_dir("agents")
_PRESENTATION_FILENAME = "triage_presentation.json"


def build_presentation(
    contextualize_output: dict[str, Any],
    clarifying_questions: list[dict[str, str]] | None = None,
    item_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Transform contextualize output into a modal-ready presentation.

    Args:
        contextualize_output: Output of ``contextualize_intents`` (or
            ``contextualize_intents_from_raw``).
        clarifying_questions: Optional questions the main agent wants
            to ask the user, embedded in the modal.  Each dict:
            ``{group_index: int, question: str}``.
        item_metadata: Optional lookup ``{item_id: {title, url, summary}}``
            enriched from the summarize step's cluster data.

    Returns:
        A flat, UI-ready dict.  No nested cluster objects, no embeddings,
        no raw step data — just what the modal needs to render.
    """
    groups = contextualize_output.get("intent_groups", [])
    uncategorized = contextualize_output.get("uncategorized_tabs", [])
    narrative = contextualize_output.get("overall_narrative", "")
    item_metadata = item_metadata or {}

    # Detect source from items (all items in a triage run share a source)
    source = _detect_source(groups)

    # Group by suggested action
    groups_by_action: dict[str, list[dict[str, Any]]] = {
        action: [] for action in TRIAGE_ACTIONS
    }

    all_item_ids: list[str] = []

    for i, group in enumerate(groups):
        action = group.get("suggested_action", "leave")
        if action not in groups_by_action:
            action = "leave"

        # Flatten items to display objects, enriched from metadata
        items = []
        for item_id in group.get("tab_ids", []):
            meta = item_metadata.get(item_id, {})
            title = meta.get("title") or item_id
            url = meta.get("url", "")
            summary = meta.get("summary", "")
            item = {"id": item_id, "label": title}
            if url:
                item["url"] = url
            if summary:
                item["summary"] = summary
            tab_id = meta.get("tab_id")
            if tab_id is not None:
                item["tab_id"] = tab_id
            items.append(item)
            all_item_ids.append(item_id)

        presentation_group = {
            "index": i,
            "intent": group.get("intent", ""),
            "confidence": group.get("confidence", "low"),
            "items": items,
            "rationale": group.get("action_detail", ""),
            "context": group.get("context", ""),
            "ambiguities": group.get("ambiguities", []),
            "likely_task_id": group.get("likely_task_id", ""),
            "suggested_action": action,
        }

        # Flag revisions
        if "[REVISED]" in group.get("action_detail", ""):
            presentation_group["revised"] = True

        # Attach suggested task text for create_task actions
        if action == "create_task":
            # Extract from action_detail if present
            detail = group.get("action_detail", "")
            presentation_group["suggested_task_text"] = detail

        groups_by_action[action].append(presentation_group)

    # Attach clarifying questions to their target groups
    questions_by_group: dict[int, list[str]] = {}
    for q in (clarifying_questions or []):
        gidx = q.get("group_index", -1)
        questions_by_group.setdefault(gidx, []).append(q.get("question", ""))

    for action_groups in groups_by_action.values():
        for g in action_groups:
            gidx = g["index"]
            if gidx in questions_by_group:
                g["clarifying_questions"] = questions_by_group[gidx]

    # Compute seriation order (semantic similarity layout)
    all_groups_flat = [g for gs in groups_by_action.values() for g in gs]
    display_order = _compute_display_order(all_groups_flat)

    presentation = {
        "source": source,
        "narrative": narrative,
        "total_groups": len(groups),
        "total_items": len(all_item_ids),
        "groups_by_action": groups_by_action,
        "display_order": display_order,
        "uncategorized": uncategorized,
        "available_detail_ids": all_item_ids,
        "has_clarifying_questions": bool(clarifying_questions),
        "revisions": contextualize_output.get("revisions", 0),
    }

    return presentation


def save_presentation(
    presentation: dict[str, Any],
    session_dir: Path | None = None,
) -> Path:
    """Write the presentation to a file for the modal to pick up.

    Args:
        presentation: Output of ``build_presentation``.
        session_dir: Agent session directory.  If None, uses a default
            location under the agents/ dir.

    Returns:
        Path to the written file.
    """
    if session_dir is None:
        from work_buddy.agent_session import get_session_dir
        session_dir = get_session_dir()

    out_path = session_dir / _PRESENTATION_FILENAME
    out_path.write_text(
        json.dumps(presentation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Triage presentation saved: %s", out_path)
    return out_path


def load_presentation(session_dir: Path | None = None) -> dict[str, Any] | None:
    """Load a previously saved presentation from the session directory.

    Returns ``None`` if no presentation file exists.
    """
    if session_dir is None:
        from work_buddy.agent_session import get_session_dir
        session_dir = get_session_dir()

    path = session_dir / _PRESENTATION_FILENAME
    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load presentation from %s: %s", path, exc)
        return None


_DECISIONS_FILENAME = "triage_decisions.json"


def save_decisions(
    decisions: dict[str, Any],
    session_dir: Path | None = None,
) -> Path:
    """Write user's triage decisions to disk for audit/replay.

    Args:
        decisions: The Phase 2 review response (group_decisions,
            reassignments, item overrides, etc.).
        session_dir: Agent session directory.  If None, uses a default
            location under the agents/ dir.

    Returns:
        Path to the written file.
    """
    if session_dir is None:
        from work_buddy.agent_session import get_session_dir
        session_dir = get_session_dir()

    out_path = session_dir / _DECISIONS_FILENAME
    out_path.write_text(
        json.dumps(decisions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Triage decisions saved: %s", out_path)
    return out_path


def _compute_display_order(
    all_groups: list[dict[str, Any]],
) -> list[int]:
    """Compute a semantic seriation order for groups.

    Embeds each group (intent + item labels combined) and uses greedy
    nearest-neighbor seriation so semantically similar groups appear
    adjacent in the display.

    Falls back to original index order if embedding fails.
    """
    if len(all_groups) <= 2:
        return [g["index"] for g in all_groups]

    # Build text for each group: intent + all item labels
    texts = []
    for g in all_groups:
        parts = [g.get("intent", "")]
        for item in g.get("items", []):
            parts.append(item.get("label", ""))
        # Include rationale for extra signal
        if g.get("rationale"):
            parts.append(g["rationale"])
        texts.append(" ".join(parts))

    ids = [str(g["index"]) for g in all_groups]

    try:
        from work_buddy.embedding.client import embed
        vectors = embed(texts)
        if not vectors or len(vectors) != len(ids):
            logger.warning("Embedding returned unexpected result, using index order")
            return [g["index"] for g in all_groups]

        from work_buddy.ml.seriation import seriate_by_cosine
        result = seriate_by_cosine(ids, vectors)
        order = [int(x) for x in result["order"]]

        logger.info(
            "Seriated %d groups (adj similarities: %s)",
            len(order),
            [f"{s:.2f}" for s in result["adjacent_similarities"]],
        )
        return order

    except Exception as e:
        logger.warning("Seriation failed, using index order: %s", e)
        return [g["index"] for g in all_groups]


def _extract_item_metadata(
    summarize_output: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build an item metadata lookup from the summarize step output.

    Extracts titles, URLs, and Haiku summaries from the cluster/singleton
    data produced by the summarize step.

    Returns:
        Dict mapping item_id (the slug label, e.g. 'chatgpt-work-buddy-xxx')
        to ``{title, url, summary}``.
    """
    metadata: dict[str, dict[str, Any]] = {}

    # Process items from clusters and singletons
    for source_key in ("clusters", "singletons"):
        for cluster in summarize_output.get(source_key, []):
            items = cluster.get("items", [])
            if isinstance(cluster, dict) and "items" not in cluster:
                # Singleton might be a flat item
                items = [cluster]
            for item in items:
                item_id = item.get("label") or item.get("id", "")
                if not item_id:
                    continue

                meta = item.get("metadata", {})
                title = meta.get("title", "")
                url = item.get("url", "")

                # Extract Haiku summary
                summary_data = meta.get("summary_data", {})
                summary = summary_data.get("content_summary", "")

                # Chrome tab ID for mutations (close, group, move)
                tab_id = meta.get("tab_id")

                entry = {
                    "title": title or item_id,
                    "url": url,
                    "summary": summary,
                }
                if tab_id is not None:
                    entry["tab_id"] = tab_id

                metadata[item_id] = entry

    if metadata:
        logger.info("Extracted metadata for %d items", len(metadata))

    return metadata


def _detect_source(groups: list[dict[str, Any]]) -> str:
    """Detect the data source from intent group items."""
    for group in groups:
        for item_id in group.get("tab_ids", []):
            if isinstance(item_id, str):
                if item_id.startswith("tab_"):
                    return "chrome"
                if item_id.startswith("journal_"):
                    return "journal"
                if item_id.startswith("conv_"):
                    return "conversation"
    return "unknown"


# ── auto_run entry point ────────────────────────────────────────


def build_presentation_from_raw(
    contextualize_output: dict[str, Any],
    summarize_output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Auto_run entry point: build + save the presentation.

    Args:
        contextualize_output: Output of contextualize_intents_from_raw.
        summarize_output: Output of the summarize step (clusters with
            item metadata, titles, URLs, and Haiku summaries).

    Returns:
        The presentation dict (also saved to disk).
    """
    # Extract item metadata from cluster data (titles, URLs, summaries)
    item_metadata = _extract_item_metadata(summarize_output or {})

    presentation = build_presentation(
        contextualize_output, item_metadata=item_metadata,
    )

    try:
        path = save_presentation(presentation)
        presentation["saved_to"] = str(path)
    except Exception as e:
        logger.warning("Could not save presentation: %s", e)

    return {"success": True, "presentation": presentation}
