"""Canonical Markdown projection of a structured document at its current head.

Python treats Y.Doc state as opaque bytes. The packaged DOM-free worker owns the
ProseMirror/Yjs schema and the Markdown mapping, so producing text means handing
the compacted snapshot plus every un-compacted update to that worker and reading
back what it serializes.

The result is the *canonical projection*: the same text the browser's own
serializer produces, and the text space in which proposal anchors, expression
marks, and content hashes resolve. Callers that need publication-shaped output
convert from this, never instead of it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.runtime_service import shared_document_kernel
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.store import TruthStore


@dataclass(frozen=True, slots=True)
class DocumentProjection:
    """One document's canonical Markdown, bound to the head it came from."""

    markdown: str
    structured_head_sha256: str
    projection_sha256: str

    @property
    def byte_length(self) -> int:
        return len(self.markdown.encode("utf-8"))


def project_document(
    store: TruthStore,
    document_id: str,
    *,
    kernel: DocumentKernelClient | None = None,
) -> DocumentProjection:
    """Serialize the document's current head to canonical Markdown.

    ``ydoc_store.current_structured_head`` is the head authority rather than a
    bare hash over whatever the three reads happened to observe. Compaction
    rotates the update log separately from the snapshot pointer, so a head
    computed from an unchecked pair can name state that no longer exists. The
    authority raises on an in-flight compaction marker instead, which turns a
    silently stale projection into a retryable failure.
    """

    document = documents.get_document(store, document_id)
    if document.ydoc_snapshot_sha256 is None:
        raise InvariantViolation("document has no structured snapshot to project")

    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    snapshot = ydoc_store.read_snapshot(
        store,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    updates, _cursor = ydoc_store.read_updates(store, document_id=document.id)

    client = kernel or shared_document_kernel()
    outcome = client.request(
        {
            "kind": "project_markdown",
            "snapshotBase64": snapshot,
            "updatesBase64": updates,
            "expectedBaseStructuredHeadSha256": head,
        },
        request_id=f"project_{hashlib.sha256(f'{document.id}:{head}'.encode()).hexdigest()[:24]}",
    )
    projection = outcome.projection
    if projection is None:
        raise InvariantViolation("document kernel returned no projection")

    return DocumentProjection(
        markdown=projection.decode("utf-8"),
        structured_head_sha256=head,
        projection_sha256=hashlib.sha256(projection).hexdigest(),
    )
