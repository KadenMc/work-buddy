"""Target-level applicability for Co-work review proposals.

Proposal base hashes are immutable lineage.  They are useful evidence about the
version an agent saw, but a whole-document hash is not an application veto: an
unrelated edit may move a proposal while leaving its original passage intact.
This module binds the latest canonical Markdown projection to the current Y.Doc
checkpoint and resolves the proposal's immutable quote selector without
guessing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.anchors import CompositeSelector, locate_all, reanchor
from work_buddy.truth.contracts import AnchorError
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.store import DocumentRecord, TruthStore


ApplicabilityStatus = Literal["applicable", "target_changed", "unknown"]
_PROJECTION_BOUND_VERSION_KINDS = frozenset(
    {"initial_import", "materialized", "reimported", "repaired"}
)


@dataclass(frozen=True, slots=True)
class CurrentProjection:
    """One canonical Markdown projection bound to the live Y.Doc state."""

    text: str
    projection_sha256: str
    structured_head_sha256: str
    snapshot_sha256: str
    generation_sha256: str
    binding_id: str


@dataclass(frozen=True, slots=True)
class ProposalApplicability:
    """Whether a proposal's original passage is safely actionable now."""

    status: ApplicabilityStatus
    reason: str
    resolved_start: int | None = None
    resolved_end: int | None = None
    current_projection_sha256: str | None = None
    current_structured_head_sha256: str | None = None

    @property
    def applicable(self) -> bool:
        return self.status == "applicable"

    def to_wire(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "status": self.status,
            "reason": self.reason,
        }
        if self.resolved_start is not None:
            value["resolved_start"] = self.resolved_start
        if self.resolved_end is not None:
            value["resolved_end"] = self.resolved_end
        if self.current_projection_sha256 is not None:
            value["current_projection_sha256"] = self.current_projection_sha256
        if self.current_structured_head_sha256 is not None:
            value["current_structured_head_sha256"] = (
                self.current_structured_head_sha256
            )
        return value


def load_current_projection(
    store: TruthStore,
    document: DocumentRecord,
    *,
    structured_head_sha256: str | None,
    conn: sqlite3.Connection | None = None,
) -> tuple[CurrentProjection | None, str]:
    """Load a projection that matches every live Y.Doc identity.

    A client compaction receipt is the normal proof. A document version is an
    equivalent fallback when its projection, snapshot, and structured head are
    exactly the current atomically committed tuple. Returning a typed
    unavailable reason lets Review avoid inventing a stale claim when neither
    proof exists.
    """

    snapshot = document.ydoc_snapshot_sha256
    if snapshot is None or structured_head_sha256 is None:
        return None, "structured_document_unavailable"
    receipt = ydoc_store.current_projection_receipt(
        store,
        document_id=document.id,
    )
    generation = documents.current_ydoc_generation(
        store,
        document.id,
        conn=conn,
    )
    receipt_matches = (
        receipt is not None
        and receipt.document_id == document.id
        and receipt.ydoc_snapshot_sha256 == snapshot
        and receipt.structured_head_sha256 == structured_head_sha256
        and receipt.ydoc_generation_sha256 == generation
    )
    if receipt_matches:
        projection_sha256 = receipt.projection_sha256
        binding_id = receipt.id
    else:
        version = documents.current_document_version(
            store,
            document.id,
            conn=conn,
        )
        if (
            version is None
            or version.kind not in _PROJECTION_BOUND_VERSION_KINDS
            or version.projection_sha256 != document.content_sha256
            or version.ydoc_snapshot_sha256 != snapshot
            or version.structured_head_sha256 != structured_head_sha256
        ):
            return None, (
                "projection_receipt_unavailable"
                if receipt is None
                else "projection_receipt_outdated"
            )
        projection_sha256 = version.projection_sha256
        binding_id = version.id
    path = store.resolve_blob_path(f"blobs/{projection_sha256}")
    if not path.is_file():
        return None, "projection_blob_unavailable"
    try:
        raw = path.read_bytes()
    except OSError:
        return None, "projection_blob_unavailable"
    if sha256_bytes(raw) != projection_sha256:
        return None, "projection_blob_invalid"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "projection_blob_invalid"
    return (
        CurrentProjection(
            text=text,
            projection_sha256=projection_sha256,
            structured_head_sha256=structured_head_sha256,
            snapshot_sha256=snapshot,
            generation_sha256=generation,
            binding_id=binding_id,
        ),
        "available",
    )


def assess_proposal_applicability(
    proposal: Any,
    document: DocumentRecord,
    *,
    structured_head_sha256: str | None,
    current_projection: CurrentProjection | None,
    projection_unavailable_reason: str = "projection_unavailable",
) -> ProposalApplicability:
    """Assess the proposal's target against the current canonical projection.

    A matching structured head is a complete same-document proof. Proposals
    without a structured head retain their materialized-baseline compatibility.
    Otherwise the original quote must resolve uniquely in the state-bound
    current projection.
    """

    proposal_head = proposal.base_structured_head_sha256
    if (
        proposal_head is not None
        and structured_head_sha256 is not None
        and proposal_head == structured_head_sha256
    ):
        return ProposalApplicability(
            status="applicable",
            reason="same_structured_head",
            current_structured_head_sha256=structured_head_sha256,
        )
    if proposal_head is None and proposal.base_content_sha256 == document.content_sha256:
        return ProposalApplicability(
            status="applicable",
            reason="same_materialized_baseline",
            current_structured_head_sha256=structured_head_sha256,
        )
    if current_projection is None:
        return ProposalApplicability(
            status="unknown",
            reason=projection_unavailable_reason,
            current_structured_head_sha256=structured_head_sha256,
        )

    selector = CompositeSelector.from_json(proposal.selector_json)
    candidates = locate_all(current_projection.text, selector.exact)
    if not candidates:
        return ProposalApplicability(
            status="target_changed",
            reason="target_missing",
            current_projection_sha256=current_projection.projection_sha256,
            current_structured_head_sha256=structured_head_sha256,
        )
    try:
        resolved = reanchor(current_projection.text, selector)
    except AnchorError:
        return ProposalApplicability(
            status="target_changed",
            reason="target_ambiguous",
            current_projection_sha256=current_projection.projection_sha256,
            current_structured_head_sha256=structured_head_sha256,
        )
    return ProposalApplicability(
        status="applicable",
        reason=(
            "same_projection"
            if proposal.base_content_sha256 == current_projection.projection_sha256
            else "reanchored"
        ),
        resolved_start=resolved.start,
        resolved_end=resolved.end,
        current_projection_sha256=current_projection.projection_sha256,
        current_structured_head_sha256=structured_head_sha256,
    )


__all__ = [
    "CurrentProjection",
    "ProposalApplicability",
    "assess_proposal_applicability",
    "load_current_projection",
]
