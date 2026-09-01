"""Read-only certification of native search and legacy-root detachment.

Receipts are derived on demand from authority databases, the actual partition
registry, live partition discovery, durable outbox lag, and the consolidated
index ledger. Nothing is created, migrated, acknowledged, sealed, or written.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from work_buddy.index.partition import get_change_key
from work_buddy.vault_index.authority_exclusions import normalized_path


SEARCH_DOMAINS = ("journal", "projects", "contracts", "personal_knowledge")
_EXPECTED_AUTHORITIES = {
    "journal": "database_only",
    "projects": "sqlite",
    "contracts": "native",
    "personal_knowledge": "sqlite",
}


def _ro_connection(path: Path) -> sqlite3.Connection:
    for suffix in ("-wal", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.is_file() and sidecar.stat().st_size:
            raise RuntimeError(
                "cutover certification requires checkpointed SQLite state"
            )
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


class _ReadOnlyIndexLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        return _ro_connection(self.path)

    def available(self) -> bool:
        if not self.path.is_file():
            return False
        try:
            with self._connect() as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                    )
                }
            return {"documents", "indexed_items", "index_meta"} <= tables
        except (sqlite3.Error, RuntimeError):
            return False

    def indexed_items(self, partition: str) -> dict[str, tuple[float, str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT item_id,mtime,content_hash,doc_count FROM indexed_items "
                "WHERE partition=?",
                (partition,),
            ).fetchall()
        return {
            str(row["item_id"]): (
                float(row["mtime"]),
                str(row["content_hash"] or ""),
                int(row["doc_count"]),
            )
            for row in rows
        }

    def document_count(self, partition: str) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM documents WHERE partition=?", (partition,)
                ).fetchone()[0]
            )

    def last_build(self, partition: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM index_meta WHERE key=?", (f"last_build:{partition}",)
            ).fetchone()
        return str(row[0]) if row is not None else None


class _ReadOnlyContractStore:
    """Minimal Contracts store port backed exclusively by SQLite ``mode=ro``."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def connect(self, *, migrate: bool = False) -> sqlite3.Connection:
        if migrate:
            raise RuntimeError("cutover certification cannot migrate Contracts")
        return _ro_connection(self.path)


def _default_partitions(states: Mapping[str, Any]) -> dict[str, Any]:
    """Construct authority adapters whose reads cannot migrate their stores."""

    from work_buddy.contracts_domain.partition import ContractsPartition
    from work_buddy.journal_capture.partition import JournalPartition
    from work_buddy.journal_capture.store import JournalCaptureStore
    from work_buddy.knowledge.personal.partition import PersonalKnowledgePartition
    from work_buddy.knowledge.personal.store import PersonalKnowledgeStore
    from work_buddy.projects.partition import ProjectsPartition

    project_path = states["projects"].database_path
    return {
        "journal": JournalPartition(
            JournalCaptureStore(states["journal"].database_path, read_only=True)
        ),
        "projects": ProjectsPartition(
            connection_factory=lambda: _ro_connection(project_path)
        ),
        "contracts": ContractsPartition(
            _ReadOnlyContractStore(states["contracts"].database_path)
        ),
        "personal_knowledge": PersonalKnowledgePartition(
            PersonalKnowledgeStore(states["personal_knowledge"].database_path)
        ),
    }


def _partition_receipt(
    name: str,
    *,
    partition: Any,
    registered: bool,
    authority: Any,
    ledger: _ReadOnlyIndexLedger,
) -> dict[str, Any]:
    source_refs = list(partition.discover())
    source = {str(ref.item_id): ref for ref in source_refs}
    indexed = ledger.indexed_items(name)
    mismatch = len(set(source) ^ set(indexed))
    change_key = get_change_key(partition)
    for item_id in set(source) & set(indexed):
        mtime, content_hash, _doc_count = indexed[item_id]
        ref = source[item_id]
        if change_key == "mtime":
            mismatch += int(abs(float(ref.mtime) - mtime) > 1e-6)
        else:
            mismatch += int(str(ref.content_hash or "") != content_hash)
    counter = getattr(partition, "pending_search_event_count", None)
    pending = int(counter()) if counter is not None else -1
    zero_document_items = sum(1 for _m, _h, count in indexed.values() if count == 0)
    last_build = ledger.last_build(name)
    authority_ready = authority.value == _EXPECTED_AUTHORITIES[name]
    # A Project whose body is Co-work-authoritative can deliberately have a
    # zero-doc Projects ledger row. Report it separately, but keep this gate
    # closed until canonical document-head search supplies its own correlated
    # receipt; silently treating disappearance from this partition as parity
    # would overstate the cutover.
    zero_docs_ready = zero_document_items == 0
    visible = bool(
        registered
        and authority_ready
        and last_build
        and mismatch == 0
        and zero_docs_ready
    )
    ready = visible and pending == 0
    return {
        "registered": registered,
        "authority": authority.value,
        "authority_ready": authority_ready,
        "source_items": len(source),
        "indexed_items": len(indexed),
        "indexed_documents": ledger.document_count(name),
        "zero_document_items": zero_document_items,
        "delegated_or_empty_project_items": (
            zero_document_items if name == "projects" else 0
        ),
        "parity_mismatches": mismatch,
        "pending_outbox": pending,
        "build_observed": last_build is not None,
        "native_documents_visible": visible,
        "ready": ready,
    }


def _root_digest(path: Path) -> str:
    return hashlib.sha256(normalized_path(path, real=True).encode("utf-8")).hexdigest()


def certify_search_cutover(
    *,
    cfg: dict[str, Any] | None = None,
    prospective_domains: Iterable[str] = (),
    domains: Iterable[str] = SEARCH_DOMAINS,
    index_db_path: str | Path | None = None,
    registry: Any = None,
    partitions: Mapping[str, Any] | None = None,
    _index_build_lock_held: bool = False,
) -> dict[str, dict[str, Any]]:
    """Generate search and detachment receipts without mutating live state.

    ``prospective_domains`` activates the pre-seal evidence mode for validated
    configured roots. With no prospective domains, detachment is sustained only
    by authority rows and is ready only after every requested domain is sealed.
    """

    from work_buddy.config import load_config
    from work_buddy.index.config import load_index_config
    from work_buddy.index.partition import get_partition_registry
    from work_buddy.index.partitions.bootstrap import ensure_partitions_registered
    from work_buddy.vault_index.authority_exclusions import (
        legacy_authority_states,
        prospective_legacy_roots,
    )
    from work_buddy.vault_index.source import FilesystemSource

    app_cfg = cfg if cfg is not None else load_config()
    requested = tuple(dict.fromkeys(str(name) for name in domains))
    invalid = sorted(set(requested) - set(SEARCH_DOMAINS))
    if invalid or not requested:
        raise ValueError("domains must be a non-empty subset of native search domains")
    prospective_names = tuple(
        dict.fromkeys(str(name) for name in prospective_domains)
    )
    if set(prospective_names) - set(requested):
        raise ValueError("prospective domains must be included in certified domains")

    states = legacy_authority_states(
        app_cfg,
        allow_default_data_root=cfg is None,
        immutable=True,
    )
    if set(states) != set(SEARCH_DOMAINS):
        raise RuntimeError("configured authority state is unavailable")
    prospective_roots = prospective_legacy_roots(app_cfg, prospective_names)
    sealed_roots = tuple(
        state.configured_root for state in states.values() if state.sealed
    )
    effective_roots = tuple(dict.fromkeys((*sealed_roots, *prospective_roots)))

    if registry is None:
        ensure_partitions_registered()
        registry = get_partition_registry()
    registered_names = set(registry.names())

    configured_index = load_index_config(app_cfg)
    idx_path = (
        Path(index_db_path)
        if index_db_path is not None
        else (
            configured_index.db_path
            if configured_index.db_path is not None
            else states["journal"].database_path.parent / "index-consolidated.db"
        )
    )
    ledger = _ReadOnlyIndexLedger(idx_path)
    from work_buddy.utils.index_lock import is_locked

    build_gate = idx_path.parent / f"{idx_path.name}.build"
    # Release validation has a stronger caller that owns this gate across
    # database-head checks and the complete certification pass.  Treat that
    # caller-owned lock as quiescence rather than as a competing build.  The
    # leading underscore keeps this escape hatch internal to the cutover
    # evidence boundary; ordinary callers must continue to observe the lock.
    build_in_progress = (
        False if _index_build_lock_held else is_locked(build_gate)
    )
    index_available = ledger.available()
    partition_rows: dict[str, dict[str, Any]] = {}
    if index_available:
        try:
            adapters = dict(partitions) if partitions is not None else _default_partitions(states)
        except Exception:
            adapters = {}
        for name in requested:
            try:
                partition_rows[name] = _partition_receipt(
                    name,
                    partition=adapters[name],
                    registered=name in registered_names,
                    authority=states[name],
                    ledger=ledger,
                )
            except Exception:
                partition_rows[name] = {
                    "registered": name in registered_names,
                    "authority": states[name].value,
                    "authority_ready": False,
                    "source_items": -1,
                    "indexed_items": -1,
                    "indexed_documents": -1,
                    "zero_document_items": -1,
                    "delegated_or_empty_project_items": -1,
                    "parity_mismatches": -1,
                    "pending_outbox": -1,
                    "build_observed": False,
                    "native_documents_visible": False,
                    "ready": False,
                }

    search_ready = bool(
        index_available
        and not build_in_progress
        and len(partition_rows) == len(requested)
        and all(row["ready"] for row in partition_rows.values())
    )
    search = {
        "schema": "wb.search-cutover-evidence/v1",
        "index_available": index_available,
        "build_in_progress": build_in_progress,
        "partitions": partition_rows,
        "ready": search_ready,
    }

    vault_items = ledger.indexed_items("vault") if index_available else {}
    source = FilesystemSource(app_cfg, authority_exclusions=effective_roots)
    base = source.authority_detachment_evidence(vault_items.keys())
    root_rows: list[dict[str, Any]] = []
    for name in requested:
        state = states[name]
        prospective = name in prospective_names
        effective = state.configured_root in effective_roots
        root_source = FilesystemSource(
            app_cfg, authority_exclusions=(state.configured_root,)
        )
        stale = root_source.authority_detachment_evidence(vault_items.keys())[
            "indexed_excluded_items"
        ]
        root_rows.append(
            {
                "domain": name,
                "root_sha256": _root_digest(state.configured_root),
                "authority": state.value,
                "authority_sealed": state.sealed,
                "prospective": prospective,
                "effective_exclusion": effective,
                "discovery_fenced": effective and source.excludes_path(state.configured_root),
                "indexed_excluded_items": stale,
            }
        )
    prospective_mode = any(
        row["prospective"] and not row["authority_sealed"] for row in root_rows
    )
    detachment_ready = bool(
        index_available
        and not build_in_progress
        and all(row["effective_exclusion"] for row in root_rows)
        and all(row["discovery_fenced"] for row in root_rows)
        and all(row["indexed_excluded_items"] == 0 for row in root_rows)
        and (prospective_mode or all(row["authority_sealed"] for row in root_rows))
    )
    detachment = {
        "schema": "wb.legacy-root-detachment-evidence/v1",
        "mode": "prospective" if prospective_mode else "sustained",
        "configured_roots": len(root_rows),
        "effective_excluded_roots": int(base["detached_roots"]),
        "indexed_excluded_items": int(base["indexed_excluded_items"]),
        "build_in_progress": build_in_progress,
        "roots": root_rows,
        "ready": detachment_ready,
    }
    # A builder that began during certification invalidates both receipts. The
    # probe is read-only; callers retry after the writer gate clears.
    if not _index_build_lock_held and is_locked(build_gate):
        search["build_in_progress"] = True
        detachment["build_in_progress"] = True
        search["ready"] = False
        detachment["ready"] = False
    return {"search": search, "detachment": detachment}


__all__ = ["SEARCH_DOMAINS", "certify_search_cutover"]
