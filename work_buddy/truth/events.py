"""Non-authoritative lifecycle events emitted after Truth commits."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from work_buddy.truth.identity import truth_uri


logger = logging.getLogger(__name__)

# This frozenset is the SINGLE SOURCE OF TRUTH for every truth.doc_* event
# name (C1). The surface SSE table mirrors these names verbatim and coins none
# of its own, because emit_truth_event raises ValueError for any name absent
# here, so a surface listener on an unpublished name is silently dead.
TRUTH_EVENT_TYPES = frozenset(
    {
        "truth.store_created",
        "truth.evidence_captured",
        "truth.span_marked",
        "truth.claim_proposed",
        "truth.claim_confirmed",
        "truth.claim_rejected",
        "truth.claim_challenged",
        "truth.claim_superseded",
        "truth.claim_redacted",
        "truth.sweep_completed",
        "truth.doc_registered",
        "truth.doc_imported",
        "truth.doc_materialized",
        "truth.doc_drift_detected",
        "truth.doc_reimported",
        "truth.doc_retired",
        "truth.doc_proposed",
        "truth.doc_proposal_decided",
        "truth.doc_proposal_applied",
        "truth.doc_proposal_expired",
        "truth.doc_expression_marked",
        "truth.doc_feedback_captured",
    }
)


@dataclass(frozen=True, slots=True)
class TruthEventEmission:
    """Best-effort publication result returned without changing Truth state."""

    event_id: str | None
    published: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "published": self.published,
            "error": self.error,
        }


def emit_truth_event(
    event_type: str,
    *,
    store_id: str,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> TruthEventEmission:
    """Publish one durable Truth event without becoming a write authority.

    Callers invoke this only after the ledger transaction commits. Publication
    failure is reported and logged, but it never changes or rolls back Truth.
    """

    if event_type not in TRUTH_EVENT_TYPES:
        raise ValueError(f"unsupported Truth event type: {event_type!r}")
    if (subject_kind is None) != (subject_id is None):
        raise ValueError("subject_kind and subject_id must be supplied together")
    subject = (
        None
        if subject_kind is None or subject_id is None
        else truth_uri(store_id, subject_kind, subject_id)
    )
    payload = {**dict(data or {}), "store_id": store_id}

    try:
        from work_buddy.events.dispatcher import publish
        from work_buddy.events.envelope import new_event

        event = new_event(
            f"/wb/truth/{store_id}",
            event_type,
            payload,
            durable=True,
            subject=subject,
            modality="internal",
        )
        publish(event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Truth event publication failed for %s: %s", event_type, exc)
        return TruthEventEmission(None, False, str(exc))
    return TruthEventEmission(event.id, True)
