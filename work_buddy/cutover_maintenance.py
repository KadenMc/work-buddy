"""Durable per-domain maintenance fences for authority cutovers.

The domain migration owns the two table declarations.  This module owns the
state machine and content-free receipts so Journal-adjacent SQLite domains use
the same fail-closed release rule without pretending their authority flips are
one distributed transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import threading
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


_SHA256 = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,255}")
_DOMAINS = frozenset({"journal", "projects", "contracts", "personal_knowledge"})
_POSTSEAL_EVIDENCE_KEYS = frozenset(
    {"databaseCheckpoint", "search", "detachment", "authorityHead"}
)
_REHEARSAL_CAPABILITY_SECRET = secrets.token_bytes(32)
_REHEARSAL_HANDLE_LOCK = threading.Lock()
_REHEARSAL_OPEN_HANDLES: dict[int, tuple[int, tuple[tuple[str, int], ...]]] = {}


class CutoverMaintenanceError(RuntimeError):
    """A maintenance fence request is invalid or conflicts with durable state."""


class CutoverMaintenanceFenced(CutoverMaintenanceError):
    """An ordinary domain mutation was attempted while maintenance is held."""


@dataclass(frozen=True, slots=True, weakref_slot=True)
class IsolatedRehearsalAuthorization:
    """Process-local capability for exact temporary authority database files."""

    root: str
    root_identity: tuple[int, int, int]
    authority_paths: tuple[tuple[str, str], ...]
    authority_identities: tuple[tuple[str, int, int, int], ...]
    proof: str


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _token(value: str, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise CutoverMaintenanceError(f"{label} is invalid")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CutoverMaintenanceError(f"{label} is invalid")
    return value


def _domain(value: str) -> str:
    if value not in _DOMAINS:
        raise CutoverMaintenanceError("domain is invalid")
    return value


def _row(conn: sqlite3.Connection, domain: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM cutover_maintenance WHERE singleton=1"
    ).fetchone()
    if row is None or str(row["domain"]) != domain:
        raise CutoverMaintenanceError("cutover maintenance state is unavailable")
    return row


def _receipt_replay(
    conn: sqlite3.Connection,
    *,
    mutation_id: str,
    request_sha256: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT request_sha256,result_json,result_sha256 "
        "FROM cutover_maintenance_receipts WHERE mutation_id=?",
        (mutation_id,),
    ).fetchone()
    if row is None:
        return None
    if str(row["request_sha256"]) != request_sha256:
        raise CutoverMaintenanceError("cutover maintenance mutation identity was reused")
    result_json = str(row["result_json"])
    if hashlib.sha256(result_json.encode("utf-8")).hexdigest() != str(
        row["result_sha256"]
    ):
        raise CutoverMaintenanceError("cutover maintenance receipt changed")
    value = json.loads(result_json)
    if not isinstance(value, dict):
        raise CutoverMaintenanceError("cutover maintenance receipt is invalid")
    return value


def _record_receipt(
    conn: sqlite3.Connection,
    *,
    mutation_id: str,
    operation: str,
    request_sha256: str,
    result: Mapping[str, Any],
    created_at: str,
) -> None:
    result_json = canonical_json(dict(result))
    conn.execute(
        "INSERT INTO cutover_maintenance_receipts "
        "(mutation_id,operation,request_sha256,result_json,result_sha256,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            mutation_id,
            operation,
            request_sha256,
            result_json,
            hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
            created_at,
        ),
    )


def prior_postseal_release_evidence(
    conn: sqlite3.Connection, *, mutation_id: str
) -> dict[str, str] | None:
    """Return the evidence frozen by an already-completed release replay."""

    row = conn.execute(
        "SELECT operation,result_json,result_sha256 FROM cutover_maintenance_receipts "
        "WHERE mutation_id=?",
        (mutation_id,),
    ).fetchone()
    if row is None or str(row["operation"]) != "release_postseal":
        return None
    result_json = str(row["result_json"])
    if hashlib.sha256(result_json.encode("utf-8")).hexdigest() != str(
        row["result_sha256"]
    ):
        raise CutoverMaintenanceError("cutover maintenance receipt changed")
    result = json.loads(result_json)
    evidence = result.get("evidenceSha256s") if isinstance(result, dict) else None
    if not isinstance(evidence, dict) or set(evidence) != _POSTSEAL_EVIDENCE_KEYS:
        raise CutoverMaintenanceError("cutover maintenance receipt is invalid")
    return {
        key: _digest(str(value), f"{key} evidence digest")
        for key, value in evidence.items()
    }


def require_mutations_open(conn: sqlite3.Connection, *, domain: str) -> None:
    """Fail an ordinary mutation while a pre/post-seal fence is held."""

    if str(_row(conn, _domain(domain))["state"]) != "open":
        raise CutoverMaintenanceFenced("domain mutations are fenced for cutover")


def _stat_identity(value: os.stat_result) -> tuple[int, int, int]:
    # Windows can recycle ``st_ino`` immediately after unlink/recreate. Its
    # ``st_ctime_ns`` is the file creation timestamp and remains stable across
    # normal SQLite writes, so include it to distinguish a replacement file.
    # POSIX ctime changes on ordinary writes, so inode identity remains the
    # stable capability boundary there.
    creation_stamp = int(value.st_ctime_ns) if os.name == "nt" else 0
    return int(value.st_dev), int(value.st_ino), creation_stamp


def _identity(path: Path) -> tuple[int, int, int]:
    return _stat_identity(path.stat())


def _release_rehearsal_handles(authorization_id: int) -> None:
    with _REHEARSAL_HANDLE_LOCK:
        pinned = _REHEARSAL_OPEN_HANDLES.pop(authorization_id, None)
    if pinned is None:
        return
    root_fd, authority_fds = pinned
    for fd in (root_fd, *(fd for _domain_name, fd in authority_fds)):
        try:
            os.close(fd)
        except OSError:
            pass


def _pin_rehearsal_handles(
    authorization: IsolatedRehearsalAuthorization,
) -> None:
    """Keep POSIX inodes alive so unlink/recreate cannot recycle identity."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0))
    opened: list[int] = []
    try:
        root_fd = os.open(authorization.root, flags)
        opened.append(root_fd)
        if _stat_identity(os.fstat(root_fd)) != authorization.root_identity:
            raise CutoverMaintenanceError("isolated rehearsal root identity changed")
        expected_identities = {
            name: (device, inode, creation_stamp)
            for name, device, inode, creation_stamp in authorization.authority_identities
        }
        authority_fds: list[tuple[str, int]] = []
        for domain, path in authorization.authority_paths:
            fd = os.open(path, flags)
            opened.append(fd)
            if _stat_identity(os.fstat(fd)) != expected_identities[domain]:
                raise CutoverMaintenanceError(
                    "rehearsal authority file identity changed"
                )
            authority_fds.append((domain, fd))
        with _REHEARSAL_HANDLE_LOCK:
            _REHEARSAL_OPEN_HANDLES[id(authorization)] = (
                root_fd,
                tuple(authority_fds),
            )
        weakref.finalize(
            authorization,
            _release_rehearsal_handles,
            id(authorization),
        )
    except Exception:
        for fd in reversed(opened):
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _require_pinned_rehearsal_handles(
    authorization: IsolatedRehearsalAuthorization,
    domain: str,
) -> None:
    if os.name == "nt":
        return
    with _REHEARSAL_HANDLE_LOCK:
        pinned = _REHEARSAL_OPEN_HANDLES.get(id(authorization))
    if pinned is None:
        raise CutoverMaintenanceError("isolated rehearsal authorization changed")
    root_fd, authority_fds = pinned
    domain_fds = dict(authority_fds)
    try:
        root_identity = _stat_identity(os.fstat(root_fd))
        authority_identity = _stat_identity(os.fstat(domain_fds[domain]))
    except (KeyError, OSError) as exc:
        raise CutoverMaintenanceError(
            "isolated rehearsal authorization changed"
        ) from exc
    expected_identities = {
        name: (device, inode, creation_stamp)
        for name, device, inode, creation_stamp in authorization.authority_identities
    }
    if (
        root_identity != authorization.root_identity
        or authority_identity != expected_identities.get(domain)
    ):
        raise CutoverMaintenanceError("isolated rehearsal authorization changed")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reparse(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return True
    attributes = int(getattr(value, "st_file_attributes", 0))
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return path.is_symlink() or bool(marker and attributes & marker)


def _reject_reparse_below(path: Path, root: Path) -> None:
    raw = Path(os.path.abspath(str(path.expanduser())))
    boundary = Path(os.path.abspath(str(root.expanduser())))
    try:
        relative = raw.relative_to(boundary)
    except ValueError as exc:
        raise CutoverMaintenanceError(
            "rehearsal authority is outside its temporary root"
        ) from exc
    current = boundary
    if _reparse(current):
        raise CutoverMaintenanceError("rehearsal root cannot be a filesystem alias")
    for part in relative.parts:
        current /= part
        if current.exists() and _reparse(current):
            raise CutoverMaintenanceError(
                "rehearsal authority cannot traverse a filesystem alias"
            )


def _reject_any_reparse(path: Path, *, label: str) -> None:
    """Reject an existing reparse component anywhere in an absolute path."""

    raw = Path(os.path.abspath(str(path.expanduser())))
    anchor = Path(raw.anchor)
    current = anchor
    for part in raw.parts[1:]:
        current /= part
        if current.exists() and _reparse(current):
            raise CutoverMaintenanceError(f"{label} cannot use a filesystem alias")


def _configured_live_authorities() -> tuple[Path, tuple[Path, ...]]:
    """Resolve configured live roots read-only, without creating a store."""

    from work_buddy.config import load_config
    from work_buddy.index.config import load_index_config
    from work_buddy.paths import RESOURCES, _data_base
    from work_buddy.sources.store import SourcesPaths
    from work_buddy.vault_index.authority_exclusions import legacy_authority_states

    cfg = load_config()
    raw_live_root = Path(_data_base()).expanduser()
    _reject_any_reparse(raw_live_root, label="configured live data root")
    live_root = raw_live_root.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    raw_live_absolute = Path(os.path.abspath(str(raw_live_root)))
    if _is_within(raw_live_absolute, temporary_root) or _is_within(
        live_root, temporary_root
    ):
        raise CutoverMaintenanceError(
            "configured live data root cannot be inside the OS temporary root"
        )
    states = legacy_authority_states(
        cfg,
        allow_default_data_root=True,
        immutable=True,
    )
    paths = [state.database_path.expanduser().resolve() for state in states.values()]
    configured_index = load_index_config(cfg).resolved_db_path().expanduser().resolve()
    paths.append(configured_index)
    paths.append(SourcesPaths.from_root(live_root / "db" / "sources").db.resolve())
    # A domain seal must never be rehearsed against a hard link to any other
    # registered live SQLite authority.  Resolve this registry without calling
    # ``paths.resolve`` so this read-only guard cannot create live directories.
    paths.extend(
        (live_root / relative).resolve()
        for name, relative in RESOURCES.items()
        if name.startswith("db/") and str(relative).endswith(".db")
    )
    return live_root, tuple(dict.fromkeys(paths))


def _same_file(left: Path, right: Path) -> bool:
    if os.path.normcase(str(left)) == os.path.normcase(str(right)):
        return True
    if not left.exists() or not right.exists():
        return False
    try:
        return os.path.samefile(left, right) or _identity(left) == _identity(right)
    except OSError:
        return True


def _capability_payload(
    *,
    root: Path,
    root_identity: tuple[int, int, int],
    authority_paths: tuple[tuple[str, str], ...],
    authority_identities: tuple[tuple[str, int, int, int], ...],
) -> bytes:
    return canonical_json(
        {
            "schema": "wb.isolated-cutover-rehearsal-authorization/v1",
            "root": str(root),
            "rootIdentity": list(root_identity),
            "authorityPaths": [list(value) for value in authority_paths],
            "authorityIdentities": [list(value) for value in authority_identities],
        }
    ).encode("utf-8")


def authorize_isolated_rehearsal_root(
    root: str | Path,
    *,
    authority_paths: Mapping[str, str | Path],
) -> IsolatedRehearsalAuthorization:
    """Mint one process-local capability for exact DBs under a fresh temp root."""

    if not isinstance(authority_paths, Mapping) or not authority_paths:
        raise CutoverMaintenanceError("rehearsal authority scope is empty")
    if any(not isinstance(value, str) for value in authority_paths):
        raise CutoverMaintenanceError("rehearsal authority scope is invalid")
    domains = tuple(sorted(authority_paths))
    if set(domains) - _DOMAINS:
        raise CutoverMaintenanceError("rehearsal authority scope is invalid")
    raw_root = Path(root).expanduser()
    resolved_root = raw_root.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        not resolved_root.is_dir()
        or not _is_within(resolved_root, temporary_root)
    ):
        raise CutoverMaintenanceError(
            "rehearsal authorization requires an existing OS-temporary root"
        )
    _reject_reparse_below(raw_root, temporary_root)
    live_root, live_paths = _configured_live_authorities()
    if (
        _is_within(live_root, temporary_root)
        or _is_within(resolved_root, live_root)
        or _is_within(live_root, resolved_root)
        or _same_file(resolved_root, live_root)
    ):
        raise CutoverMaintenanceError(
            "configured live data cannot share a rehearsal root"
        )
    normalized: list[tuple[str, str]] = []
    identities: list[tuple[str, int, int, int]] = []
    for domain in domains:
        raw_path = Path(authority_paths[domain]).expanduser()
        candidate = raw_path.resolve()
        if not candidate.is_file() or not _is_within(candidate, resolved_root):
            raise CutoverMaintenanceError(
                "rehearsal authorization requires an existing scoped database"
            )
        _reject_reparse_below(raw_path, raw_root)
        if any(_same_file(candidate, live) for live in live_paths):
            raise CutoverMaintenanceError(
                "a configured authority database cannot be authorized for rehearsal"
            )
        device, inode, creation_stamp = _identity(candidate)
        normalized.append((domain, str(candidate)))
        identities.append((domain, device, inode, creation_stamp))
    normalized_scope = tuple(normalized)
    identity_scope = tuple(identities)
    root_identity = _identity(resolved_root)
    payload = _capability_payload(
        root=resolved_root,
        root_identity=root_identity,
        authority_paths=normalized_scope,
        authority_identities=identity_scope,
    )
    proof = hmac.new(
        _REHEARSAL_CAPABILITY_SECRET, payload, hashlib.sha256
    ).hexdigest()
    authorization = IsolatedRehearsalAuthorization(
        root=str(resolved_root),
        root_identity=root_identity,
        authority_paths=normalized_scope,
        authority_identities=identity_scope,
        proof=proof,
    )
    _pin_rehearsal_handles(authorization)
    return authorization


def require_isolated_rehearsal_path(
    path: str | Path,
    *,
    domain: str,
    authorization: IsolatedRehearsalAuthorization | None,
) -> None:
    """Require an intact exact-file capability; lexical temp paths never suffice."""

    domain = _domain(domain)
    if not isinstance(authorization, IsolatedRehearsalAuthorization):
        raise CutoverMaintenanceError("isolated rehearsal authorization is required")
    root = Path(authorization.root)
    payload = _capability_payload(
        root=root,
        root_identity=authorization.root_identity,
        authority_paths=authorization.authority_paths,
        authority_identities=authorization.authority_identities,
    )
    expected = hmac.new(
        _REHEARSAL_CAPABILITY_SECRET, payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, authorization.proof):
        raise CutoverMaintenanceError("isolated rehearsal authorization changed")
    _require_pinned_rehearsal_handles(authorization, domain)
    if not root.is_dir() or _identity(root) != authorization.root_identity:
        raise CutoverMaintenanceError("isolated rehearsal root identity changed")
    scoped_paths = dict(authorization.authority_paths)
    scoped_identities = {
        name: (device, inode, creation_stamp)
        for name, device, inode, creation_stamp in authorization.authority_identities
    }
    candidate = Path(path).expanduser().resolve()
    if scoped_paths.get(domain) != str(candidate):
        raise CutoverMaintenanceError("rehearsal authority is outside its scope")
    _reject_reparse_below(Path(path).expanduser(), root)
    if not candidate.is_file() or _identity(candidate) != scoped_identities.get(domain):
        raise CutoverMaintenanceError("rehearsal authority file identity changed")
    live_root, live_paths = _configured_live_authorities()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if _is_within(live_root, temporary_root) or any(
        _same_file(candidate, live) for live in live_paths
    ):
        raise CutoverMaintenanceError(
            "configured live authority cannot use rehearsal authorization"
        )


def pause_cutover_maintenance(
    conn: sqlite3.Connection,
    *,
    domain: str,
    cohort_id: str,
    inventory_sha256: str,
    mutation_id: str,
    actor_sha256: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Acquire an idempotent preseal fence bound to one exact cohort scope."""

    domain = _domain(domain)
    cohort_id = _token(cohort_id, "cohort identity")
    mutation_id = _token(mutation_id, "mutation identity")
    inventory_sha256 = _digest(inventory_sha256, "inventory digest")
    actor_sha256 = _digest(actor_sha256, "actor digest")
    request = {
        "operation": "pause",
        "domain": domain,
        "cohortId": cohort_id,
        "inventorySha256": inventory_sha256,
        "actorSha256": actor_sha256,
    }
    request_sha = sha256_json(request)
    replay = _receipt_replay(
        conn,
        mutation_id=mutation_id,
        request_sha256=request_sha,
    )
    if replay is not None:
        return replay
    state = _row(conn, domain)
    if str(state["state"]) != "open":
        raise CutoverMaintenanceError("another cutover maintenance fence is active")
    timestamp = at or _now()
    conn.execute(
        "UPDATE cutover_maintenance SET state='preseal_fenced',cohort_id=?,"
        "inventory_sha256=?,fence_id=?,pause_request_sha256=?,paused_at=?,"
        "postseal_evidence_sha256=NULL,released_at=NULL,updated_at=? WHERE singleton=1",
        (cohort_id, inventory_sha256, mutation_id, request_sha, timestamp, timestamp),
    )
    result = {
        "schema": "wb.domain-cutover-maintenance/v1",
        "domain": domain,
        "cohortId": cohort_id,
        "inventorySha256": inventory_sha256,
        "state": "preseal_fenced",
    }
    _record_receipt(
        conn,
        mutation_id=mutation_id,
        operation="pause",
        request_sha256=request_sha,
        result=result,
        created_at=timestamp,
    )
    return result


def mark_postseal_pending(
    conn: sqlite3.Connection,
    *,
    domain: str,
    cohort_id: str,
    inventory_sha256: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Retain the fence after authority publication until search certifies."""

    domain = _domain(domain)
    state = _row(conn, domain)
    if (
        str(state["state"]) != "preseal_fenced"
        or str(state["cohort_id"]) != cohort_id
        or str(state["inventory_sha256"]) != inventory_sha256
    ):
        raise CutoverMaintenanceError("cutover publication is outside its fence")
    timestamp = at or _now()
    conn.execute(
        "UPDATE cutover_maintenance SET state='postseal_pending',updated_at=? "
        "WHERE singleton=1",
        (timestamp,),
    )
    return {
        "schema": "wb.domain-cutover-maintenance/v1",
        "domain": domain,
        "cohortId": cohort_id,
        "inventorySha256": inventory_sha256,
        "state": "postseal_pending",
    }


def resume_preseal_maintenance(
    conn: sqlite3.Connection,
    *,
    domain: str,
    cohort_id: str,
    mutation_id: str,
    actor_sha256: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Release only a preseal fence; postseal authority always rolls forward."""

    domain = _domain(domain)
    mutation_id = _token(mutation_id, "mutation identity")
    actor_sha256 = _digest(actor_sha256, "actor digest")
    request = {
        "operation": "resume_preseal",
        "domain": domain,
        "cohortId": cohort_id,
        "actorSha256": actor_sha256,
    }
    request_sha = sha256_json(request)
    replay = _receipt_replay(
        conn,
        mutation_id=mutation_id,
        request_sha256=request_sha,
    )
    if replay is not None:
        return replay
    state = _row(conn, domain)
    if str(state["state"]) != "preseal_fenced" or str(state["cohort_id"]) != cohort_id:
        raise CutoverMaintenanceError("only the active preseal fence can resume")
    timestamp = at or _now()
    conn.execute(
        "UPDATE cutover_maintenance SET state='open',cohort_id=NULL,"
        "inventory_sha256=NULL,fence_id=NULL,pause_request_sha256=NULL,"
        "paused_at=NULL,updated_at=? WHERE singleton=1",
        (timestamp,),
    )
    result = {
        "schema": "wb.domain-cutover-maintenance/v1",
        "domain": domain,
        "cohortId": cohort_id,
        "state": "open",
    }
    _record_receipt(
        conn,
        mutation_id=mutation_id,
        operation="resume_preseal",
        request_sha256=request_sha,
        result=result,
        created_at=timestamp,
    )
    return result


def release_postseal_maintenance(
    conn: sqlite3.Connection,
    *,
    domain: str,
    cohort_id: str,
    mutation_id: str,
    actor_sha256: str,
    evidence_sha256s: Mapping[str, str],
    at: str | None = None,
) -> dict[str, Any]:
    """Open native writes only after the complete postflip evidence set."""

    domain = _domain(domain)
    mutation_id = _token(mutation_id, "mutation identity")
    actor_sha256 = _digest(actor_sha256, "actor digest")
    if set(evidence_sha256s) != _POSTSEAL_EVIDENCE_KEYS:
        raise CutoverMaintenanceError("postseal evidence is incomplete")
    evidence = {
        key: _digest(evidence_sha256s[key], f"{key} evidence digest")
        for key in sorted(_POSTSEAL_EVIDENCE_KEYS)
    }
    state = _row(conn, domain)
    inventory_sha256 = _digest(
        str(state["inventory_sha256"]), "maintenance inventory digest"
    )
    evidence_sha = sha256_json(evidence)
    request = {
        "operation": "release_postseal",
        "domain": domain,
        "cohortId": cohort_id,
        "inventorySha256": inventory_sha256,
        "actorSha256": actor_sha256,
        "evidenceSha256": evidence_sha,
    }
    request_sha = sha256_json(request)
    replay = _receipt_replay(
        conn,
        mutation_id=mutation_id,
        request_sha256=request_sha,
    )
    if replay is not None:
        return replay
    if str(state["state"]) != "postseal_pending" or str(state["cohort_id"]) != cohort_id:
        raise CutoverMaintenanceError("postseal maintenance is not releasable")
    timestamp = at or _now()
    conn.execute(
        "UPDATE cutover_maintenance SET state='open',"
        "postseal_evidence_sha256=?,released_at=?,updated_at=? WHERE singleton=1",
        (evidence_sha, timestamp, timestamp),
    )
    result = {
        "schema": "wb.domain-cutover-maintenance/v1",
        "domain": domain,
        "cohortId": cohort_id,
        "inventorySha256": inventory_sha256,
        "mutationId": mutation_id,
        "actorSha256": actor_sha256,
        "state": "open",
        "postsealEvidenceSha256": evidence_sha,
        "evidenceSha256s": evidence,
        "releasedAt": timestamp,
    }
    _record_receipt(
        conn,
        mutation_id=mutation_id,
        operation="release_postseal",
        request_sha256=request_sha,
        result=result,
        created_at=timestamp,
    )
    return result


__all__ = [
    "CutoverMaintenanceError",
    "CutoverMaintenanceFenced",
    "IsolatedRehearsalAuthorization",
    "authorize_isolated_rehearsal_root",
    "mark_postseal_pending",
    "pause_cutover_maintenance",
    "prior_postseal_release_evidence",
    "release_postseal_maintenance",
    "require_isolated_rehearsal_path",
    "require_mutations_open",
    "resume_preseal_maintenance",
    "sha256_json",
]
