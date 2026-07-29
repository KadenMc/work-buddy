"""Idempotent document events for completed Verify and Co-think jobs."""

from __future__ import annotations

from typing import Any, Mapping

from work_buddy.cowork.execution_identity import CoworkVerifyRole
from work_buddy.cowork.verify_runtime import VerifyRuntimeJob
from work_buddy.truth.events import emit_truth_event


def emit_verify_completion_event(
    job: VerifyRuntimeJob | None,
    result: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Publish the same completion invalidation for worker and recovery paths."""

    if job is None or result.get("status") != "completed":
        return None
    event_type = "truth.doc_verify_job_completed"
    if job.role is CoworkVerifyRole.COTHINK:
        event_type = (
            "truth.doc_cothink_item_added"
            if result.get("cothink_item_id")
            else "truth.doc_cothink_outcome_recorded"
        )
    event = emit_truth_event(
        event_type,
        store_id=job.store_id,
        event_id=(
            f"cowork-verify-job:{job.job_id}:"
            f"{result.get('output_sha256') or 'completed'}"
        ),
        data={
            "document_id": job.document_id,
            "job_id": job.job_id,
            "role": job.role.value,
            "run_id": job.evaluation_run_id,
            **(
                {
                    "outcome": (
                        "perspective"
                        if result.get("cothink_item_id")
                        else "none"
                    ),
                    "item_id": result.get("cothink_item_id"),
                }
                if job.role is CoworkVerifyRole.COTHINK
                else {}
            ),
        },
    )
    return event.to_dict()


__all__ = ["emit_verify_completion_event"]
