"""``triage_submit`` capability — record a local-agent verdict into the pool.

Exposed to background-triage agents via the ``triage_agent`` tool
preset. Safe to call from outside a live run: unknown ``run_id`` or
``item_id`` return a structured error and do nothing. That makes it
a real, reusable work-buddy capability rather than a synthetic
"emit_verdict" tool that only makes sense inside one loop.

The capability is intentionally narrow: it validates the run,
validates the payload shape, and writes one :class:`PoolEntry`.
All reasoning about what to do with that verdict happens later,
during the on-demand review.
"""

from __future__ import annotations

from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.triage.background import get_pool

logger = get_logger(__name__)


def triage_submit(
    *,
    run_id: str,
    item_id: str,
    recommended_action: str,
    rationale: str,
    group_intent: str | None = None,
    confidence: float | None = None,
    target_task_id: str | None = None,
    suggested_task_text: str | None = None,
    related_item_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Submit a triage verdict for one item of an active background run.

    Args:
        run_id: The producer-assigned run identifier. The agent
            receives this in its prompt.
        item_id: The id of the item this verdict applies to. Must
            belong to the named run.
        recommended_action: One of the five canonical
            :data:`TRIAGE_ACTIONS`: ``"close"``, ``"group"``,
            ``"create_task"``, ``"record_into_task"``, ``"leave"``.
            Not every action makes sense for every source — the
            reviewer can override during human triage.
        rationale: One-to-three-sentence explanation of why the
            agent chose this action. Persisted verbatim in the pool.
        group_intent: Short (≤8-word) noun-phrase naming the
            underlying *intent* behind the item, used as the group
            header in the review UI. Distinct from the action name
            (e.g. "ETF weekly tracking habit" or "work-buddy search
            feature design question"). Omit only when no meaningful
            abstraction exists; the review UI will fall back to a
            rationale excerpt.
        confidence: Optional [0,1] confidence score. The agent is
            free to omit it.
        target_task_id: For ``record_into_task``, the task to append
            into.
        suggested_task_text: For ``create_task``, the proposed task
            body.
        related_item_ids: Other item_ids from the same run that the
            agent believes belong to the same cluster. Used later to
            group recommendations in the review modal.

    Returns:
        ``{"status": "ok", ...}`` on accepted submission.
        ``{"status": "error", "error": ...}`` for any rejection
        (unknown run, wrong item, bad action, duplicate). Rejections
        are structured — the calling agent can retry with corrected
        arguments.
    """
    if not run_id or not isinstance(run_id, str):
        return {"status": "error", "error": "run_id must be a non-empty string."}
    if not item_id or not isinstance(item_id, str):
        return {"status": "error", "error": "item_id must be a non-empty string."}
    if not recommended_action or not isinstance(recommended_action, str):
        return {
            "status": "error",
            "error": "recommended_action must be a non-empty string.",
        }
    if not rationale or not isinstance(rationale, str):
        return {"status": "error", "error": "rationale must be a non-empty string."}

    verdict: dict[str, Any] = {
        "recommended_action": recommended_action,
        "rationale": rationale.strip(),
    }
    if group_intent and isinstance(group_intent, str):
        # Keep it short: agents occasionally duplicate the rationale
        # here. Truncate at a generous bound; the review UI only
        # shows the first ~80 chars anyway.
        verdict["group_intent"] = group_intent.strip()[:160]
    if confidence is not None:
        try:
            c = float(confidence)
        except (TypeError, ValueError):
            return {
                "status": "error",
                "error": "confidence must be a number between 0 and 1.",
            }
        if not (0.0 <= c <= 1.0):
            return {
                "status": "error",
                "error": "confidence must be between 0 and 1.",
            }
        verdict["confidence"] = c
    if target_task_id:
        verdict["target_task_id"] = str(target_task_id)
    if suggested_task_text:
        verdict["suggested_task_text"] = str(suggested_task_text)
    if related_item_ids:
        if not isinstance(related_item_ids, list):
            return {
                "status": "error",
                "error": "related_item_ids must be a list of strings.",
            }
        verdict["related_item_ids"] = [str(x) for x in related_item_ids]

    pool = get_pool()
    result = pool.submit(run_id=run_id, item_id=item_id, verdict=verdict)
    if result.get("status") != "ok":
        logger.info(
            "triage_submit rejected: run=%s item=%s reason=%s",
            run_id, item_id, result.get("error"),
        )
    return result
