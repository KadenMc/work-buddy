"""Remote-session ops — launch and resume visible Claude Code sessions.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). These callables wrap
``work_buddy.session_launcher`` for the Remote Control (phone app) flow.
"""

from __future__ import annotations

from pathlib import Path

from work_buddy.mcp_server.op_registry import register_op

# The ``work_buddy`` package directory — the default cwd for session listing,
# matching the legacy builder's resolution (registry.py's parent.parent).
_PKG_DIR = Path(__file__).resolve().parents[2]


def remote_session_begin(
    cwd: str | None = None,
    prompt: str | None = None,
    session_id: str | None = None,
    session_name: str | None = None,
    bypass_permissions: bool = True,
) -> dict:
    """Launch or resume a visible Claude Code session in a real terminal."""
    from work_buddy.session_launcher import begin_session

    return begin_session(
        cwd=cwd, prompt=prompt,
        session_id=session_id, session_name=session_name,
        bypass_permissions=bypass_permissions,
    )


def session_resume(
    session_id: str,
    cwd: str | None = None,
    bypass_permissions: bool = True,
) -> dict:
    """Resume an existing session in a new local terminal (no remote-control)."""
    # Validate up-front: begin_session falls back to a bare ``claude --resume``
    # if resolution fails, which spawns a useless terminal.
    from work_buddy.session_launcher import begin_session
    from work_buddy.sessions.inspector import resolve_session_id

    try:
        resolved = resolve_session_id(session_id)
    except FileNotFoundError as exc:
        return {"status": "error", "error": str(exc)}
    return begin_session(
        cwd=cwd,
        session_id=resolved,
        bypass_permissions=bypass_permissions,
        remote_control=False,
    )


def remote_session_list(cwd: str | None = None) -> dict:
    """List resumable Claude Code sessions from ~/.claude/sessions/."""
    from work_buddy.session_launcher import list_resumable_sessions

    sessions = list_resumable_sessions(cwd=cwd or str(_PKG_DIR))
    return {
        "sessions": sessions[:20],  # Cap at 20
        "total": len(sessions),
        "cwd_filter": cwd,
    }


def _register() -> None:
    register_op("op.wb.remote_session_begin", remote_session_begin)
    register_op("op.wb.session_resume", session_resume)
    register_op("op.wb.remote_session_list", remote_session_list)


_register()
