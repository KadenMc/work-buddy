"""Small end-to-end Running Note source-backed document pilot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlencode

from work_buddy.document_kernel.causality import ProjectionCursor
from work_buddy.document_kernel.domain_service import (
    BoundDocumentChange,
    RunningNoteDocumentService,
)
from work_buddy.document_kernel.journal_projection import JournalProjectionWorker
from work_buddy.sources import ReservedResolution, SourceStore


@dataclass(frozen=True, slots=True)
class RunningNotePilotResult:
    document: BoundDocumentChange
    projection: ProjectionCursor

    @property
    def cowork_href(self) -> str:
        return "/app/cowork?" + urlencode(
            {
                "store_id": self.document.binding.store_id,
                "document_id": self.document.binding.document_id,
                "change_id": self.document.change.change_id,
            }
        )

    def inspection(self) -> dict[str, object]:
        """Content-minimized inspection projection for an existing UI action."""

        change = self.document.change
        binding = self.document.binding
        return {
            "schema": "cowork-running-note-pilot/v1",
            "coworkHref": self.cowork_href,
            "binding": {
                "bindingId": binding.binding_id,
                "domainNamespace": binding.domain_namespace,
                "domainKind": binding.domain_kind,
                "domainEntityId": binding.domain_entity_id,
                "storeId": binding.store_id,
                "documentId": binding.document_id,
                "contentAuthority": binding.content_authority,
                "contentAuthorityEpoch": binding.content_authority_epoch,
            },
            "change": {
                "changeId": change.change_id,
                "operationKind": change.operation_kind,
                "sourceRef": change.source_ref,
                "sourceRepresentationId": change.source_representation_id,
                "sourceContentSha256": change.source_content_sha256,
                "exactCopiedTextSha256": change.exact_copied_text_sha256,
                "baseStructuredHeadSha256": change.base_structured_head_sha256,
                "resultStructuredHeadSha256": change.result_structured_head_sha256,
                "assurance": json.loads(change.assurance_json),
                "actors": json.loads(change.actors_json),
                "protocolVersion": change.protocol_version,
                "runtimeVersion": change.runtime_version,
                "schemaVersion": change.schema_version,
            },
            "journalProjection": {
                "status": self.projection.status,
                "documentHeadSha256": self.projection.document_head_sha256,
                "sectionSha256": self.projection.section_sha256,
                "divergenceSourceRef": self.projection.divergence_source_ref,
            },
        }


class RunningNotePilotService:
    """Materialize, cut over, and project one already-reserved Running Note."""

    def __init__(
        self,
        *,
        documents: RunningNoteDocumentService,
        projections: JournalProjectionWorker,
    ) -> None:
        self.documents = documents
        self.projections = projections

    def execute(
        self,
        *,
        vault_root: str | Path,
        entry_id: str,
        day_id: str,
        domain_revision: str,
        source_store: SourceStore,
        reserved_source: ReservedResolution,
        actors: Mapping[str, str | None],
        idempotency_key: str,
        expected_initial_text: str,
        projection_path: str | None = None,
    ) -> RunningNotePilotResult:
        change = self.documents.materialize(
            vault_root=vault_root,
            entry_id=entry_id,
            day_id=day_id,
            domain_revision=domain_revision,
            source_store=source_store,
            reserved_source=reserved_source,
            actors=actors,
            idempotency_key=idempotency_key,
            projection_path=projection_path,
        )
        cursor = self.projections.project(
            change.store,
            binding=change.binding,
            entry_id=entry_id,
            expected_initial_text=expected_initial_text,
        )
        return RunningNotePilotResult(change, cursor)


__all__ = ["RunningNotePilotResult", "RunningNotePilotService"]
