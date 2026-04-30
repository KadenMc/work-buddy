"""Sonnet-powered grouping for triage clusters.

The "expensive reasoning" tier — runs after embedding clustering (Tier 1)
and Haiku summarization (Tier 2) have structured the raw data.

The system prompt is **templated** via Jinja2 to adapt to different
clustering lenses (``intent`` vs ``topic``) and data types (``chrome``,
``document``, etc.).  This keeps the module general-purpose while
producing data-type-appropriate groupings.

Runs in a **subprocess** (auto_run).
"""

from __future__ import annotations

import subprocess
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.prompts import get_prompt
from work_buddy.clarify.items import TRIAGE_ACTIONS, TriageCluster, TriageItem

logger = get_logger(__name__)

# ── Prompt template ─────────────────────────────────────────────
#
# Variables:
#   lens: "intent" | "topic"
#   data_type: "chrome" | "document" | "journal" | "conversation"
#   actions: list of action strings


# Action descriptions (used in the user prompt, not the template)
_ACTION_DESCRIPTIONS = {
    "close": "items have served their purpose, no pending work remains",
    "group": "related items, still actively useful, should be organized together",
    "create_task": "represents untracked work; suggest task text",
    "record_into_task": "relates to an existing task; record context into it",
    "leave": "actively in use right now, don't touch",
}


def render_system_prompt(
    lens: str = "intent",
    data_type: str = "chrome",
) -> str:
    """Render the system prompt for the given lens and data type."""
    return get_prompt(
        "triage_recommend_system",
        lens=lens,
        data_type=data_type,
        actions=list(TRIAGE_ACTIONS),
    )


def group_intents(
    clusters: list[TriageCluster],
    summaries: dict[str, dict] | None = None,
    context: dict[str, Any] | None = None,
    lens: str = "intent",
    data_type: str = "chrome",
) -> dict[str, Any]:
    """Group items by inferred intent (or topic) using Sonnet.

    Args:
        clusters: TriageClusters from the embedding+clustering step.
        summaries: {url: summary_dict} from the Haiku summarization step.
        context: Auto-extracted context (tasks, contracts, commits).
            If None, will be built automatically.
        lens: Clustering lens — "intent" (what is the user trying to do?)
            or "topic" (what is this about?).  Default "intent" for Chrome.
        data_type: Source type — "chrome", "document", "journal",
            "conversation".  Affects data-specific prompt guidance.

    Returns:
        Structured grouping result with intent_groups, uncategorized items,
        and overall narrative.
    """
    if not clusters:
        return {"intent_groups": [], "uncategorized_tabs": [], "overall_narrative": "No items to group."}

    from work_buddy.llm import LLMRunner, ModelTier

    if context is None:
        context = build_triage_context()

    system_prompt = render_system_prompt(lens=lens, data_type=data_type)
    user_prompt = _build_user_prompt(clusters, summaries or {}, context)
    schema = _build_output_schema()

    resp = LLMRunner().call(
        tier=ModelTier.FRONTIER_BALANCED,
        system=system_prompt,
        user=user_prompt,
        max_tokens=4096,
        temperature=0,
        output_schema=schema,
        cache_ttl_minutes=0,  # intent grouping should be fresh
        trace_id=f"triage_group_{lens}_{data_type}",
    )

    if resp.is_error():
        logger.warning("Intent grouping failed: %s", resp.error)
        return {
            "intent_groups": [],
            "uncategorized_tabs": [],
            "overall_narrative": f"Intent grouping failed: {resp.error}",
            "error": resp.error,
        }

    parsed = resp.structured_output or {}
    parsed["tokens"] = {
        "input": resp.input_tokens,
        "output": resp.output_tokens,
    }

    logger.info(
        "Intent grouping: %d groups, %d uncategorized (tokens: %d in / %d out)",
        len(parsed.get("intent_groups", [])),
        len(parsed.get("uncategorized_tabs", [])),
        resp.input_tokens,
        resp.output_tokens,
    )

    return parsed


def build_triage_context(
    task_states: list[str] | None = None,
    max_tasks: int | None = None,
) -> dict[str, Any]:
    """Auto-extract context for a triage LLM call.

    Thin wrapper around :mod:`work_buddy.context` since the phase-5
    refactor. Gathers active tasks, contracts, active projects, and
    recent git commits from the unified :class:`ContextCollector`
    (cache-aware) and reshapes into the pre-refactor dict for
    backward compatibility with existing callers.

    Args:
        task_states: Which task states to include. Default
            ``["inbox", "mit", "focused"]`` matches the Chrome
            cluster-level call (sees the full active set). Pass a
            narrower list (e.g. ``["focused", "mit"]``) for
            per-item triage calls where small models get
            overwhelmed by a large inbox dump.
        max_tasks: Optional cap on total tasks after state
            filtering. Earlier states (by list order) get priority.
            ``None`` = no cap.

    Backward-compat return shape (unchanged for callers):
        ``{active_tasks, active_contracts, active_projects, recent_commits}``
    """
    # Keep the legacy call path working by delegating to the
    # ContextCollector with a request shaped to match the old defaults.
    from work_buddy.context import (
        ContextCollector,
        ContextRequest,
    )

    states = task_states or ["inbox", "mit", "focused"]
    req = ContextRequest(
        sources=["tasks", "projects", "git"],
        window_days=1,  # matches the old "--since=24.hours.ago" behavior
        custom={
            "tasks": {"states": states},
        },
    )
    try:
        ctx = ContextCollector().collect(req)
    except Exception as exc:
        logger.debug("build_triage_context: collector failed: %s", exc)
        return {
            "active_tasks": [],
            "active_contracts": [],
            "active_projects": [],
            "recent_commits": [],
        }

    active_tasks = []
    tasks_section = ctx.section("tasks")
    if tasks_section:
        active_tasks = [dict(t) for t in (tasks_section.items or [])]
        if max_tasks is not None and len(active_tasks) > max_tasks:
            active_tasks = active_tasks[:max_tasks]

    active_projects: list[dict[str, Any]] = []
    active_contracts: list[dict[str, Any]] = []
    projects_section = ctx.section("projects")
    if projects_section:
        for item in projects_section.items or []:
            kind = item.get("type")
            if kind == "project":
                active_projects.append({
                    "slug": item.get("slug", ""),
                    "name": item.get("name", item.get("slug", "")),
                    "status": item.get("status", ""),
                    "description": item.get("description", "") or "",
                })
            elif kind == "contract":
                active_contracts.append({
                    "title": item.get("title", ""),
                    "status": item.get("status", ""),
                    "deadline": item.get("deadline", ""),
                    "claim": item.get("claim", ""),
                })

    recent_commits: list[str] = []
    git_section = ctx.section("git")
    if git_section:
        # Legacy format was a list of "<short> <subject>" strings — one
        # per line of `git log --oneline`. Rebuild that shape from the
        # structured items so prompt renderers that don't use the new
        # curator still work unchanged.
        for commit in git_section.items or []:
            short = commit.get("short") or ""
            subject = commit.get("subject") or ""
            if short or subject:
                recent_commits.append(f"{short} {subject}".strip())

    return {
        "active_tasks": active_tasks,
        "active_contracts": active_contracts,
        "active_projects": active_projects,
        "recent_commits": recent_commits,
    }


def render_triage_context_block(context: dict[str, Any]) -> str:
    """Render the ``build_triage_context`` output as a prompt block.

    Reusable across the Chrome cluster-level intent-group prompt and
    per-item triage prompts (e.g., ``journal_triage_scan``). Factored
    out of ``_build_user_prompt`` so any triage pass that wants the
    "here's what the user is actively working on" registry can get
    it without rebuilding the rendering logic.

    Returns a string starting with ``## User's Current Context`` and
    sections for active tasks / contracts / projects / recent
    commits. Empty sections are omitted. Returns an empty string
    when nothing is active.
    """
    lines: list[str] = ["## User's Current Context\n"]
    started_with = len(lines)

    tasks = context.get("active_tasks", [])
    if tasks:
        lines.append(f"### Active Tasks ({len(tasks)})")
        for t in tasks:
            lines.append(f"- [{t['task_id']}] ({t['state']}) {t['text']}")
        lines.append("")

    contracts = context.get("active_contracts", [])
    if contracts:
        lines.append(f"### Active Contracts ({len(contracts)})")
        for c in contracts:
            deadline = f" (deadline: {c['deadline']})" if c["deadline"] else ""
            lines.append(f"- {c['title']}{deadline}")
            if c["claim"]:
                lines.append(f"  Claim: {c['claim']}")
        lines.append("")

    projects = context.get("active_projects", [])
    if projects:
        lines.append(f"### Active Projects ({len(projects)})")
        for p in projects:
            desc = f" — {p['description']}" if p["description"] else ""
            lines.append(f"- {p['slug']}{desc}")
        lines.append("")

    commits = context.get("recent_commits", [])
    if commits:
        lines.append(f"### Recent Commits ({len(commits)})")
        for c in commits[:10]:
            lines.append(f"- {c}")
        lines.append("")

    if len(lines) == started_with:
        # Header line only; no sections populated. Return empty so
        # the caller can cleanly skip the block.
        return ""
    return "\n".join(lines)


def _build_user_prompt(
    clusters: list[TriageCluster],
    summaries: dict[str, dict],
    context: dict[str, Any],
) -> str:
    """Build the user prompt for Sonnet intent grouping."""
    lines = []

    # Context section — delegated to the shared renderer so journal's
    # per-item prompt gets the same shape.
    ctx_block = render_triage_context_block(context)
    if ctx_block:
        lines.append(ctx_block)

    # Tab clusters section
    total_tabs = sum(c.size for c in clusters)
    lines.append(f"## Tab Clusters ({len(clusters)} clusters, {total_tabs} tabs)\n")

    for cluster in clusters:
        lines.append(f"### Cluster {cluster.cluster_id}: {cluster.label}")
        lines.append(f"Cohesion: {cluster.cohesion:.2f} | Items: {cluster.size}")
        lines.append("")

        for item in cluster.items:
            url = item.url or ""
            engagement = item.metadata.get("score", 0)
            posture = item.metadata.get("user_posture", "")

            lines.append(f"**{item.label}**")
            if url:
                lines.append(f"  URL: {url}")
            lines.append(f"  Engagement: {engagement}")

            # Include Haiku summary if available
            sd = summaries.get(url) or item.metadata.get("summary_data")
            if sd:
                lines.append(f"  Summary: {sd.get('content_summary', '')}")
                if sd.get("user_intent_speculation"):
                    lines.append(f"  Intent speculation: {sd['user_intent_speculation']}")
                if posture:
                    lines.append(f"  User posture: {posture}")
                entities = sd.get("entities", [])
                if entities:
                    ent_str = ", ".join(
                        f"{e['name']} ({e['type']})" for e in entities[:5]
                    )
                    lines.append(f"  Entities: {ent_str}")
            lines.append("")

    return "\n".join(lines)


def _build_output_schema() -> dict[str, Any]:
    """Raw JSON Schema for Sonnet's structured intent grouping output.

    Passed to run_task(output_schema=...) which wraps it in the
    Anthropic API's output_config format automatically.
    """
    return {
        "type": "object",
        "properties": {
            "intent_groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "Clear description of the inferred intent",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "tab_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "TriageItem IDs belonging to this intent",
                        },
                        "likely_task_id": {
                            "type": "string",
                            "description": "Existing task ID if intent matches a tracked task, empty string if none",
                        },
                        "ambiguities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Things that can't be resolved without environment access",
                        },
                        "suggested_action": {
                            "type": "string",
                            "enum": list(TRIAGE_ACTIONS),
                        },
                        "action_detail": {
                            "type": "string",
                            "description": "Explanation of the suggested action",
                        },
                    },
                    "required": [
                        "intent", "confidence", "tab_ids",
                        "likely_task_id", "ambiguities",
                        "suggested_action", "action_detail",
                    ],
                    "additionalProperties": False,
                },
            },
            "uncategorized_tabs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tab IDs that don't clearly belong to any intent",
            },
            "overall_narrative": {
                "type": "string",
                "description": "1-3 sentence summary of the user's active work streams",
            },
        },
        "required": ["intent_groups", "uncategorized_tabs", "overall_narrative"],
        "additionalProperties": False,
    }


# ── Contextualize (programmatic — no LLM) ──────────────────────


def contextualize_intents(
    intent_data: dict[str, Any],
) -> dict[str, Any]:
    """Enrich Sonnet's intent groups with task state and activity context.

    Purely programmatic — no LLM calls.  For each intent group:
    1. If a task is referenced, look up its current state and recent history
    2. Add a ``context`` field with grounding information
    3. Revise ``suggested_action`` when context contradicts the suggestion
       (e.g., task was recently completed → change from record_into_task to close)

    Args:
        intent_data: Output of group_intents (or group_intents_from_raw).

    Returns:
        Same structure with added ``context`` and possibly revised actions.
    """
    groups = intent_data.get("intent_groups", [])
    if not groups:
        return intent_data

    # Load task state + history
    task_info = _load_task_info()

    # Load recent activity timeline (compact)
    activity = _load_recent_activity()

    revised_count = 0
    for group in groups:
        task_id = group.get("likely_task_id", "")
        context_parts: list[str] = []

        # Task state context
        if task_id and task_id in task_info:
            ti = task_info[task_id]
            state = ti["state"]
            text = ti["text"]
            context_parts.append(f"Task [{task_id}] is currently '{state}': {text}")

            # State history
            history = ti.get("history", [])
            if history:
                recent = history[:3]
                transitions = [
                    f"{h['old_state']}→{h['new_state']} ({h['changed_at'][:10]})"
                    for h in recent
                ]
                context_parts.append(f"Recent transitions: {', '.join(transitions)}")

            # Revise action if task is done/archived
            if state == "done" and group.get("suggested_action") == "record_into_task":
                group["suggested_action"] = "create_task"
                group["action_detail"] = (
                    f"[REVISED] Task {task_id} is already done. "
                    f"Consider creating a new task if this work is ongoing. "
                    f"Original suggestion was record_into_task."
                )
                group["ambiguities"].append(
                    f"Task {task_id} was marked done — is this a continuation or new work?"
                )
                revised_count += 1

            # Note if task is stale
            updated = ti.get("updated_at", "")
            if updated and updated < _days_ago(14):
                context_parts.append(
                    f"Task hasn't been updated since {updated[:10]} — may be stale"
                )

        elif task_id:
            context_parts.append(f"Task [{task_id}] not found in store — may be archived or invalid")

        # Activity context — check if any recent activity mentions this intent
        if activity:
            related = _find_related_activity(group.get("intent", ""), activity)
            if related:
                context_parts.append(f"Recent activity: {related}")

        group["context"] = " | ".join(context_parts) if context_parts else ""

    intent_data["contextualized"] = True
    intent_data["revisions"] = revised_count

    logger.info(
        "Contextualized %d intent groups (%d revised)",
        len(groups), revised_count,
    )

    return intent_data


def _load_task_info() -> dict[str, dict[str, Any]]:
    """Load task metadata + state history for all active tasks."""
    try:
        from work_buddy.obsidian.tasks import store as task_store
        from work_buddy.clarify.task_match import _read_task_texts

        task_texts = _read_task_texts()
        result: dict[str, dict[str, Any]] = {}

        for task in task_store.query(include_archived=False):
            tid = task["task_id"]
            history = task_store.get_history(tid)
            result[tid] = {
                "state": task["state"],
                "text": task_texts.get(tid, ""),
                "urgency": task.get("urgency", ""),
                "contract": task.get("contract", ""),
                "updated_at": task.get("updated_at", ""),
                "created_at": task.get("created_at", ""),
                "completed_at": task.get("completed_at", ""),
                "history": history,
            }

        # Also check recently completed tasks (last 7 days)
        for task in task_store.query(state="done"):
            tid = task["task_id"]
            if tid not in result:
                result[tid] = {
                    "state": "done",
                    "text": task_texts.get(tid, ""),
                    "completed_at": task.get("completed_at", ""),
                    "history": task_store.get_history(tid),
                }

        return result
    except Exception as e:
        logger.debug("Could not load task info: %s", e)
        return {}


def _load_recent_activity() -> list[str]:
    """Load compact recent activity timeline (last 12h)."""
    try:
        from work_buddy.activity import infer_activity

        result = infer_activity(since="12h")
        events = result.get("events", [])
        return [
            f"{e.get('time', '?')}: {e.get('description', '')}"
            for e in events[:20]
        ]
    except Exception as e:
        logger.debug("Could not load activity timeline: %s", e)
        return []


def _find_related_activity(intent: str, activity: list[str]) -> str:
    """Find activity entries that seem related to an intent (keyword match)."""
    # Extract key words from intent (lowercase, 4+ chars)
    words = {w.lower() for w in intent.split() if len(w) >= 4}
    # Filter stopwords
    words -= {"that", "this", "with", "from", "into", "about", "been", "have",
              "which", "they", "their", "some", "will", "would", "could", "should",
              "being", "were", "more", "also", "very", "likely", "possibly"}

    if not words:
        return ""

    matches = []
    for entry in activity:
        entry_lower = entry.lower()
        if any(w in entry_lower for w in words):
            matches.append(entry)

    if matches:
        return "; ".join(matches[:3])
    return ""


def _days_ago(n: int) -> str:
    """ISO date string for N days ago."""
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=n)).isoformat()


# ── auto_run entry points ───────────────────────────────────────


def contextualize_intents_from_raw(
    intent_data: dict[str, Any],
) -> dict[str, Any]:
    """Auto_run entry point for contextualization.

    Expects the output of group_intents_from_raw.
    """
    return contextualize_intents(intent_data)


def group_intents_from_raw(
    clusters_data: dict[str, Any],
    lens: str = "intent",
    data_type: str = "chrome",
) -> dict[str, Any]:
    """Auto_run entry point for Sonnet grouping.

    Expects the output of enrich_items_with_summaries (with clusters,
    singletons, and summaries).

    Args:
        clusters_data: Output from the summarize step.
        lens: "intent" or "topic".
        data_type: "chrome", "document", "journal", "conversation".
    """
    all_cluster_dicts = (
        clusters_data.get("clusters", [])
        + clusters_data.get("singletons", [])
    )
    clusters = [TriageCluster.from_dict(d) for d in all_cluster_dicts]
    summaries = clusters_data.get("summaries", {})

    result = group_intents(clusters, summaries=summaries, lens=lens, data_type=data_type)

    return {
        "success": not result.get("error"),
        "intent_groups": result.get("intent_groups", []),
        "uncategorized_tabs": result.get("uncategorized_tabs", []),
        "overall_narrative": result.get("overall_narrative", ""),
        "tokens": result.get("tokens", {}),
        # Pass through for the main model
        "clusters": clusters_data.get("clusters", []),
        "singletons": clusters_data.get("singletons", []),
        "item_count": clusters_data.get("item_count", 0),
    }
