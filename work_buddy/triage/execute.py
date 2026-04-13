"""Batch-execute triage decisions — close tabs, create tasks, organize groups.

Designed to run as an auto_run code step in the triage workflow.
Processes all decisions sequentially with:
- Sleeps between Chrome operations (prevent extension overload)
- Graceful per-operation failure (one failure doesn't abort the rest)
- Stale-tab detection (skip tabs whose URL changed since triage)

The execute step receives:
- ``decisions``: The Phase 2 review response (group_decisions + reassignments)
- ``presentation``: The original presentation dict (item metadata, tab_ids, URLs)
"""

from __future__ import annotations

import time
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Delay between Chrome extension operations (seconds)
_OP_DELAY = 1.0


def execute_triage_decisions(
    decisions: dict[str, Any],
    presentation: dict[str, Any],
) -> dict[str, Any]:
    """Execute all triage decisions in batch.

    Args:
        decisions: Phase 2 response with ``group_decisions`` and ``reassignments``.
        presentation: Original presentation dict for item metadata.

    Returns:
        Summary with per-action counts and details.
    """
    group_decisions = decisions.get("group_decisions", [])

    # Build item lookup: item_id → {label, url, tab_id, summary}
    item_lookup = _build_item_lookup(presentation)

    # Take a fresh snapshot to detect stale tabs
    current_tabs = _get_current_tabs()

    results = {
        "closed": [],
        "tasks_created": [],
        "tasks_recorded": [],
        "grouped": [],
        "left": [],
        "skipped_stale": [],
        "errors": [],
    }

    # Flatten: expand item overrides into per-item effective actions
    ops = _plan_operations(group_decisions, item_lookup, presentation)

    # Group operations by action for batch execution
    close_ops = [op for op in ops if op["action"] == "close"]
    group_ops = [op for op in ops if op["action"] == "group"]
    create_ops = [op for op in ops if op["action"] == "create_task"]
    record_ops = [op for op in ops if op["action"] == "record_into_task"]
    leave_ops = [op for op in ops if op["action"] == "leave"]

    # 1. Close tabs (batch — one Chrome API call)
    if close_ops:
        _execute_close_batch(close_ops, item_lookup, current_tabs, results)
        time.sleep(_OP_DELAY)

    # 2. Group tabs (one call per group)
    if group_ops:
        _execute_group_batch(group_ops, item_lookup, current_tabs, results, presentation)
        time.sleep(_OP_DELAY)

    # 3. Create tasks (Python only, no Chrome calls)
    for op in create_ops:
        try:
            _execute_create_task(op, item_lookup, results)
        except Exception as e:
            results["errors"].append({"op": op, "error": str(e)})
            logger.error("create_task failed for group %s: %s", op.get("group_index"), e)

    # 4. Record into tasks (Python only)
    for op in record_ops:
        try:
            _execute_record_into_task(op, item_lookup, results)
        except Exception as e:
            results["errors"].append({"op": op, "error": str(e)})
            logger.error("record_into_task failed for group %s: %s", op.get("group_index"), e)

    # 5. Leave — just log
    for op in leave_ops:
        for item_id in op.get("item_ids", []):
            meta = item_lookup.get(item_id, {})
            results["left"].append({"item_id": item_id, "label": meta.get("label", item_id)})

    summary = {
        "total_operations": len(ops),
        "closed": len(results["closed"]),
        "tasks_created": len(results["tasks_created"]),
        "tasks_recorded": len(results["tasks_recorded"]),
        "grouped": len(results["grouped"]),
        "left": len(results["left"]),
        "skipped_stale": len(results["skipped_stale"]),
        "errors": len(results["errors"]),
        "details": results,
    }

    logger.info(
        "Triage executed: %d closed, %d tasks created, %d recorded, "
        "%d grouped, %d left, %d stale, %d errors",
        summary["closed"], summary["tasks_created"],
        summary["tasks_recorded"], summary["grouped"],
        summary["left"], summary["skipped_stale"], summary["errors"],
    )

    return summary


# ── Operation Planning ──────────────────────────────────────────


def _plan_operations(
    group_decisions: list[dict],
    item_lookup: dict,
    presentation: dict,
) -> list[dict]:
    """Flatten group decisions + item overrides into a list of operations.

    Each operation is: {action, group_index, item_ids, metadata}
    Item overrides create separate operations per override action.
    """
    ops = []

    for gd in group_decisions:
        group_index = gd.get("group_index")
        group_action = gd.get("action", "leave")
        item_overrides = {o["item_id"]: o["action"] for o in gd.get("item_overrides", [])}

        # Get items for this group
        group_items = _get_group_items(group_index, gd, presentation)
        item_ids = [i["id"] for i in group_items]

        # Split by effective action
        by_action: dict[str, list[str]] = {}
        for iid in item_ids:
            effective = item_overrides.get(iid, group_action)
            by_action.setdefault(effective, []).append(iid)

        for action, ids in by_action.items():
            ops.append({
                "action": action,
                "group_index": group_index,
                "item_ids": ids,
                "group_decision": gd,
            })

    return ops


# ── Chrome Operations ───────────────────────────────────────────


def _get_current_tabs() -> dict[int, dict]:
    """Get a fresh snapshot of current tabs for stale detection.

    Returns: {tab_id: {url, title}} for all open tabs.
    """
    try:
        from work_buddy.collectors.chrome_collector import request_tabs
        snapshot = request_tabs(timeout_seconds=10)
        if snapshot and "tabs" in snapshot:
            return {
                t["tabId"]: {"url": t.get("url", ""), "title": t.get("title", "")}
                for t in snapshot["tabs"]
            }
    except Exception as e:
        logger.warning("Failed to get current tabs for stale detection: %s", e)
    return {}


def _check_stale(
    tab_id: int,
    expected_url: str,
    current_tabs: dict[int, dict],
) -> str | None:
    """Check if a tab is stale (URL changed since triage).

    Returns None if OK, or a reason string if stale.
    """
    if not current_tabs:
        return None  # Can't check, assume OK

    current = current_tabs.get(tab_id)
    if current is None:
        return "tab no longer exists"

    if expected_url and current["url"] != expected_url:
        return f"URL changed: {current['url'][:60]}"

    return None


def _filter_stale(
    item_ids: list[str],
    item_lookup: dict,
    current_tabs: dict[int, dict],
    results: dict,
) -> list[int]:
    """Filter item_ids to valid Chrome tab IDs, skipping stale ones.

    Returns list of valid tab IDs to operate on.
    """
    valid_tab_ids = []

    for iid in item_ids:
        meta = item_lookup.get(iid, {})
        tab_id = meta.get("tab_id")

        if not tab_id or not isinstance(tab_id, (int, float)):
            logger.debug("No tab_id for item %s, skipping Chrome op", iid)
            continue

        tab_id = int(tab_id)
        expected_url = meta.get("url", "")
        stale_reason = _check_stale(tab_id, expected_url, current_tabs)

        if stale_reason:
            results["skipped_stale"].append({
                "item_id": iid,
                "tab_id": tab_id,
                "reason": stale_reason,
            })
            logger.info("Skipping stale tab %d (%s): %s", tab_id, iid, stale_reason)
        else:
            valid_tab_ids.append(tab_id)

    return valid_tab_ids


def _execute_close_batch(
    ops: list[dict],
    item_lookup: dict,
    current_tabs: dict,
    results: dict,
) -> None:
    """Close all tabs from close operations in one batch."""
    all_item_ids = []
    for op in ops:
        all_item_ids.extend(op.get("item_ids", []))

    valid_tab_ids = _filter_stale(all_item_ids, item_lookup, current_tabs, results)

    if not valid_tab_ids:
        return

    try:
        from work_buddy.collectors.chrome_collector import close_tabs
        result = close_tabs(valid_tab_ids)

        if result and result.get("status") == "ok":
            details = result.get("details", {})
            for iid in all_item_ids:
                meta = item_lookup.get(iid, {})
                tab_id = meta.get("tab_id")
                if tab_id and int(tab_id) in (details.get("closed_ids") or []):
                    results["closed"].append({
                        "item_id": iid,
                        "tab_id": int(tab_id),
                        "label": meta.get("label", iid),
                    })
        else:
            logger.warning("close_tabs returned: %s", result)
            results["errors"].append({"action": "close", "error": str(result)})

    except Exception as e:
        logger.error("close_tabs failed: %s", e)
        results["errors"].append({"action": "close", "error": str(e)})


def _execute_group_batch(
    ops: list[dict],
    item_lookup: dict,
    current_tabs: dict,
    results: dict,
    presentation: dict | None = None,
) -> None:
    """Create Chrome tab groups — one per group operation."""
    for op in ops:
        item_ids = op.get("item_ids", [])
        valid_tab_ids = _filter_stale(item_ids, item_lookup, current_tabs, results)

        if not valid_tab_ids:
            continue

        gd = op.get("group_decision", {})
        group_index = op.get("group_index")
        # Intent lives in the presentation group, not the decision dict.
        intent = gd.get("intent", "")
        if not intent and group_index is not None and presentation:
            intent = _get_group_intent(group_index, presentation)
        title = intent[:50] if intent else "Triage Group"

        try:
            from work_buddy.collectors.chrome_collector import group_tabs
            result = group_tabs(valid_tab_ids, title=title, color="cyan")

            if result and result.get("status") == "ok":
                results["grouped"].append({
                    "title": title,
                    "group_id": result.get("details", {}).get("group_id"),
                    "tab_count": len(valid_tab_ids),
                    "item_ids": item_ids,
                })
            else:
                results["errors"].append({"action": "group", "error": str(result)})
        except Exception as e:
            results["errors"].append({"action": "group", "error": str(e)})

        time.sleep(_OP_DELAY)


# ── Task Operations ─────────────────────────────────────────────


def _execute_create_task(
    op: dict,
    item_lookup: dict,
    results: dict,
) -> None:
    """Create a new task from a triage group."""
    gd = op.get("group_decision", {})
    task_text = gd.get("new_task_text", "")
    intent = gd.get("intent", "")
    item_ids = op.get("item_ids", [])

    if not task_text:
        task_text = intent or "Triage: " + ", ".join(
            item_lookup.get(iid, {}).get("label", iid) for iid in item_ids[:3]
        )

    # Build note content with URLs
    url_lines = []
    for iid in item_ids:
        meta = item_lookup.get(iid, {})
        url = meta.get("url", "")
        label = meta.get("label", iid)
        if url:
            url_lines.append(f"- [{label}]({url})")
        else:
            url_lines.append(f"- {label}")

    note_content = "## Source Tabs\n\n" + "\n".join(url_lines) if url_lines else ""

    # Include override reason if provided
    reason = gd.get("override_reason", "")
    if reason:
        note_content = f"**Reason:** {reason}\n\n" + note_content

    try:
        from work_buddy.obsidian.tasks.mutations import create_task
        result = create_task(
            task_text=task_text,
            urgency="medium",
            summary=note_content if note_content else None,
        )
        results["tasks_created"].append({
            "task_text": task_text,
            "task_id": result.get("task_id", ""),
            "item_ids": item_ids,
        })
    except Exception as e:
        logger.error("Failed to create task '%s': %s", task_text[:40], e)
        results["errors"].append({
            "action": "create_task",
            "task_text": task_text,
            "error": str(e),
        })


def _execute_record_into_task(
    op: dict,
    item_lookup: dict,
    results: dict,
) -> None:
    """Record items into an existing task's note."""
    gd = op.get("group_decision", {})
    target_task_id = gd.get("target_task_id", "")
    item_ids = op.get("item_ids", [])

    if not target_task_id:
        results["errors"].append({
            "action": "record_into_task",
            "error": "No target_task_id",
            "item_ids": item_ids,
        })
        return

    # Build context to append
    url_lines = []
    for iid in item_ids:
        meta = item_lookup.get(iid, {})
        url = meta.get("url", "")
        label = meta.get("label", iid)
        if url:
            url_lines.append(f"- [{label}]({url})")
        else:
            url_lines.append(f"- {label}")

    context = "\n## Related Tabs (from triage)\n\n" + "\n".join(url_lines)

    results["tasks_recorded"].append({
        "target_task_id": target_task_id,
        "context": context,
        "item_ids": item_ids,
    })


# ── Helpers ─────────────────────────────────────────────────────


def _build_item_lookup(presentation: dict) -> dict[str, dict]:
    """Build lookup: item_id → {label, url, tab_id, summary}."""
    lookup = {}
    for groups in presentation.get("groups_by_action", {}).values():
        for group in groups:
            for item in group.get("items", []):
                lookup[item["id"]] = item
    return lookup


def _get_group_intent(group_index: int, presentation: dict) -> str:
    """Look up the intent string for a group from the presentation."""
    for groups in presentation.get("groups_by_action", {}).values():
        for group in groups:
            if group.get("index") == group_index:
                return group.get("intent", "")
    return ""


def _get_group_items(
    group_index: int,
    group_decision: dict,
    presentation: dict,
) -> list[dict]:
    """Get items for a group (original or user-created)."""
    if group_index < 0:
        # New group — items listed in decision
        item_ids = group_decision.get("items", [])
        item_lookup = _build_item_lookup(presentation)
        return [item_lookup.get(iid, {"id": iid, "label": iid}) for iid in item_ids]

    for groups in presentation.get("groups_by_action", {}).values():
        for group in groups:
            if group.get("index") == group_index:
                return group.get("items", [])

    return []


# ── Auto-run entry point ────────────────────────────────────────


def execute_from_raw(
    decisions: dict[str, Any],
    presentation: dict[str, Any],
) -> dict[str, Any]:
    """Auto_run entry point for the execute step.

    Accepts outputs from the dispatch-review and build-recommendations
    steps (which wrap their data in envelope dicts) and unwraps them
    before executing.

    Args:
        decisions: Output of dispatch_review step. Expected shape:
            ``{"decisions": {...}, "presentation": {...}, ...}``
            Falls back to using the dict directly if no ``decisions`` key.
        presentation: Output of build-recommendations reasoning step.
            May be wrapped as ``{"presentation": {...}}`` or bare dict.

    Returns:
        Execution summary dict.
    """
    _EMPTY = {
        "total_operations": 0, "closed": 0, "tasks_created": 0,
        "tasks_recorded": 0, "grouped": 0, "left": 0,
        "skipped_stale": 0, "errors": 0,
        "details": {k: [] for k in (
            "closed", "tasks_created", "tasks_recorded",
            "grouped", "left", "skipped_stale", "errors",
        )},
    }

    # Unwrap dispatch-review envelope
    actual_decisions = decisions.get("decisions", decisions)

    # Unwrap presentation envelope (reasoning step may wrap it)
    actual_presentation = presentation
    if "presentation" in presentation and isinstance(presentation.get("presentation"), dict):
        actual_presentation = presentation["presentation"]

    # Validate inputs — catch malformed data from upstream steps
    if not isinstance(actual_presentation, dict) or "groups_by_action" not in actual_presentation:
        logger.error(
            "execute_from_raw: invalid presentation. Keys: %s",
            list(actual_presentation.keys()) if isinstance(actual_presentation, dict) else type(actual_presentation).__name__,
        )
        return {**_EMPTY, "error": "Invalid presentation — missing groups_by_action"}

    if decisions.get("timeout"):
        logger.warning("execute_from_raw: dispatch-review timed out — no decisions to execute")
        return {**_EMPTY, "skipped_reason": "dispatch-review timed out, no user decisions"}

    return execute_triage_decisions(actual_decisions, actual_presentation)
