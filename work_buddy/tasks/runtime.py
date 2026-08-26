"""Read-only authority routing plus the durable native-authority latch.

Native activation writes a small latch outside SQLite before its transaction
commits. A crash can therefore leave a safe fail-closed state, but can never
make a missing task database silently route writes back to legacy task files.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from work_buddy.agent_session import get_originating_session
from work_buddy.paths import resolve

from .store import default_task_db_path


NATIVE_AUTHORITY_EPOCHS = frozenset({"native", "active"})
AUTHORITY_LATCH_SCHEMA = "wb.task-authority-latch/v1"
TASK_MUTATION_CAPABILITIES = frozenset(
    {
        "task_archive",
        "task_assign",
        "task_change_state",
        "task_create",
        "task_delete",
        "task_proposals_reconcile",
        "task_set_tags",
        "task_sync",
        "task_toggle",
        "task_update_description",
    }
)


@dataclass(frozen=True, slots=True)
class AuthorityLatch:
    """Validated durable intent for one native authority activation."""

    cohort_id: str
    target_authority_epoch: str
    cutover_receipt_id: str
    database_path_sha256: str
    armed_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": AUTHORITY_LATCH_SCHEMA,
            "cohort_id": self.cohort_id,
            "target_authority_epoch": self.target_authority_epoch,
            "cutover_receipt_id": self.cutover_receipt_id,
            "database_path_sha256": self.database_path_sha256,
            "armed_at": self.armed_at,
        }


@dataclass(frozen=True, slots=True)
class _AuthorityState:
    authority_epoch: str
    rollback_fence: bool


def _canonical_default_latch_path() -> Path:
    """Use a data-root anchor that does not move with ``tasks.db_path``."""

    # Keep this outside ``db/`` as well: an operator may relocate that whole
    # directory while repairing storage. The data-root itself is the durable
    # installation identity; changing it is an explicit installation move.
    return resolve("db/tasks").parent.parent / "task_authority_latch.json"


def authority_latch_path(path: str | Path | None = None) -> Path:
    """Resolve the installation latch or an isolated store's sibling latch."""

    if path is None:
        return _canonical_default_latch_path()
    target = Path(path)
    return target.with_name(f".{target.name}.authority-latch.json")


def activation_authority_latch_path(database_path: str | Path) -> Path:
    """Choose the installation latch only for the configured task database."""

    target = Path(database_path).expanduser().resolve(strict=False)
    configured = default_task_db_path().expanduser().resolve(strict=False)
    return authority_latch_path(None if target == configured else target)


def _database_path_sha256(path: str | Path) -> str:
    normalized = os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_latch(value: Mapping[str, Any]) -> AuthorityLatch:
    from .errors import TaskAuthorityUnavailable

    try:
        schema = str(value["schema"])
        latch = AuthorityLatch(
            cohort_id=str(value["cohort_id"]).strip(),
            target_authority_epoch=str(value["target_authority_epoch"]).strip(),
            cutover_receipt_id=str(value["cutover_receipt_id"]).strip(),
            database_path_sha256=str(value["database_path_sha256"]).strip(),
            armed_at=str(value["armed_at"]).strip(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskAuthorityUnavailable() from exc
    digest = latch.database_path_sha256
    if (
        schema != AUTHORITY_LATCH_SCHEMA
        or not latch.cohort_id
        or not latch.cutover_receipt_id
        or not latch.armed_at
        or not latch.target_authority_epoch.startswith("native:")
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise TaskAuthorityUnavailable()
    return latch


def _read_latch_file(path: Path) -> AuthorityLatch | None:
    from .errors import TaskAuthorityUnavailable

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TaskAuthorityUnavailable() from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskAuthorityUnavailable() from exc
    if not isinstance(value, Mapping):
        raise TaskAuthorityUnavailable()
    return _parse_latch(value)


def _candidate_latch_paths(target: Path, *, explicit_path: bool) -> tuple[Path, ...]:
    # Resolve the latch with the same rule used by activation.  In particular,
    # passing the configured database path explicitly must not switch authority
    # verification from the installation latch to an optional sibling file.
    # ``explicit_path`` remains part of the private signature so older callers
    # cannot accidentally select a different verification policy.
    del explicit_path
    return (activation_authority_latch_path(target),)


def _read_authority_latch(
    target: Path,
    *,
    explicit_path: bool,
) -> AuthorityLatch | None:
    found = [
        latch
        for candidate in _candidate_latch_paths(target, explicit_path=explicit_path)
        if (latch := _read_latch_file(candidate)) is not None
    ]
    if not found:
        return None
    first = found[0]
    if any(latch != first for latch in found[1:]):
        from .errors import TaskAuthorityUnavailable

        raise TaskAuthorityUnavailable()
    return first


def _fsync_parent(path: Path) -> None:
    """Best-effort directory flush after replacing or deleting a latch."""

    flags = getattr(os, "O_RDONLY", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path.parent, flags)
    except OSError:
        # Windows does not permit opening a directory this way. The marker
        # file itself is still flushed before os.replace.
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def arm_native_authority_latch(
    database_path: str | Path,
    *,
    cohort_id: str,
    target_authority_epoch: str,
    cutover_receipt_id: str,
    armed_at: str,
) -> Path:
    """Atomically persist native authority intent before SQLite activation.

    Replays with the same stable activation identity are accepted. A stale,
    malformed, or different latch is never overwritten automatically.
    """

    target = Path(database_path)
    latch_path = activation_authority_latch_path(target)
    requested = AuthorityLatch(
        cohort_id=str(cohort_id).strip(),
        target_authority_epoch=str(target_authority_epoch).strip(),
        cutover_receipt_id=str(cutover_receipt_id).strip(),
        database_path_sha256=_database_path_sha256(target),
        armed_at=str(armed_at).strip(),
    )
    _parse_latch(requested.to_dict())
    existing = _read_latch_file(latch_path)
    if existing is not None:
        stable_existing = (
            existing.cohort_id,
            existing.target_authority_epoch,
            existing.cutover_receipt_id,
            existing.database_path_sha256,
        )
        stable_requested = (
            requested.cohort_id,
            requested.target_authority_epoch,
            requested.cutover_receipt_id,
            requested.database_path_sha256,
        )
        if stable_existing != stable_requested:
            from .errors import TaskAuthorityUnavailable

            raise TaskAuthorityUnavailable()
        return latch_path

    latch_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            requested.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    temporary = latch_path.with_name(f".{latch_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, latch_path)
        _fsync_parent(latch_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return latch_path


def clear_pending_authority_latch(
    database_path: str | Path,
    *,
    cohort_id: str,
    target_authority_epoch: str,
) -> None:
    """Remove only a matching, never-committed activation latch.

    The migration abort path calls this only after SQLite has durably recorded
    the cohort as aborted at its legacy epoch. A crash before this unlink is
    fail closed; an abort replay can safely finish the cleanup.
    """

    latch_path = activation_authority_latch_path(database_path)
    existing = _read_latch_file(latch_path)
    if existing is None:
        return
    if (
        existing.cohort_id != str(cohort_id)
        or existing.target_authority_epoch != str(target_authority_epoch)
        or existing.database_path_sha256 != _database_path_sha256(database_path)
    ):
        from .errors import TaskAuthorityUnavailable

        raise TaskAuthorityUnavailable()
    try:
        latch_path.unlink()
    except FileNotFoundError:
        return
    _fsync_parent(latch_path)


def _authority_state(path: str | Path | None = None) -> _AuthorityState | None:
    """Read and cross-check SQLite authority with the external latch."""

    explicit_path = path is not None
    target = Path(path) if explicit_path else default_task_db_path()
    latch = _read_authority_latch(target, explicit_path=explicit_path)
    if not target.is_file():
        if latch is not None:
            from .errors import TaskAuthorityUnavailable

            raise TaskAuthorityUnavailable()
        return None
    try:
        uri = target.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT authority_epoch, rollback_fence "
                "FROM task_system_state WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        if "no such table" in str(exc).casefold() and latch is None:
            return None
        from .errors import TaskAuthorityUnavailable

        raise TaskAuthorityUnavailable() from exc
    except OSError as exc:
        from .errors import TaskAuthorityUnavailable

        raise TaskAuthorityUnavailable() from exc
    if row is None:
        from .errors import TaskAuthorityUnavailable

        raise TaskAuthorityUnavailable()

    state = _AuthorityState(
        authority_epoch=str(row[0] or "legacy"),
        rollback_fence=bool(row[1]),
    )
    if is_native_authority_epoch(state.authority_epoch) and latch is None:
        # SQLite is not allowed to vouch for its own authority.  The durable
        # external latch is the independent installation-level proof that the
        # native epoch was intentionally activated.
        from .errors import TaskAuthorityUnavailable

        raise TaskAuthorityUnavailable()
    if latch is not None and (
        latch.database_path_sha256 != _database_path_sha256(target)
        or latch.target_authority_epoch != state.authority_epoch
    ):
        # This includes marker-before-commit crash state: SQLite still says
        # legacy while the marker records pending native intent.
        from .errors import TaskAuthorityUnavailable

        raise TaskAuthorityUnavailable()
    return state


def authority_epoch(path: str | Path | None = None) -> str:
    """Read the current authority epoch without creating or migrating a DB."""

    state = _authority_state(path)
    return "legacy" if state is None else state.authority_epoch


def is_native_authority_epoch(value: object) -> bool:
    epoch = str(value or "").casefold()
    return epoch in NATIVE_AUTHORITY_EPOCHS or epoch.startswith("native:")


def native_authority_active(path: str | Path | None = None) -> bool:
    return is_native_authority_epoch(authority_epoch(path))


def mutation_fence_active(path: str | Path | None = None) -> bool:
    """Read the durable cutover fence without creating or migrating a DB."""

    state = _authority_state(path)
    return False if state is None else state.rollback_fence


def assert_task_mutations_allowed(path: str | Path | None = None) -> None:
    """Fail closed when authority is unavailable or its write fence is armed."""

    if mutation_fence_active(path):
        from .errors import TaskMutationFenced

        raise TaskMutationFenced()


def native_task_mutation_authority(path: str | Path | None = None) -> bool:
    """Atomically route a mutation from one authority-state snapshot.

    Unlike calling ``assert_task_mutations_allowed`` and
    ``native_authority_active`` separately, this cannot observe a fence from
    one ledger generation and an epoch from another.
    """

    state = _authority_state(path)
    if state is not None and state.rollback_fence:
        from .errors import TaskMutationFenced

        raise TaskMutationFenced()
    epoch = "legacy" if state is None else state.authority_epoch
    return is_native_authority_epoch(epoch)


def is_task_mutation_capability(name: object) -> bool:
    """Return whether an MCP capability can mutate task authority."""

    return str(name or "") in TASK_MUTATION_CAPABILITIES


def _authority_family(epoch: str | None) -> str:
    normalized = str(epoch or "legacy").casefold()
    if is_native_authority_epoch(normalized):
        return "native"
    return "legacy"


def assert_task_replay_authority(
    recorded_epoch: str | None,
    *,
    path: str | Path | None = None,
) -> None:
    """Keep durable task retries on the authority family that recorded them.

    Old operation records predate the authority stamp and are therefore
    treated as legacy.  That preserves pre-cutover retry behaviour while
    making an unstamped operation fail closed once native authority is live.
    Native epoch-number changes remain compatible because they address the
    same neutral store and receipt ledger.
    """

    if str(recorded_epoch or "").casefold() == "unavailable":
        from .errors import TaskReplayAuthorityMismatch

        raise TaskReplayAuthorityMismatch()
    current = authority_epoch(path)
    if _authority_family(recorded_epoch) != _authority_family(current):
        from .errors import TaskReplayAuthorityMismatch

        raise TaskReplayAuthorityMismatch()


def originating_session() -> str | None:
    return (
        get_originating_session()
        or os.environ.get("WORK_BUDDY_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
    )


def mutation_actor() -> str:
    session_id = originating_session()
    return f"agent:{session_id}" if session_id else "service:task-compatibility"


def new_client_mutation_id(operation: str, supplied: str | None = None) -> str:
    value = str(supplied or "").strip()
    if value:
        return value
    return f"compat:{operation}:{uuid.uuid4().hex}"


__all__ = [
    "AUTHORITY_LATCH_SCHEMA",
    "AuthorityLatch",
    "NATIVE_AUTHORITY_EPOCHS",
    "TASK_MUTATION_CAPABILITIES",
    "activation_authority_latch_path",
    "arm_native_authority_latch",
    "assert_task_mutations_allowed",
    "assert_task_replay_authority",
    "authority_epoch",
    "authority_latch_path",
    "clear_pending_authority_latch",
    "is_task_mutation_capability",
    "is_native_authority_epoch",
    "mutation_actor",
    "mutation_fence_active",
    "native_authority_active",
    "native_task_mutation_authority",
    "new_client_mutation_id",
    "originating_session",
]
