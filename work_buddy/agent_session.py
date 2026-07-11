"""Agent session identity and directory management.

Each agent-harness session gets its own directory under agents/ with:
- manifest.json     — full session metadata (session ID, timestamps, etc.)
- consent.db        — SQLite consent grants for this session
- consent_audit.log — audit trail for this session
- context/          — context bundle files for this session

The directory name is: <timestamp>_<session_id_first_8>
The full session ID is stored in manifest.json for complete traceability.
"""

import json
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from work_buddy.paths import data_dir, repo_root

# Cache the resolved session dir within a single Python process
_cached_session_dir: Path | None = None


# ---------------------------------------------------------------------------
# Originating-session context var
# ---------------------------------------------------------------------------
# When a capability is executed asynchronously by the sidecar (e.g. the
# retry sweep replaying a queued llm_submit job), the thread running the
# callable is not the originating agent's thread. To route per-session
# artifacts like the LLM cost log back to the agent who requested the
# work, the caller sets this context var before invoking the callable;
# cost/ledger code reads it to pick the right session directory.
_originating_session: ContextVar[str | None] = ContextVar(
    "wb_originating_session", default=None,
)


def set_originating_session(session_id: str | None) -> Any:
    """Set the originating session for the current context.

    Returns an opaque token that must be passed to
    ``reset_originating_session`` to restore the previous value.
    """
    return _originating_session.set(session_id)


def reset_originating_session(token: Any) -> None:
    """Restore a previous originating-session value from its token."""
    _originating_session.reset(token)


def get_originating_session() -> str | None:
    """Return the current originating session id, or None when unset."""
    return _originating_session.get()


def _get_session_id() -> str:
    """Get the current native agent session ID from environment.

    Generated hooks expose ``WORK_BUDDY_SESSION_ID`` for compatibility. Codex
    also provides its native ``CODEX_THREAD_ID`` to every local command, so it
    is a deterministic fallback when the hook subprocess cannot mutate the
    parent agent process environment.

    Raises RuntimeError if not set.
    """
    session_id = os.environ.get("WORK_BUDDY_SESSION_ID") or os.environ.get(
        "CODEX_THREAD_ID"
    )
    if not session_id:
        raise RuntimeError(
            "No agent session identity is available.\n"
            "\n"
            "The SessionStart hook should have output your session ID in the\n"
            "conversation context as WORK_BUDDY_SESSION_ID=<uuid>, or Codex\n"
            "should provide CODEX_THREAD_ID automatically.\n"
            "\n"
            "If you see it in your context, run:\n"
            '  export WORK_BUDDY_SESSION_ID="<uuid-from-hook-output>"\n'
            "\n"
            "If the hook didn't fire, discover your session ID manually:\n"
            "  Inspect your harness's native session metadata for this project\n"
            '  export WORK_BUDDY_SESSION_ID="<your-session-id>"\n'
        )
    return session_id


def get_agents_dir() -> Path:
    """Return the agents/ directory under the data root, creating it if needed."""
    return data_dir("agents")


def get_session_dir(session_id: str | None = None) -> Path:
    """Get or create the current session's agent directory.

    If session_id is not provided, reads from WORK_BUDDY_SESSION_ID or the
    native Codex CODEX_THREAD_ID fallback.

    Creates the directory and manifest.json if they don't exist.

    Returns the session directory path.
    """
    global _cached_session_dir

    # Only use AND populate the cache when no explicit session_id is
    # requested. The MCP gateway / conductor call with explicit session IDs
    # (agent sessions, per-run sidecar sessions) that differ from this
    # process's own session — caching one of those as the process default
    # would misroute subsequent default-session lookups to it. The cache
    # must track only the process's own (env-derived) session.
    is_default = session_id is None
    if is_default:
        if _cached_session_dir is not None and _cached_session_dir.exists():
            return _cached_session_dir
        session_id = _get_session_id()

    agents_dir = get_agents_dir()
    short_id = session_id[:8]

    # Check if a directory already exists for this session
    for existing in agents_dir.iterdir():
        if existing.is_dir() and existing.name.endswith(f"_{short_id}"):
            if is_default:
                _cached_session_dir = existing
            return existing

    # Create new session directory
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    dir_name = f"{timestamp}_{short_id}"
    session_dir = agents_dir / dir_name
    session_dir.mkdir(parents=True, exist_ok=True)

    # Write manifest
    manifest = {
        "session_id": session_id,
        "short_id": short_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "directory": dir_name,
        "project": str(repo_root()),
        "entrypoint": os.environ.get("CLAUDE_CODE_ENTRYPOINT", "unknown"),
        "harness_id": os.environ.get("WORK_BUDDY_HARNESS_ID")
        or ("codexcli" if os.environ.get("CODEX_THREAD_ID") else "unknown"),
    }
    manifest_path = session_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if is_default:
        _cached_session_dir = session_dir
    return session_dir


def update_manifest(session_id: str | None = None, **updates: Any) -> dict[str, Any]:
    """Update a session's manifest with additional fields.

    Reads the existing manifest, merges ``updates``, writes back, and returns
    the merged dict. ``session_id`` selects the target session (defaults to
    this process's own env-derived session).
    """
    session_dir = get_session_dir(session_id)
    manifest_path = session_dir / "manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(updates)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def get_active_modes(session_id: str | None = None) -> set[str]:
    """Return the set of active mode ids for a session.

    Reads the ``active_modes`` list from the session manifest (defaulting to
    this process's own session). Returns an empty set when the manifest is
    missing, unreadable, or declares no modes. Modes gate capability and
    workflow availability — see ``work_buddy/modes/`` and ``available_when``.
    """
    session_dir = get_session_dir(session_id)
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return set()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {str(m) for m in (manifest.get("active_modes") or [])}


def set_active_modes(modes: set[str], session_id: str | None = None) -> None:
    """Persist the active mode ids for a session (stored as a sorted list)."""
    update_manifest(active_modes=sorted(modes), session_id=session_id)


def get_session_consent_db_path(session_dir: Path | None = None) -> Path:
    """Return the path to the session-scoped consent SQLite DB."""
    if session_dir is None:
        session_dir = get_session_dir()
    return session_dir / "consent.db"


def get_session_audit_path(session_dir: Path | None = None) -> Path:
    """Return the path to the session's audit log."""
    if session_dir is None:
        session_dir = get_session_dir()
    return session_dir / "consent_audit.log"


def get_consent_requests_dir() -> Path:
    """Return the shared consent requests directory, creating it if needed.

    This is NOT per-session — it's shared across all processes so that
    async consent requests are visible to all frontends (Obsidian modal,
    Telegram, etc.) and all backend processes (MCP server, sidecar).
    """
    requests_dir = get_agents_dir() / "consent" / "requests"
    requests_dir.mkdir(parents=True, exist_ok=True)
    return requests_dir


def get_session_context_dir(session_dir: Path | None = None) -> Path:
    """Return a new timestamped context bundle directory for this session.

    Artifacts are stored globally under ``<data_root>/context/`` (managed
    by the artifact store) rather than buried inside the session directory.
    The session's ``artifacts.jsonl`` ledger records a reference.

    The directory name includes the session short-ID for provenance::

        <data_root>/context/<short-id>_<YYYYMMDD-HHMMSS>/
    """
    if session_dir is None:
        session_dir = get_session_dir()

    from datetime import datetime
    from work_buddy.paths import data_dir

    short_id = session_dir.name.split("_")[-1]  # last segment is short-id
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ctx_dir = data_dir("context") / f"{short_id}_{timestamp}"
    ctx_dir.mkdir(parents=True, exist_ok=True)

    # Record to session artifact ledger
    try:
        from work_buddy.artifacts import get_store
        session_id = _get_session_id()
        get_store().record_to_session_ledger(
            session_id, f"context-dir:{short_id}_{timestamp}"
        )
    except Exception:
        pass  # best-effort — don't break collection if ledger fails

    return ctx_dir


def list_sessions() -> list[dict[str, Any]]:
    """List all agent sessions with their metadata.

    Returns list of manifest dicts, sorted by creation time (newest first).
    """
    agents_dir = get_agents_dir()
    sessions = []
    for entry in sorted(agents_dir.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
                manifest["path"] = str(entry)
                sessions.append(manifest)
            except (json.JSONDecodeError, OSError):
                continue
    return sessions


# ---------------------------------------------------------------------------
# Lifecycle registration — agent-sessions artifact
# ---------------------------------------------------------------------------
#
# Registers a DirectoryTreeStorage(SESSION_DIRS) + MtimeWindow(created_at,
# 14d, activity_check) + Delete + SessionTagged artifact under
# "agent-sessions". The activity_check defers eviction when the session
# has files modified within the cutoff window — same compound rule as
# the original prune_stale_sessions.

import logging as _logging
_logger = _logging.getLogger(__name__)


def _agent_sessions_recent_activity(record: dict, cutoff) -> bool:
    """Return True if the session has had recent file activity."""
    from work_buddy.artifacts.expiry import _parse_to_utc

    latest = _parse_to_utc(record.get("_latest_mtime", ""))
    if latest is None:
        return False
    return latest >= cutoff


def _register_agent_sessions_artifact() -> None:
    try:
        from work_buddy.artifacts import (
            Artifact,
            Delete,
            DirectoryTreeStorage,
            DirShape,
            Lifecycle,
            MtimeWindow,
            register_artifact,
            SessionTagged,
        )

        register_artifact(Artifact(
            name="agent-sessions",
            storage=DirectoryTreeStorage(
                root=get_agents_dir(),
                shape=DirShape.SESSION_DIRS,
                manifest_filename="manifest.json",
                artifact_name="agent-sessions",
            ),
            lifecycle=Lifecycle(
                trigger=MtimeWindow(
                    mtime_field="created_at",
                    max_age_days=14,
                    activity_check=_agent_sessions_recent_activity,
                ),
                action=Delete(),
            ),
            provenance=SessionTagged(session_field="session_id"),
        ))
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("Failed to register agent-sessions artifact: %s", exc)


_register_agent_sessions_artifact()
