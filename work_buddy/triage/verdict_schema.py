"""Shared JSON schema for triage verdicts.

Both :mod:`inline_triage_scan` and :mod:`journal_triage_scan` ask Sonnet
for the same verdict shape via ``LLMRunner.call(output_schema=...)``.
Keeping the schema in one module avoids drift between the two callers
and matches the fields :func:`triage_submit` accepts.

The enum for ``recommended_action`` is sourced from
:data:`work_buddy.triage.items.TRIAGE_ACTIONS` so any future action
expansion lands in one place.
"""

from __future__ import annotations

from typing import Any

from work_buddy.triage.items import TRIAGE_ACTIONS


VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommended_action": {
            "type": "string",
            "enum": list(TRIAGE_ACTIONS),
            "description": (
                "One of: create_task, record_into_task, leave, close, group."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "One to three sentences explaining the decision.",
        },
        "group_intent": {
            "type": "string",
            "description": (
                "Short noun phrase (≤8 words) naming the underlying intent. "
                "Shown as the card title in the Review view."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0.0–1.0 self-assessed confidence.",
        },
        "suggested_task_text": {
            "type": "string",
            "description": (
                "Required when recommended_action == 'create_task'. "
                "A concise task title suitable for the master task list."
            ),
        },
        "target_task_id": {
            "type": "string",
            "description": (
                "Required when recommended_action == 'record_into_task'. "
                "Must match a task_id from the user's current-context block."
            ),
        },
        "related_item_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Required when recommended_action == 'group'. Other "
                "pool-item IDs this item clusters with."
            ),
        },
    },
    "required": ["recommended_action", "rationale", "group_intent"],
    "additionalProperties": False,
}


def verdict_to_submit_kwargs(verdict: dict[str, Any]) -> dict[str, Any]:
    """Filter a parsed verdict down to :func:`triage_submit`'s named kwargs.

    The schema allows a few action-specific optional fields
    (``suggested_task_text`` for create_task, ``target_task_id`` for
    record_into_task, ``related_item_ids`` for group). ``triage_submit``
    silently ignores unrecognized fields, but filtering here keeps
    pool entries tidy and protects against future schema drift.
    """
    allowed = {
        "recommended_action",
        "rationale",
        "group_intent",
        "confidence",
        "suggested_task_text",
        "target_task_id",
        "related_item_ids",
    }
    return {k: v for k, v in verdict.items() if k in allowed and v is not None}
