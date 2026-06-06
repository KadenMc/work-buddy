"""Vault semantic index → :class:`Index`. One partition per vault.

Sources its counts from ``vault_index.status.index_status`` (the single source of
truth) so the capability, the ``/vault/index`` status path, and this adapter never
drift. The vault is one shared SQLite file across all vaults, so DB size is an
index-level figure (not per-partition).
"""
from __future__ import annotations

from typing import Callable

from work_buddy.indexing.protocol import (
    BuildProgress,
    BuildResult,
    IndexStatus,
    PartitionStatus,
)


class VaultIndexAdapter:
    name = "vault_index"

    def status(self) -> IndexStatus:
        from work_buddy.vault_index.status import index_status

        raw = index_status()
        if raw.get("status") != "ok":
            return IndexStatus(name=self.name, partitions=[])

        last_build = raw.get("last_build")
        parts: list[PartitionStatus] = []
        for vid, info in sorted(raw.get("vaults", {}).items()):
            vcount = info.get("vector_count", 0)
            pending = info.get("pending", 0)
            parts.append(
                PartitionStatus(
                    key=vid or "(unassigned)",
                    total_items=info.get("chunk_count", 0),
                    dense_eligible=vcount + pending,  # chunks with a non-blank embed_input
                    vector_count=vcount,
                    pending=pending,
                    last_build=last_build,
                    size_on_disk_mb=None,  # shared DB — reported at the index level
                    health=info.get("health", "ok"),
                    detail=None if info.get("in_config", True) else "not in config",
                )
            )
        return IndexStatus(
            name=self.name,
            partitions=parts,
            size_on_disk_mb=raw.get("size_on_disk_mb"),
        )

    def lock_key(self) -> str:
        from work_buddy.vault_index import store
        return str(store._db_path())

    def bulk_build(
        self,
        *,
        full_history: bool = False,
        on_progress: Callable[[BuildProgress], None] | None = None,
    ) -> BuildResult:
        from work_buddy.vault_index.indexer import build_all

        try:
            stats = build_all(force=full_history, encode=True)
            return BuildResult(name=self.name, ok=True, stats=stats)
        except Exception as exc:
            return BuildResult(
                name=self.name, ok=False, stats={},
                error=f"{type(exc).__name__}: {exc}",
            )
