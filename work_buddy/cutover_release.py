"""Validate configured post-seal search evidence before reopening mutations.

Callers provide receipt files, not trusted digests.  Validation regenerates the
read-only search/detachment receipt from the configured authorities and requires
exact equality, so a caller-created JSON lookalike cannot manufacture a release.
The returned values contain hashes only and are safe to persist in the domain's
maintenance receipt.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from work_buddy.config import load_config
from work_buddy.cutover_maintenance import CutoverMaintenanceError, canonical_json
from work_buddy.index.config import load_index_config
from work_buddy.index.cutover_evidence import certify_search_cutover
from work_buddy.vault_index.authority_exclusions import (
    legacy_authority_states,
    normalized_path,
)


CHECKPOINT_SCHEMA = "wb.search-cutover-checkpoint-evidence/v1"
SEARCH_SCHEMA = "wb.search-cutover-evidence/v1"
DETACHMENT_SCHEMA = "wb.legacy-root-detachment-evidence/v1"
_DOMAIN_NAMES = {
    "journal": "journal",
    "projects": "projects",
    "contracts": "contracts",
    "personal_knowledge": "personal_knowledge",
}


def _read_json(path: str | Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = Path(path).expanduser().resolve().read_bytes()
        if not raw or len(raw) > 1_000_000:
            raise ValueError
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return raw, value
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise CutoverMaintenanceError(
            "postseal evidence is unavailable"
        ) from exc


def _nested(value: Mapping[str, Any], *, schema: str, key: str) -> dict[str, Any]:
    candidate: Any = value
    if value.get("schema") != schema:
        candidate = value.get(key)
    if not isinstance(candidate, dict) or candidate.get("schema") != schema:
        raise CutoverMaintenanceError("postseal evidence schema is invalid")
    return candidate


def _sha_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _path_sha(path: Path) -> str:
    return hashlib.sha256(
        normalized_path(path, real=True).encode("utf-8")
    ).hexdigest()


def validate_configured_postseal_evidence(
    *,
    domain: str,
    authority_db_path: str | Path,
    checkpoint_evidence_path: str | Path,
    search_evidence_path: str | Path,
    detachment_evidence_path: str | Path,
) -> dict[str, str]:
    """Re-certify the configured live state and return exact evidence hashes."""

    try:
        canonical_domain = _DOMAIN_NAMES[domain]
    except KeyError as exc:
        raise CutoverMaintenanceError("postseal evidence domain is invalid") from exc
    cfg = load_config()
    states = legacy_authority_states(
        cfg, allow_default_data_root=True, immutable=True
    )
    state = states.get(canonical_domain)
    supplied_db = Path(authority_db_path).expanduser().resolve()
    if (
        state is None
        or state.database_path.resolve() != supplied_db
        or not state.sealed
        or not supplied_db.is_file()
    ):
        raise CutoverMaintenanceError(
            "postseal evidence is not bound to the configured authority"
        )

    _checkpoint_raw, checkpoint_value = _read_json(checkpoint_evidence_path)
    if (
    checkpoint_value.get("schema") != CHECKPOINT_SCHEMA
        or checkpoint_value.get("ready") is not True
    ):
        raise CutoverMaintenanceError("database checkpoint evidence is not ready")
    requested = checkpoint_value.get("requested_domains")
    rows = checkpoint_value.get("databases")
    if (
        not isinstance(requested, list)
        or canonical_domain not in requested
        or not isinstance(rows, list)
        or not all(isinstance(row, dict) for row in rows)
    ):
        raise CutoverMaintenanceError("database checkpoint scope is invalid")
    configured_index = load_index_config(cfg)
    index_path = configured_index.resolved_db_path()
    expected_paths = {
        canonical_domain: _path_sha(supplied_db),
        "consolidated_index": _path_sha(index_path),
    }
    selected = {
        str(row.get("name")): row
        for row in rows
        if str(row.get("name")) in expected_paths
    }
    if set(selected) != set(expected_paths):
        raise CutoverMaintenanceError("database checkpoint paths are incomplete")
    for name, expected_path_sha in expected_paths.items():
        row = selected[name]
        if (
            row.get("path_sha256") != expected_path_sha
            or row.get("database_exists") is not True
            or row.get("ready") is not True
            or int(row.get("busy_frames", -1)) != 0
            or int(row.get("wal_bytes_after", -1)) != 0
            or int(row.get("rollback_journal_bytes_after", -1)) != 0
        ):
            raise CutoverMaintenanceError("database checkpoint paths are not ready")

    # Regenerate the stable, content-bound portion of the checkpoint receipt.
    # The operational WAL counters alone are caller-authorable; exact main-file
    # heads plus fresh sidecar checks make release depend on the configured
    # databases actually observed by Work Buddy.
    supplied_heads = checkpoint_value.get("database_heads")
    if not isinstance(supplied_heads, dict):
        raise CutoverMaintenanceError("database checkpoint heads are unavailable")
    _search_raw, search_container = _read_json(search_evidence_path)
    supplied_search = _nested(
        search_container, schema=SEARCH_SCHEMA, key="search"
    )
    _detach_raw, detach_container = _read_json(detachment_evidence_path)
    supplied_detachment = _nested(
        detach_container, schema=DETACHMENT_SCHEMA, key="detachment"
    )
    from work_buddy.index.cutover_checkpoint import (
        recertify_checkpointed_search_cutover,
    )

    actual = recertify_checkpointed_search_cutover(
        cfg=cfg,
        domains=(canonical_domain,),
        _certifier=certify_search_cutover,
    )
    actual_heads = actual["database_heads"]
    if (
        actual.get("database_heads_stable") is not True
        or actual_heads.get("ready") is not True
        or supplied_heads != actual_heads
    ):
        raise CutoverMaintenanceError(
            "database checkpoint heads do not match live state"
        )
    actual_search = actual["search"]
    actual_detachment = actual["detachment"]
    if (
        actual_search.get("ready") is not True
        or actual_detachment.get("ready") is not True
        or actual_detachment.get("mode") != "sustained"
        or supplied_search != actual_search
        or supplied_detachment != actual_detachment
    ):
        raise CutoverMaintenanceError(
            "postseal search or detachment evidence does not match live state"
        )

    # Immutable certification above rejects nonempty WAL/journal sidecars.  Hash
    # the exact main database file it observed; the release receipt thereby
    # binds the domain high-water immediately before opening native mutations.
    authority_head = hashlib.sha256(supplied_db.read_bytes()).hexdigest()
    return {
        "databaseCheckpoint": _sha_json(checkpoint_value),
        "search": _sha_json(actual_search),
        "detachment": _sha_json(actual_detachment),
        "authorityHead": authority_head,
    }


def hash_supplied_postseal_evidence(
    *,
    checkpoint_evidence_path: str | Path,
    search_evidence_path: str | Path,
    detachment_evidence_path: str | Path,
) -> dict[str, str]:
    """Hash schema-valid supplied receipts for an exact completed replay."""

    _raw, checkpoint = _read_json(checkpoint_evidence_path)
    if checkpoint.get("schema") != CHECKPOINT_SCHEMA:
        raise CutoverMaintenanceError("database checkpoint evidence schema is invalid")
    _raw, search_container = _read_json(search_evidence_path)
    search = _nested(search_container, schema=SEARCH_SCHEMA, key="search")
    _raw, detachment_container = _read_json(detachment_evidence_path)
    detachment = _nested(
        detachment_container, schema=DETACHMENT_SCHEMA, key="detachment"
    )
    return {
        "databaseCheckpoint": _sha_json(checkpoint),
        "search": _sha_json(search),
        "detachment": _sha_json(detachment),
    }


__all__ = [
    "hash_supplied_postseal_evidence",
    "validate_configured_postseal_evidence",
]
