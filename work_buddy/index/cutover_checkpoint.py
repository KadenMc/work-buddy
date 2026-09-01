"""Bounded SQLite checkpoint step for immutable search-cutover evidence.

Certification opens the consolidated index and native authority stores with
SQLite's ``immutable=1`` flag.  That is deliberately strict: immutable readers
do not merge a WAL, so an operator must checkpoint the exact configured stores
after writers are quiesced and before asking for a certification receipt.

This module is the narrow write boundary for that preparation.  Callers select
known domains, never paths; no database is created or migrated.  The
consolidated-index build lock prevents a refresh from racing its checkpoint.
Each receipt also carries a stable, content-bound main-file head set that the
release path regenerates independently before opening native mutations.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

from work_buddy.index.cutover_evidence import SEARCH_DOMAINS
from work_buddy.vault_index.authority_exclusions import (
    legacy_authority_states,
    normalized_path,
)


CHECKPOINT_SCHEMA = "wb.search-cutover-checkpoint-evidence/v1"
DATABASE_HEADS_SCHEMA = "wb.search-cutover-database-heads/v1"


def _path_digest(path: Path) -> str:
    return hashlib.sha256(
        normalized_path(path, real=True).encode("utf-8")
    ).hexdigest()


def _sidecar_bytes(path: Path, suffix: str) -> int:
    sidecar = Path(f"{path}{suffix}")
    try:
        return int(sidecar.stat().st_size) if sidecar.is_file() else 0
    except OSError:
        # A concurrent filesystem change is not safe to certify.
        return -1


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _database_head(name: str, path: Path) -> dict[str, Any]:
    """Read one stable main-file head while rejecting SQLite sidecars."""

    row: dict[str, Any] = {
        "name": name,
        "path_sha256": _path_digest(path),
        "database_exists": path.is_file(),
        "database_bytes": -1,
        "database_sha256": None,
        "wal_bytes": _sidecar_bytes(path, "-wal"),
        "rollback_journal_bytes": _sidecar_bytes(path, "-journal"),
        "ready": False,
    }
    if not row["database_exists"]:
        return row
    try:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        after = path.stat()
        row["database_bytes"] = int(after.st_size)
        row["database_sha256"] = digest.hexdigest()
        row["wal_bytes"] = _sidecar_bytes(path, "-wal")
        row["rollback_journal_bytes"] = _sidecar_bytes(path, "-journal")
        row["ready"] = bool(
            before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and row["wal_bytes"] == 0
            and row["rollback_journal_bytes"] == 0
        )
    except OSError:
        pass
    return row


def _database_heads_receipt(
    requested: tuple[str, ...],
    paths: list[tuple[str, Path]],
) -> dict[str, Any]:
    rows = [_database_head(name, path) for name, path in paths]
    stable = {
        "schema": DATABASE_HEADS_SCHEMA,
        "requested_domains": list(requested),
        "databases": rows,
        "ready": all(row["ready"] for row in rows),
    }
    return {**stable, "head_set_sha256": _canonical_sha256(stable)}


def _configured_paths(
    *,
    cfg: dict[str, Any] | None,
    domains: Iterable[str],
    index_db_path: str | Path | None,
) -> tuple[dict[str, Any], tuple[str, ...], list[tuple[str, Path]], Path]:
    from work_buddy.config import load_config
    from work_buddy.index.config import load_index_config

    app_cfg = cfg if cfg is not None else load_config()
    requested = tuple(dict.fromkeys(str(name) for name in domains))
    invalid = sorted(set(requested) - set(SEARCH_DOMAINS))
    if invalid or not requested:
        raise ValueError("domains must be a non-empty subset of native search domains")
    states = legacy_authority_states(
        app_cfg,
        allow_default_data_root=cfg is None,
        immutable=False,
    )
    if set(states) != set(SEARCH_DOMAINS):
        raise RuntimeError("configured authority databases are unavailable")
    configured_index = load_index_config(app_cfg)
    idx_path = (
        Path(index_db_path)
        if index_db_path is not None
        else (
            configured_index.db_path
            if configured_index.db_path is not None
            else states["journal"].database_path.parent / "index-consolidated.db"
        )
    ).expanduser().resolve()
    paths = [(name, states[name].database_path.resolve()) for name in requested]
    paths.append(("consolidated_index", idx_path))
    return app_cfg, requested, paths, idx_path


def inspect_checkpointed_database_heads(
    *,
    cfg: dict[str, Any] | None = None,
    domains: Iterable[str] = SEARCH_DOMAINS,
    index_db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Regenerate a read-only receipt for exact checkpointed main-file heads."""

    from work_buddy.utils.index_lock import index_lock

    _app_cfg, requested, paths, idx_path = _configured_paths(
        cfg=cfg,
        domains=domains,
        index_db_path=index_db_path,
    )
    build_gate = idx_path.parent / f"{idx_path.name}.build"
    with index_lock(build_gate, timeout=30):
        return _database_heads_receipt(requested, paths)


def recertify_checkpointed_search_cutover(
    *,
    cfg: dict[str, Any] | None = None,
    domains: Iterable[str] = SEARCH_DOMAINS,
    index_db_path: str | Path | None = None,
    _certifier: Callable[..., dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Bind exact database heads to one lock-held search certification.

    The consolidated-index build gate stays owned from the first database hash
    through search/detachment inspection and the final hash.  Comparing both
    head sets also detects a domain database writer that races the read-only
    certification even though domain writers do not use the index build gate.
    """

    from work_buddy.utils.index_lock import index_lock

    if _certifier is None:
        from work_buddy.index.cutover_evidence import certify_search_cutover

        _certifier = certify_search_cutover

    app_cfg, requested, paths, idx_path = _configured_paths(
        cfg=cfg,
        domains=domains,
        index_db_path=index_db_path,
    )
    build_gate = idx_path.parent / f"{idx_path.name}.build"
    with index_lock(build_gate, timeout=30):
        heads_before = _database_heads_receipt(requested, paths)
        evidence = _certifier(
            cfg=app_cfg,
            domains=requested,
            index_db_path=idx_path,
            _index_build_lock_held=True,
        )
        heads_after = _database_heads_receipt(requested, paths)
    return {
        "database_heads": heads_after,
        "database_heads_stable": heads_before == heads_after,
        "search": evidence["search"],
        "detachment": evidence["detachment"],
    }


def _checkpoint_database(name: str, path: Path) -> dict[str, Any]:
    """Checkpoint one existing database without creating or migrating it."""

    row: dict[str, Any] = {
        "name": name,
        "path_sha256": _path_digest(path),
        "database_exists": path.is_file(),
        "wal_bytes_before": _sidecar_bytes(path, "-wal"),
        "busy_frames": -1,
        "log_frames": -1,
        "checkpointed_frames": -1,
        "wal_bytes_after": -1,
        "rollback_journal_bytes_after": -1,
        "ready": False,
    }
    if not row["database_exists"]:
        return row

    try:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=rw",
            uri=True,
            timeout=30,
        )
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            connection.close()
        if checkpoint is not None and len(checkpoint) >= 3:
            row["busy_frames"] = int(checkpoint[0])
            row["log_frames"] = int(checkpoint[1])
            row["checkpointed_frames"] = int(checkpoint[2])
    except sqlite3.Error:
        # Receipts are intentionally data-free.  The numeric/status fields are
        # sufficient for the operator to retry after quiescing writers.
        pass

    row["wal_bytes_after"] = _sidecar_bytes(path, "-wal")
    row["rollback_journal_bytes_after"] = _sidecar_bytes(path, "-journal")
    row["ready"] = bool(
        row["busy_frames"] == 0
        and row["wal_bytes_after"] == 0
        and row["rollback_journal_bytes_after"] == 0
    )
    return row


def checkpoint_search_cutover_databases(
    *,
    cfg: dict[str, Any] | None = None,
    domains: Iterable[str] = SEARCH_DOMAINS,
    index_db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Checkpoint configured native stores and the consolidated index.

    The function accepts domain names only and derives every physical path from
    configuration.  It refuses missing databases and returns a machine-derived,
    content-free receipt.  Operators should quiesce domain writers before this
    step; any writer that races afterward recreates a WAL and causes immutable
    certification to fail closed.
    """

    from work_buddy.utils.index_lock import index_lock

    _app_cfg, requested, paths, idx_path = _configured_paths(
        cfg=cfg,
        domains=domains,
        index_db_path=index_db_path,
    )

    # Validate before acquiring the lock so a bad request cannot create a lock
    # parent as a side effect.
    missing = [name for name, path in paths if not path.is_file()]
    if missing:
        heads = _database_heads_receipt(requested, paths)
        return {
            "schema": CHECKPOINT_SCHEMA,
            "requested_domains": list(requested),
            "databases": [
                {
                    "name": name,
                    "path_sha256": _path_digest(path),
                    "database_exists": path.is_file(),
                    "wal_bytes_before": _sidecar_bytes(path, "-wal"),
                    "busy_frames": -1,
                    "log_frames": -1,
                    "checkpointed_frames": -1,
                    "wal_bytes_after": _sidecar_bytes(path, "-wal"),
                    "rollback_journal_bytes_after": _sidecar_bytes(path, "-journal"),
                    "ready": False,
                }
                for name, path in paths
            ],
            "database_heads": heads,
            "ready": False,
        }

    build_gate = idx_path.parent / f"{idx_path.name}.build"
    with index_lock(build_gate, timeout=30):
        rows = [_checkpoint_database(name, path) for name, path in paths]
        heads = _database_heads_receipt(requested, paths)
    return {
        "schema": CHECKPOINT_SCHEMA,
        "requested_domains": list(requested),
        "databases": rows,
        "database_heads": heads,
        "ready": all(row["ready"] for row in rows) and heads["ready"] is True,
    }


__all__ = [
    "CHECKPOINT_SCHEMA",
    "DATABASE_HEADS_SCHEMA",
    "checkpoint_search_cutover_databases",
    "inspect_checkpointed_database_heads",
    "recertify_checkpointed_search_cutover",
]
