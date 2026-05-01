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

    # Chrome-only setup: a fresh tabs snapshot is needed to detect stale
    # tabs before close/group calls. Sources that don't have tabs (journal,
    # conversation, …) skip this — it pulls in the Chrome collector which
    # may be unavailable, and the resulting ``current_tabs`` would be
    # unused anyway.
    source = presentation.get("source") or "unknown"
    is_chrome = source == "chrome"
    current_tabs = _get_current_tabs() if is_chrome else {}

    results = {
        "closed": [],
        "tasks_created": [],
        "tasks_recorded": [],
        "grouped": [],
        "left": [],
        "skipped_stale": [],
        "errors": [],
        # Slice 3 buckets: separate from the legacy task buckets so
        # callers can tell a multi-record execution apart from a
        # legacy create_task. Slices 6/10 will wire executors for
        # references_filed / calendar_added; today they're recorded
        # as "logged only" so the user sees what would have happened.
        "records_executed": [],
        "references_logged": [],
        "calendar_logged": [],
        "deleted": [],
    }

    # Slice 3 multi-record execution: any group whose presentation
    # carries ``records: [...]`` runs through the per-record router
    # below instead of the legacy action ops loop. The router still
    # marks reviewed via the existing pool stamp path because it
    # populates the same ``tasks_created`` / ``tasks_recorded`` /
    # ``deleted`` buckets (with item_ids), and the dashboard's
    # ``api_review_execute`` filter walks all buckets.
    multi_record_decisions, legacy_decisions = _split_decisions_by_shape(
        group_decisions, presentation,
    )
    if multi_record_decisions:
        _execute_multi_record_decisions(
            multi_record_decisions, item_lookup, presentation, results,
            source=source,
        )

    # Flatten: expand item overrides into per-item effective actions
    # (legacy path only — multi-record decisions handled above).
    ops = _plan_operations(legacy_decisions, item_lookup, presentation)

    # Group operations by action for batch execution
    close_ops = [op for op in ops if op["action"] == "close"]
    group_ops = [op for op in ops if op["action"] == "group"]
    create_ops = [op for op in ops if op["action"] == "create_task"]
    record_ops = [op for op in ops if op["action"] == "record_into_task"]
    leave_ops = [op for op in ops if op["action"] == "leave"]

    # 1. Close tabs (batch — one Chrome API call). Source-gated: only
    #    Chrome has tabs to close. For other sources the action doesn't
    #    make sense; any close ops that slip through here (e.g. a
    #    mislabelled override) get recorded as an error rather than
    #    silently dispatched to a no-op.
    if close_ops:
        if is_chrome:
            _execute_close_batch(close_ops, item_lookup, current_tabs, results)
            time.sleep(_OP_DELAY)
        else:
            for op in close_ops:
                results["errors"].append({
                    "op": op,
                    "error": f"'close' action is Chrome-only; source={source!r}",
                })

    # 2. Group tabs (one call per group). Same source gate as close.
    if group_ops:
        if is_chrome:
            _execute_group_batch(group_ops, item_lookup, current_tabs, results, presentation)
            time.sleep(_OP_DELAY)
        else:
            for op in group_ops:
                results["errors"].append({
                    "op": op,
                    "error": f"'group' action is Chrome-only; source={source!r}",
                })

    # 3. Create tasks (Python only, no Chrome calls)
    for op in create_ops:
        try:
            _execute_create_task(op, item_lookup, results, source=source)
        except Exception as e:
            results["errors"].append({"op": op, "error": str(e)})
            logger.error("create_task failed for group %s: %s", op.get("group_index"), e)

    # 4. Record into tasks (Python only)
    for op in record_ops:
        try:
            _execute_record_into_task(op, item_lookup, results, source=source)
        except Exception as e:
            results["errors"].append({"op": op, "error": str(e)})
            logger.error("record_into_task failed for group %s: %s", op.get("group_index"), e)

    # 5. Leave — just log
    for op in leave_ops:
        for item_id in op.get("item_ids", []):
            meta = item_lookup.get(item_id, {})
            results["left"].append({"item_id": item_id, "label": meta.get("label", item_id)})

    summary = {
        "total_operations": len(ops) + len(multi_record_decisions),
        "closed": len(results["closed"]),
        "tasks_created": len(results["tasks_created"]),
        "tasks_recorded": len(results["tasks_recorded"]),
        "grouped": len(results["grouped"]),
        "left": len(results["left"]),
        "skipped_stale": len(results["skipped_stale"]),
        "errors": len(results["errors"]),
        # Slice 3 summary fields
        "records_executed": len(results["records_executed"]),
        "references_logged": len(results["references_logged"]),
        "calendar_logged": len(results["calendar_logged"]),
        "deleted": len(results["deleted"]),
        "details": results,
    }

    logger.info(
        "Triage executed: %d closed, %d tasks created, %d recorded, "
        "%d grouped, %d left, %d stale, %d errors, "
        "%d records executed, %d references logged, %d calendar logged, "
        "%d deleted",
        summary["closed"], summary["tasks_created"],
        summary["tasks_recorded"], summary["grouped"],
        summary["left"], summary["skipped_stale"], summary["errors"],
        summary["records_executed"], summary["references_logged"],
        summary["calendar_logged"], summary["deleted"],
    )

    return summary


# ── Slice 3: multi-record execution ────────────────────────────


def _split_decisions_by_shape(
    group_decisions: list[dict],
    presentation: dict,
) -> tuple[list[dict], list[dict]]:
    """Partition decisions into multi-record vs legacy shape.

    A decision targets a multi-record group when the corresponding
    presentation_group carries a non-empty ``records: [...]`` field.
    Empty records arrays go through the legacy path (treated as
    ``leave`` — there's nothing to execute).
    """
    # Build group-by-index lookup once; presentation has groups_by_action
    # keyed by legacy action strings, but groups themselves carry the
    # records field on the same presentation_group dict.
    groups_by_index: dict[int, dict] = {}
    for action_groups in presentation.get("groups_by_action", {}).values():
        for g in action_groups:
            idx = g.get("index")
            if isinstance(idx, int):
                groups_by_index[idx] = g

    multi: list[dict] = []
    legacy: list[dict] = []
    for gd in group_decisions:
        idx = gd.get("group_index")
        group = groups_by_index.get(idx) if isinstance(idx, int) else None
        records = (group or {}).get("records") if isinstance(group, dict) else None
        if isinstance(records, list) and records:
            multi.append(gd)
        else:
            legacy.append(gd)
    return multi, legacy


def _execute_multi_record_decisions(
    decisions: list[dict],
    item_lookup: dict,
    presentation: dict,
    results: dict,
    *,
    source: str = "unknown",
) -> None:
    """Run Slice 3 records[] for each multi-record group decision.

    Per-record routing:

    - ``destination=task`` → create_task OR record_into_task depending
      on whether the record's task_proposal carries ``target_task_id``.
      Slice 2 metadata fields (kind, outcome_text, definition_of_done,
      creation_effort, user_involvement, has_deadline / deadline_date /
      has_dependency / dependency_hint) are forwarded to ``create_task``
      via its kwargs when populated. Defaults preserve current behavior
      when fields are absent.
    - ``destination=delete`` → record the delete_reason in results
      (no vault mutation; the pool entry is marked reviewed by the
      existing executor success-filter path).
    - ``destination=reference`` → log only. Slice 6 wires actual
      reference filing.
    - ``destination=calendar_only`` → log only. Slice 10 wires
      calendar destinations.

    User overrides apply at the GROUP level: if the user picked
    ``action='leave'`` or ``action='close'`` on the whole group, the
    records are skipped (no-op for leave; treated as a coarse delete
    for close — record the override but don't run individual records).
    """
    groups_by_index: dict[int, dict] = {}
    for action_groups in presentation.get("groups_by_action", {}).values():
        for g in action_groups:
            idx = g.get("index")
            if isinstance(idx, int):
                groups_by_index[idx] = g

    for gd in decisions:
        gidx = gd.get("group_index")
        group = groups_by_index.get(gidx) if isinstance(gidx, int) else None
        if not group:
            continue
        records = group.get("records") or []
        item_ids = [it.get("id") for it in (group.get("items") or []) if it.get("id")]

        # User-level override: leave skips everything; close treats
        # the whole group as a coarse delete (records ignored, just
        # logged so the user can see what was skipped).
        user_action = gd.get("action", "")
        if user_action == "leave":
            for iid in item_ids:
                meta = item_lookup.get(iid, {})
                results["left"].append({"item_id": iid, "label": meta.get("label", iid)})
            continue
        if user_action == "close":
            for iid in item_ids:
                results["deleted"].append({
                    "item_id": iid,
                    "reason": "user_overrode_to_close",
                    "records_skipped": len(records),
                })
            continue

        # Default path: execute each record per its destination.
        record_outcomes: list[dict[str, Any]] = []
        for rec_idx, rec in enumerate(records):
            dest = rec.get("destination")
            try:
                outcome = _execute_record(
                    rec=rec, group=group, item_ids=item_ids,
                    item_lookup=item_lookup, results=results,
                    source=source,
                    record_index=rec_idx,
                )
                record_outcomes.append(outcome)
            except Exception as exc:
                logger.error(
                    "execute_record %s/%s failed: %s",
                    gidx, rec_idx, exc,
                )
                results["errors"].append({
                    "action": f"record:{dest}",
                    "group_index": gidx,
                    "record_index": rec_idx,
                    "error": str(exc),
                })

        results["records_executed"].append({
            "group_index": gidx,
            "item_ids": item_ids,
            "outcomes": record_outcomes,
            "n_records": len(records),
        })


def _execute_record(
    *,
    rec: dict[str, Any],
    group: dict[str, Any],
    item_ids: list[str],
    item_lookup: dict[str, Any],
    results: dict[str, Any],
    source: str,
    record_index: int,
) -> dict[str, Any]:
    """Route one Slice 3 record to its destination handler.

    Returns a per-record outcome dict so the caller can attach a
    record-by-record audit trail to the group-level result.
    """
    dest = rec.get("destination")
    if dest == "task":
        return _execute_record_task(
            rec, group, item_ids, item_lookup, results, source=source,
        )
    if dest == "delete":
        reason = rec.get("delete_reason") or "agent_marked_delete"
        for iid in item_ids:
            results["deleted"].append({
                "item_id": iid,
                "reason": reason,
                "record_index": record_index,
            })
        return {"destination": "delete", "reason": reason}
    if dest == "reference":
        # Slice 6 wires reference filing. Log here so the user can
        # see what would have been filed.
        proposal = rec.get("reference_proposal") or {}
        for iid in item_ids:
            results["references_logged"].append({
                "item_id": iid,
                "summary": proposal.get("summary", ""),
                "suggested_path": proposal.get("suggested_path"),
                "record_index": record_index,
            })
        return {
            "destination": "reference",
            "logged_only": True,
            "summary": proposal.get("summary", ""),
        }
    if dest == "calendar_only":
        proposal = rec.get("calendar_proposal") or {}
        for iid in item_ids:
            results["calendar_logged"].append({
                "item_id": iid,
                "title": proposal.get("title", ""),
                "datetime": proposal.get("datetime"),
                "record_index": record_index,
            })
        return {
            "destination": "calendar_only",
            "logged_only": True,
            "title": proposal.get("title", ""),
        }
    # Unknown destination — should have been caught by the schema /
    # pool validation, but be defensive.
    return {"destination": dest, "skipped": True, "reason": "unknown_destination"}


def _execute_record_task(
    rec: dict[str, Any],
    group: dict[str, Any],
    item_ids: list[str],
    item_lookup: dict[str, Any],
    results: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    """Run one ``destination=task`` record.

    Forks on ``task_proposal.target_task_id``:
    - present and non-empty → record_into_task path
    - absent → create_task path with the Slice 2 metadata forwarded
      to ``tasks_create`` as kwargs.
    """
    proposal = rec.get("task_proposal") or {}
    target = proposal.get("target_task_id") or ""
    suggested = proposal.get("suggested_task_text") or ""

    # Build the Source Items section (same shape as the legacy path
    # for visual consistency in the task note).
    url_lines: list[str] = []
    for iid in item_ids:
        meta = item_lookup.get(iid, {})
        url = meta.get("url", "")
        label = meta.get("label", iid)
        if url:
            url_lines.append(f"- [{label}]({url})")
        else:
            url_lines.append(f"- {label}")

    if target:
        # record_into_task
        header_by_source = {
            "chrome": "\n## Related Tabs (from Clarify record)\n\n",
            "journal": "\n## Related Notes (from Clarify record)\n\n",
            "inline": "\n## Source Selection (from Clarify record)\n\n",
        }
        header = header_by_source.get(source, "\n## Related Items (from Clarify record)\n\n")
        context = header + "\n".join(url_lines)
        results["tasks_recorded"].append({
            "target_task_id": target,
            "context": context,
            "item_ids": item_ids,
            "source_record_destination": "task",
        })
        return {
            "destination": "task",
            "mode": "record_into_task",
            "target_task_id": target,
        }

    # create_task
    if not suggested:
        # Fall back to group_intent → first-item-label, mirroring the
        # legacy behavior.
        suggested = group.get("intent") or "Triage: " + ", ".join(
            item_lookup.get(iid, {}).get("label", iid) for iid in item_ids[:3]
        )

    header_by_source = {
        "chrome": "## Source Tabs",
        "journal": "## Source Notes",
        "inline": "## Source Selection",
    }
    header = header_by_source.get(source, "## Source Items")
    note_content = header + "\n\n" + "\n".join(url_lines) if url_lines else ""

    # Forward Slice 2 metadata to tasks_create when populated.
    create_kwargs: dict[str, Any] = {
        "task_text": suggested,
        "urgency": "medium",
        "summary": note_content if note_content else None,
    }
    namespace_tags = group.get("suggested_namespace_tags") or []
    if namespace_tags:
        create_kwargs["tags"] = list(namespace_tags)

    # Slice 2 fields — forwarded only when present so legacy callers
    # of tasks_create don't see surprise None values. The schema
    # field ``kind`` maps to tasks_create's ``task_kind`` kwarg
    # (the rest of the names match identically).
    proposal_to_kwarg = {
        "kind": "task_kind",
        "outcome_text": "outcome_text",
        "next_action_text": "next_action_text",
        "definition_of_done": "definition_of_done",
        "creation_effort": "creation_effort",
        "user_involvement": "user_involvement",
        "creation_provenance": "creation_provenance",
        "has_deadline": "has_deadline",
        "deadline_date": "deadline_date",
        "has_dependency": "has_dependency",
        "dependency_hint": "dependency_hint",
    }
    for proposal_field, kwarg in proposal_to_kwarg.items():
        if proposal_field in proposal and proposal[proposal_field] is not None:
            create_kwargs[kwarg] = proposal[proposal_field]

    # Slice 4: serialize the verdict's risk_profile dict into the
    # JSON-blob column.  parse_risk_profile handles unknown ladder
    # values + missing keys via clamp-to-safe; ``to_json`` produces a
    # canonical sorted-key form so two equal profiles round-trip
    # bytewise (helps test fixtures and audit comparisons).
    rp_dict = proposal.get("risk_profile")
    if isinstance(rp_dict, dict):
        try:
            from work_buddy.automation.risk import parse_risk_profile
            create_kwargs["risk_profile_json"] = parse_risk_profile(
                rp_dict,
            ).to_json()
        except Exception:  # pragma: no cover — defensive
            logger.warning(
                "execute_clarify: failed to serialize risk_profile for task=%r",
                suggested[:60],
            )

    # Slice 5a: serialize the verdict's two action-context lists.  The
    # contexts module canonicalizes (deduplicates, drops non-strings)
    # and JSON-encodes; storing None preserves NULL columns for legacy
    # rows.  The source defaults to "agent_inferred" when either list
    # is populated by the LLM.
    agent_ctx = proposal.get("agent_required_contexts")
    user_ctx = proposal.get("user_required_contexts")
    if agent_ctx is not None or user_ctx is not None:
        try:
            from work_buddy.automation.contexts import serialize_context_list
            if agent_ctx is not None:
                create_kwargs["agent_required_contexts"] = (
                    serialize_context_list(agent_ctx)
                )
            if user_ctx is not None:
                create_kwargs["user_required_contexts"] = (
                    serialize_context_list(user_ctx)
                )
            source = proposal.get("required_contexts_source")
            if source is None:
                source = "agent_inferred"
            create_kwargs["required_contexts_source"] = source
        except Exception:  # pragma: no cover — defensive
            logger.warning(
                "execute_clarify: failed to serialize required_contexts for task=%r",
                suggested[:60],
            )

    try:
        from work_buddy.obsidian.tasks.mutations import create_task
        result = create_task(**create_kwargs)
        results["tasks_created"].append({
            "task_text": suggested,
            "task_id": result.get("task_id", ""),
            "item_ids": item_ids,
            "namespace_tags": list(namespace_tags),
            "source_record_destination": "task",
            "task_proposal": proposal,
        })
        return {
            "destination": "task",
            "mode": "create_task",
            "task_id": result.get("task_id", ""),
            "task_text": suggested,
        }
    except TypeError:
        # tasks_create may not yet accept the Slice 2 kwargs (e.g.,
        # during a partial Slice 2 migration). Retry without them so
        # the task still lands; record a non-fatal warning.
        logger.warning(
            "create_task rejected Slice 2 kwargs; retrying with legacy shape "
            "(task_text=%r)", suggested[:60],
        )
        from work_buddy.obsidian.tasks.mutations import create_task
        legacy_kwargs = {
            k: v for k, v in create_kwargs.items()
            if k in {"task_text", "urgency", "summary", "tags"}
        }
        result = create_task(**legacy_kwargs)
        results["tasks_created"].append({
            "task_text": suggested,
            "task_id": result.get("task_id", ""),
            "item_ids": item_ids,
            "namespace_tags": list(namespace_tags),
            "source_record_destination": "task",
            "slice_2_kwargs_dropped": True,
        })
        return {
            "destination": "task",
            "mode": "create_task",
            "task_id": result.get("task_id", ""),
            "task_text": suggested,
            "slice_2_kwargs_dropped": True,
        }


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
    source: str = "chrome",
) -> None:
    """Create a new task from a triage group.

    ``source`` drives the note-header wording: Chrome items produce a
    ``## Source Tabs`` section with URL links; journal items produce a
    ``## Source Notes`` section with the thread's leading label. Other
    sources fall back to a generic ``## Source Items``.
    """
    gd = op.get("group_decision", {})
    task_text = gd.get("new_task_text", "")
    intent = gd.get("intent", "")
    item_ids = op.get("item_ids", [])

    if not task_text:
        task_text = intent or "Triage: " + ", ".join(
            item_lookup.get(iid, {}).get("label", iid) for iid in item_ids[:3]
        )

    # Build note content. Chrome gets Markdown links to URLs; journal
    # has no URLs, so we render the thread label only.
    url_lines = []
    for iid in item_ids:
        meta = item_lookup.get(iid, {})
        url = meta.get("url", "")
        label = meta.get("label", iid)
        if url:
            url_lines.append(f"- [{label}]({url})")
        else:
            url_lines.append(f"- {label}")

    header_by_source = {
        "chrome": "## Source Tabs",
        "journal": "## Source Notes",
        "inline": "## Source Selection",
    }
    header = header_by_source.get(source, "## Source Items")
    note_content = header + "\n\n" + "\n".join(url_lines) if url_lines else ""

    # Include override reason if provided
    reason = gd.get("override_reason", "")
    if reason:
        note_content = f"**Reason:** {reason}\n\n" + note_content

    # Namespace tags (optional) travel with the group decision.
    # Validated by create_task itself via _normalize_tags.
    namespace_tags = gd.get("namespace_tags") or []

    try:
        from work_buddy.obsidian.tasks.mutations import create_task
        create_kwargs: dict[str, Any] = {
            "task_text": task_text,
            "urgency": "medium",
            "summary": note_content if note_content else None,
        }
        if namespace_tags:
            create_kwargs["tags"] = list(namespace_tags)
        result = create_task(**create_kwargs)
        results["tasks_created"].append({
            "task_text": task_text,
            "task_id": result.get("task_id", ""),
            "item_ids": item_ids,
            "namespace_tags": list(namespace_tags),
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
    source: str = "chrome",
) -> None:
    """Record items into an existing task's note.

    ``source`` drives the appended-section header, matching the
    Chrome/journal/other split used by ``_execute_create_task``.
    """
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

    header_by_source = {
        "chrome": "\n## Related Tabs (from triage)\n\n",
        "journal": "\n## Related Notes (from journal triage)\n\n",
        "inline": "\n## Source Selection (from inline triage)\n\n",
    }
    header = header_by_source.get(source, "\n## Related Items (from triage)\n\n")
    context = header + "\n".join(url_lines)

    # Namespace tags (optional): if the user set tags in the Review UI,
    # replace the target task's namespace tags in-line. Preserves #todo,
    # #projects/*, wikilinks, and plugin emojis (see _rewrite_namespace_tags).
    namespace_tags = gd.get("namespace_tags") or []
    tag_update: dict[str, Any] | None = None
    if namespace_tags:
        try:
            from work_buddy.obsidian.tasks.mutations import set_task_tags_on_line
            tag_result = set_task_tags_on_line(target_task_id, list(namespace_tags))
            tag_update = {
                "success": bool(tag_result.get("success")),
                "tags": list(namespace_tags),
            }
            if not tag_result.get("success"):
                tag_update["error"] = tag_result.get("message", "unknown")
        except Exception as e:
            logger.warning(
                "record_into_task: tag rewrite failed for %s: %s",
                target_task_id, e,
            )
            tag_update = {"success": False, "tags": list(namespace_tags), "error": str(e)}

    entry: dict[str, Any] = {
        "target_task_id": target_task_id,
        "context": context,
        "item_ids": item_ids,
    }
    if tag_update is not None:
        entry["tag_update"] = tag_update
    results["tasks_recorded"].append(entry)


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
