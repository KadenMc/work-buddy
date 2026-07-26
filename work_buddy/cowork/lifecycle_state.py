"""Shared read-only lifecycle state used by drift, reimport, and retirement."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from work_buddy.cowork.paths import resolve_markdown_path
from work_buddy.cowork.readiness import classify_document
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.store import DocumentRecord, DocumentVersionRecord, TruthStore


_MATERIALIZED_KINDS = frozenset(
    {"initial_import", "repaired", "materialized", "reimported"}
)


@dataclass(frozen=True, slots=True)
class LifecycleState:
    document: DocumentRecord
    initialization_state: str
    file_path: Path
    current_file_sha256: str | None
    structured_head_sha256: str | None
    update_tail_present: bool
    materialized_version: DocumentVersionRecord | None
    baseline_available: bool

    @property
    def unmaterialized_structured_edits(self) -> bool:
        baseline = self.materialized_version
        if baseline is None or self.structured_head_sha256 is None:
            return True
        return (
            self.update_tail_present
            or baseline.structured_head_sha256 != self.structured_head_sha256
            or baseline.ydoc_snapshot_sha256 != self.document.ydoc_snapshot_sha256
        )

    @property
    def drift_state(self) -> str:
        if self.current_file_sha256 is None:
            return "missing"
        if self.current_file_sha256 == self.document.content_sha256:
            return "clean"
        return "drifted"

    @property
    def clean_materialized(self) -> bool:
        return (
            self.initialization_state == "ready"
            and self.drift_state == "clean"
            and not self.unmaterialized_structured_edits
        )


def materialized_version(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> DocumentVersionRecord | None:
    for version in reversed(documents.document_versions(store, document_id, conn=conn)):
        if version.kind in _MATERIALIZED_KINDS:
            return version
    return None


def inspect_lifecycle_state(
    store: TruthStore,
    document: DocumentRecord,
) -> LifecycleState:
    readiness = classify_document(store, document)
    resolved = resolve_markdown_path(store, document.path)
    current_hash = None
    if resolved.path.is_file():
        current_hash = sha256_bytes(resolved.path.read_bytes())
    tail = False
    head = None
    if document.ydoc_snapshot_sha256 is not None:
        tail = ydoc_store.update_tail_present(store, document_id=document.id)
        try:
            head = ydoc_store.current_structured_head(
                store,
                document_id=document.id,
                snapshot_sha256=document.ydoc_snapshot_sha256,
            )
        except ydoc_store.CompactionRecoveryRequired:
            head = None
    baseline_path = store.resolve_blob_path(f"blobs/{document.content_sha256}")
    baseline_available = False
    if baseline_path.is_file():
        baseline = baseline_path.read_bytes()
        baseline_available = sha256_bytes(baseline) == document.content_sha256
    return LifecycleState(
        document=document,
        initialization_state=readiness.initialization_state,
        file_path=resolved.path,
        current_file_sha256=current_hash,
        structured_head_sha256=head,
        update_tail_present=tail,
        materialized_version=materialized_version(store, document.id),
        baseline_available=baseline_available,
    )


__all__ = [
    "LifecycleState",
    "inspect_lifecycle_state",
    "materialized_version",
]
