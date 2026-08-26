"""Domain-only composition for Journal's already-requested task proposals.

The scheduler calls this bounded recovery seam, not a dashboard view or a model.
It delivers the durable Journal outbox and acknowledges structured TaskStore
realization receipts without accepting any proposal on the user's behalf.
"""

from __future__ import annotations

from typing import Any

from work_buddy.backups.source_foundation_restore import source_foundation_read_only
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.store import JournalCaptureStore


def reconcile_journal_task_proposals(
    *, limit: int = 100, service: JournalCaptureService | None = None
) -> dict[str, Any]:
    """Resume local delivery only; never invoke Smart or accept a task proposal."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if source_foundation_read_only():
        return {"ok": False, "code": "source_foundation_read_only"}
    if service is None:
        from work_buddy.threads.action_proposals import get_action_proposal_service

        service = JournalCaptureService(
            JournalCaptureStore(),
            JournalContentAdapter(),
            proposal_service=get_action_proposal_service(),
        )
    result = service.reconcile_proposals(limit=limit)
    return {"ok": True, **(result or {})}


__all__ = ["reconcile_journal_task_proposals"]
