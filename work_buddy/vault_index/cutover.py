"""Targeted pre-seal Vault-index detachment for cutover operators.

The caller names one or more known domains. Paths are resolved exclusively from
configuration; arbitrary filesystem paths are not accepted. The refresh mutates
only the derived consolidated index. It never writes authority or configuration.
"""

from __future__ import annotations

from typing import Any, Iterable


def refresh_vault_for_prospective_seal(
    domains: Iterable[str],
    *,
    cfg: dict[str, Any] | None = None,
    index_config: Any = None,
    index_store: Any = None,
    encoder: Any = None,
    residents: Any = None,
) -> dict[str, Any]:
    """Purge configured legacy roots from consolidated Vault search pre-seal.

    A crash before the corresponding authority seal is safe: ordinary legacy
    discovery may re-add the material, because the durable exclusion intentionally
    begins only at the seal. The operator should run this bounded refresh again,
    certify its receipt, and then seal without an intervening Vault refresh.  The
    ``encoder`` injection remains accepted for caller compatibility but is never
    invoked by this targeted prune.
    """

    from work_buddy.config import load_config
    from work_buddy.index.config import load_index_config
    from work_buddy.index.locking import index_writer_locks
    from work_buddy.index.partition import get_projection_schema
    from work_buddy.index.resident import get_registry
    from work_buddy.index.store import IndexStore
    from work_buddy.vault_index.authority_exclusions import (
        prospective_legacy_roots,
        sealed_legacy_roots,
    )
    from work_buddy.vault_index.partition import VaultChunkPartition
    from work_buddy.vault_index.source import FilesystemSource

    app_cfg = cfg if cfg is not None else load_config()
    requested = tuple(dict.fromkeys(str(domain) for domain in domains))
    if not requested:
        raise ValueError("at least one prospective legacy domain is required")
    prospective = prospective_legacy_roots(app_cfg, requested)
    sustained = sealed_legacy_roots(app_cfg, allow_default_data_root=cfg is None)
    exclusions = tuple(dict.fromkeys((*sustained, *prospective)))

    idx_cfg = index_config or load_index_config(app_cfg)
    if not idx_cfg.enabled:
        raise RuntimeError("consolidated index is disabled")
    store = index_store or IndexStore(idx_cfg.resolved_db_path())
    source = FilesystemSource(app_cfg, authority_exclusions=exclusions)
    partition = VaultChunkPartition(source=source)
    resident_registry = residents if residents is not None else get_registry()

    # This is deliberately not an ordinary build.  A full discovery/diff would
    # parse and encode unrelated Vault edits, making a bounded authority operation
    # depend on arbitrary workspace churn.  Classify only already-represented item
    # IDs, delete exact authority matches, and leave ordinary reconciliation state
    # (including last_build:vault) untouched.
    with index_writer_locks(store.db_path, partition.name):
        item_ids = store.partition_item_ids(partition.name)
        excluded_item_ids = source.authority_excluded_item_ids(item_ids)
        for item_id in excluded_item_ids:
            store.delete_item_docs(item_id, partition=partition.name)

        # Always advance the version, even on an empty replay.  A previous process
        # may have durably deleted every target and crashed before publishing the
        # version/cache boundary.
        store.bump_version(partition.name)
        for projection in get_projection_schema(partition):
            resident_registry.invalidate(f"{partition.name}:{projection}")

        result = {
            "partition": partition.name,
            "changed": 0,
            "deleted": len(excluded_item_ids),
            "docs_indexed": 0,
            "doc_count": store.doc_count(partition.name),
            "version": store.build_version(partition.name),
            "evidence": partition.search_build_evidence(store),
        }
    result["operator"] = {
        "prospective_domains": list(requested),
        "prospective_roots": len(prospective),
        "sustained_roots": len(sustained),
    }
    return result


__all__ = ["refresh_vault_for_prospective_seal"]
