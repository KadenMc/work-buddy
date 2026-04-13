"""Agent session identity and directory management.

Each Claude session gets its own directory under agents/ with:
- manifest.json     — full session metadata (session ID, timestamps, etc.)
- consent.db        — SQLite consent grants for this session
- consent_audit.log — audit trail for this session
- context/          — context bundle files for this session

The directory name is: <timestamp>_<session_id_first_8>
The full session ID is stored in manifest.json for complete traceability.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from work_buddy.paths import data_dir, repo_root

# Cache the resolved session dir within a single Python process
_cached_session_dir: Path | None = None


def _get_session_id() -> str:
    """Get the current Claude Code session ID from environment.

    Requires WORK_BUDDY_SESSION_ID to be set. This is a hard requirement —
    no fallback, no auto-discovery. Each agent session must set this
    before running any work_buddy code.

    Raises RuntimeError if not set.
    """
    session_id = os.environ.get("WORK_BUDDY_SESSION_ID")
    if not session_id:
        raise RuntimeError(
            "WORK_BUDDY_SESSION_ID environment variable is not set.\n"
            "\n"
            "The SessionStart hook should have output your session ID in the\n"
            "conversation context as: WORK_BUDDY_SESSION_ID=<uuid>\n"
            "\n"
            "If you see it in your context, run:\n"
            '  export WORK_BUDDY_SESSION_ID="<uuid-from-hook-output>"\n'
            "\n"
            "If the hook didn't fire, discover your session ID manually:\n"
            "  Look in your OS temp directory under claude/ for this project\n"
            '  export WORK_BUDDY_SESSION_ID="<your-session-id>"\n'
        )
    return session_id


def get_agents_dir() -> Path:
    """Return the agents/ directory under data/, creating it if needed."""
    return data_dir("agents")


def get_session_dir(session_id: str | None = None) -> Path:
    """Get or create the current session's agent directory.

    If session_id is not provided, reads from WORK_BUDDY_SESSION_ID
    environment variable (required — raises RuntimeError if not set).

    Creates the directory and manifest.json if they don't exist.

    Returns the session directory path.
    """
    global _cached_session_dir

    # Only use the cache when no explicit session_id is requested —
    # the MCP gateway calls with explicit agent session IDs that differ
    # from the server's own session.
    if session_id is None:
        if _cached_session_dir is not None and _cached_session_dir.exists():
            return _cached_session_dir
        session_id = _get_session_id()

    agents_dir = get_agents_dir()
    short_id = session_id[:8]

    # Check if a directory already exists for this session
    for existing in agents_dir.iterdir():
        if existing.is_dir() and existing.name.endswith(f"_{short_id}"):
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
    }
    manifest_path = session_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    _cached_session_dir = session_dir
    return session_dir


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

    Artifacts are stored globally under ``data/context/`` (managed by the
    artifact store) rather than buried inside the session directory.  The
    session's ``artifacts.jsonl`` ledger records a reference.

    The directory name includes the session short-ID for provenance::

        data/context/<short-id>_<YYYYMMDD-HHMMSS>/
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
