"""VaultChunkPartition — vault markdown chunks as a consolidated-index partition.

Domain-owned (F-PLACEMENT): reuses ``vault_index``'s ``FilesystemSource`` (multi-vault
discovery) + ``chunk_markdown`` (heading-aware chunker) as collaborators — it does NOT
re-implement them. One Document per chunk; a ``content`` PASSAGE projection from the
chunk's breadcrumb-prefixed ``embed_input``.

Best-effort for the overnight build: registered + smoke-tested against a tmp file, but
NOT part of tonight's A/B (the vault's own index is the live path and is unaffected).
See AFK-DECISIONS D12.
"""

from __future__ import annotations

from typing import Any, Iterable

from work_buddy.index.model import (
    Document,
    ItemRef,
    Projection,
    ProjectionKind,
    ProjectionSpec,
    make_doc_id,
)
from work_buddy.index.partition import register_partition
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_PARTITION = "vault"


class VaultChunkPartition:
    name = _PARTITION
    change_key = "mtime"

    def __init__(self, source: Any = None) -> None:
        self._source = source  # inject for tests; else FilesystemSource() lazily
        self._files: dict[str, Any] = {}  # item_id -> DiscoveredFile (populated by discover)

    def _get_source(self):
        if self._source is None:
            from work_buddy.vault_index.source import FilesystemSource

            self._source = FilesystemSource()
        return self._source

    def field_weights(self) -> dict[str, float]:
        return {"name": 2.0, "body": 1.0}

    def projection_schema(self) -> dict[str, ProjectionSpec]:
        return {"content": ProjectionSpec(kind=ProjectionKind.PASSAGE)}

    def discover(self) -> Iterable[ItemRef]:
        result = self._get_source().discover()
        files = result[0] if isinstance(result, tuple) else result
        self._files = {f.item_id: f for f in files}
        return [ItemRef(item_id=f.item_id, mtime=float(getattr(f, "mtime", 0.0))) for f in files]

    def parse(self, item_id: str) -> list[Document]:
        df = self._files.get(item_id)
        if df is None:
            return []
        try:
            chunks = self._get_source().parse(item_id)
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("vault parse: cannot parse %s: %s", df.abs_path, exc)
            return []

        docs: list[Document] = []
        for chunk in chunks:
            crumb = " > ".join(chunk.heading_path) if chunk.heading_path else df.item_id
            docs.append(Document(
                doc_id=make_doc_id(_PARTITION, chunk.key),
                partition=_PARTITION,
                fields={"name": crumb, "body": chunk.text},
                display_text=chunk.text[:300],
                metadata={
                    "source_path": chunk.source_path,
                    "heading_path": chunk.heading_path,
                    "vault_id": getattr(df, "vault_id", ""),
                    "line_start": chunk.line_start,
                },
                projections={"content": Projection(text=chunk.embed_input)},
            ))
        return docs

    def search_build_evidence(self, index_store: Any) -> dict[str, object]:
        """Return the post-reconciliation authority-detachment receipt.

        ``IndexPartition`` invokes this only after the build commit.  Consequently
        ``indexed_excluded_items == 0`` proves that the first refresh after a seal
        removed old archive documents as well as fencing future discovery.
        """

        source = self._get_source()
        reporter = getattr(source, "authority_detachment_evidence", None)
        if reporter is None:
            return {
                "schema": "wb.legacy-root-detachment-evidence/v1",
                "detached_roots": 0,
                "detachment_active": False,
                "indexed_excluded_items": 0,
                "archive_discovery_fenced": False,
            }
        item_ids = index_store.partition_item_ids(self.name)
        return reporter(item_ids)


register_partition(_PARTITION, lambda: VaultChunkPartition())
