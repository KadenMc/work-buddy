"""Truthful backend readiness projection for Co-work documents.

Python deliberately treats Y.Doc bytes as opaque.  It can prove pointer/blob
presence and digest integrity; semantic Yjs validation remains a browser
hydration responsibility and must never be fabricated here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from work_buddy.truth import documents, ydoc_store
from work_buddy.cowork.policy import document_surface_denial_reason
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.store import DocumentRecord, TruthStore


@dataclass(frozen=True, slots=True)
class DocumentReadiness:
    initialization_state: str
    snapshot_sha256: str | None
    structured_head_sha256: str | None
    projection_sha256: str
    projection_blob_available: bool
    disabled_reason: str | None
    permissions: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "initialization_state": self.initialization_state,
            "snapshot_sha256": self.snapshot_sha256,
            "structured_head_sha256": self.structured_head_sha256,
            "projection_sha256": self.projection_sha256,
            "projection_blob_available": self.projection_blob_available,
            "disabled_reason": self.disabled_reason,
            "permissions": dict(self.permissions),
        }


def _projection_blob_available(store: TruthStore, digest: str) -> bool:
    path = store.resolve_blob_path(f"blobs/{digest}")
    if not path.is_file():
        return False
    try:
        return sha256_bytes(path.read_bytes()) == digest
    except OSError:
        return False


def _repair_source_matches_projection(document: DocumentRecord) -> bool:
    """Whether the legacy repair workflow can reproduce this managed copy."""

    if not documents.source_is_detached(document):
        return True
    return (
        documents.retained_file_import_source_sha256(document.meta_json)
        == document.content_sha256
    )


def classify_document(
    store: TruthStore,
    document: DocumentRecord,
    *,
    read_only: bool = False,
    semantic_corrupt: bool = False,
    conn: sqlite3.Connection | None = None,
) -> DocumentReadiness:
    """Classify one document without writing files, rows, or runtime state."""

    lifecycle = documents.current_lifecycle(store, document.id, conn=conn)
    projection_available = _projection_blob_available(
        store, document.content_sha256
    )
    snapshot = document.ydoc_snapshot_sha256
    head: str | None = None
    reason: str | None = None

    if semantic_corrupt:
        state = "semantic_corrupt"
        reason = "semantic_corrupt"
    elif snapshot is None:
        if ydoc_store.update_tail_present(store, document_id=document.id):
            state = "updates_without_snapshot"
            reason = "updates_without_snapshot"
        else:
            state = "bootstrap_required"
            reason = "bootstrap_required"
    else:
        try:
            # read_snapshot verifies the content address. current_structured_head
            # additionally validates every opaque runtime frame.
            ydoc_store.read_snapshot(store, snapshot_sha256=snapshot)
            head = ydoc_store.current_structured_head(
                store,
                document_id=document.id,
                snapshot_sha256=snapshot,
            )
        except ydoc_store.CompactionRecoveryRequired:
            state = "recovery_required"
            reason = "compaction_recovery_required"
        except Exception:  # noqa: BLE001 - typed catalog state, never a 500
            state = "corrupt"
            reason = "snapshot_missing_or_corrupt"
        else:
            state = "ready"

    active = lifecycle != "retired"
    policy_reason = document_surface_denial_reason(store, document)
    allowed = policy_reason is None
    openable = state == "ready" and active and allowed
    if lifecycle == "retired":
        reason = "retired"
    elif policy_reason is not None:
        reason = policy_reason

    permissions = {
        "open": openable,
        "edit": openable and not read_only,
        "materialize": (
            openable
            and not read_only
            and not documents.source_is_detached(document)
        ),
        "repair": (
            state == "bootstrap_required"
            and active
            and allowed
            and not read_only
            and _repair_source_matches_projection(document)
        ),
        "retire": active and allowed and not read_only,
    }
    return DocumentReadiness(
        initialization_state=state,
        snapshot_sha256=snapshot,
        structured_head_sha256=head,
        projection_sha256=document.content_sha256,
        projection_blob_available=projection_available,
        disabled_reason=reason,
        permissions=permissions,
    )


__all__ = ["DocumentReadiness", "classify_document"]
