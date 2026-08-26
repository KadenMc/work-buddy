"""Bounded domain-only recovery for authored task proposals and their links.

This maintenance capability does not infer proposals or grant approval. Threads
may resume only its durable, human-approved execution intents; Journal may
replay only already-recorded proposal ingress and synchronize receipt links.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from work_buddy.mcp_server.op_registry import register_op

logger = logging.getLogger(__name__)


def _pause_code() -> str | None:
    from work_buddy.backups.source_foundation_restore import (
        SourceFoundationRestorePending,
        require_source_foundation_writable,
    )
    from work_buddy.config import load_config

    if load_config().get("dashboard", {}).get("read_only", False):
        return "dashboard_read_only"
    try:
        require_source_foundation_writable("task_proposals.reconcile")
    except SourceFoundationRestorePending as exc:
        return exc.code
    return None


def _reconcile_threads(limit: int) -> dict[str, Any]:
    from work_buddy.threads.action_proposals import get_action_proposal_service

    results = get_action_proposal_service().reconcile_pending(limit=limit)
    statuses = Counter(result["proposal"]["status"] for result in results)
    # Maintenance logs need counts, never task text, capture content, or origin.
    return {
        "ok": statuses["realized"] == len(results),
        "examined": len(results),
        "realized": statuses["realized"],
        "needs_attention": statuses["needs_attention"],
        "unavailable": statuses["unavailable"],
        "pending": len(results) - statuses["realized"],
    }


def _reconcile_journal(limit: int) -> dict[str, Any]:
    from work_buddy.journal_capture.proposal_maintenance import (
        reconcile_journal_task_proposals,
    )

    return reconcile_journal_task_proposals(limit=limit)


def task_proposals_reconcile(limit: int = 50) -> dict[str, Any]:
    """Resume at most ``limit`` entries in each existing authority (1–100).

    Threads runs first so Journal can publish realized references in this same
    pass. A failed stage does not strand the independent stage. Read-only and
    restore fencing are rechecked before each stage; neither is bypassed.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        return {
            "ok": False,
            "code": "invalid_limit",
            "message": "Use a whole-number reconciliation limit from 1 to 100.",
        }
    result: dict[str, Any] = {"ok": True, "limit": limit}
    for name, reconcile in (
        ("threads", _reconcile_threads),
        ("journal", _reconcile_journal),
    ):
        try:
            paused = _pause_code()
            if paused:
                result.update(ok=False, skipped=True, code=paused)
                result[name] = {"ok": False, "skipped": True, "code": paused}
                break
            stage = reconcile(limit)
            if not isinstance(stage, dict) or type(stage.get("ok")) is not bool:
                raise ValueError("Invalid reconciliation result")
            result[name] = stage
            if not stage["ok"]:
                result["ok"] = False
        except Exception as exc:  # noqa: BLE001 - isolate independent recovery stages
            logger.warning(
                "Task proposal maintenance stage %s deferred (%s)",
                name,
                type(exc).__name__,
            )
            result["ok"] = False
            result[name] = {"ok": False, "code": "reconciliation_unavailable"}
    return result


register_op("op.wb.task_proposals_reconcile", task_proposals_reconcile, replace=True)

__all__ = ["task_proposals_reconcile"]
