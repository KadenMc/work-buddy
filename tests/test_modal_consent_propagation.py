"""Regression test for the modal-fallback ``consent_grant`` routing
contract.

The Obsidian plugin's modal "respond" path does two things:
  1. Stash the user's choice in plugin memory (for the active poll).
  2. Post a ``consent_grant`` message to the messaging service (for
     out-of-band resolution if the gateway poll already timed out).

The sidecar's ``MessagePoller`` must route that message through
``resolve_consent_request`` (which honors the notification's
``callback_session_id``) and NOT through the generic capability
dispatch (which would write the grant to the sidecar process's own
session DB via the singleton consent cache, leaving the agent's DB
empty and any subsequent ``@requires_consent`` check raising).

Combined with the bundle-unbundle behavior of
``resolve_consent_request``, the underlying operation gates land in
the agent's DB on individual op names.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


def _setup_isolation(tmp_path: Path, monkeypatch) -> Path:
    """Point agent_session at tmp_path so we can inspect per-session DBs
    written by both the simulated "sidecar" and the "agent" sessions.
    """
    import work_buddy.agent_session as asmod
    monkeypatch.setattr(asmod, "get_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(asmod, "_cached_session_dir", None)

    from work_buddy.consent import _cache
    monkeypatch.setattr(_cache, "_db_path", None)
    monkeypatch.setattr(_cache, "_initialized", False)
    return tmp_path


def _read_grants(session_dir: Path) -> dict[str, str]:
    """Read the consent grants table for a session, return {op: mode}."""
    db = session_dir / "consent.db"
    if not db.exists():
        return {}
    conn = sqlite3.connect(str(db), timeout=2)
    try:
        rows = conn.execute(
            "SELECT operation, mode FROM grants"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {op: mode for op, mode in rows}


def _find_session_dir(agents_root: Path, session_id: str) -> Path | None:
    """Find the session directory for a given session id."""
    short = session_id[:8]
    for child in agents_root.iterdir():
        if child.is_dir() and child.name.endswith(f"_{short}"):
            return child
    return None


def test_modal_consent_grant_lands_in_agent_session_db(
    tmp_path: Path, monkeypatch,
) -> None:
    """Simulate the modal-fallback messaging path: create a notification
    targeting an agent session, send the ``consent_grant`` message
    body the plugin would post (including ``notification_id``), drive
    the sidecar's handler, then assert the grant landed in the AGENT's
    session DB — not the current process's.
    """
    agents_root = _setup_isolation(tmp_path, monkeypatch)

    # The sidecar/MCP-server process runs under one session...
    os.environ["WORK_BUDDY_SESSION_ID"] = "sidecar-bootstrap-session"

    # ...while the agent that requested consent runs under a different one.
    agent_session_id = "agent-target-session"
    # Pre-create the agent's session dir so the consent DB write has a home.
    import work_buddy.agent_session as asmod
    asmod.get_session_dir(agent_session_id)

    # Create the consent notification (as the gateway would).
    from work_buddy.consent import create_consent_request
    record = create_consent_request(
        operation="bundle:test_capability",
        reason="test bundle",
        risk="moderate",
        default_ttl=30,
        requester="gateway:test_capability",
        context={
            "capability": "test_capability",
            "operations": ["test.op_a", "test.op_b"],
            "operation_id": "op_test123",
        },
        callback_session_id=agent_session_id,
    )
    notification_id = record["notification_id"]

    # Simulate the plugin's modal-fallback message landing in the
    # messaging system. Plugin sends:
    #   subject="consent_grant"
    #   body=JSON of {operation, mode, ttl_minutes, notification_id}
    body = json.dumps({
        "operation": "bundle:test_capability",
        "mode": "temporary",
        "ttl_minutes": 30,
        "notification_id": notification_id,
    })

    # Drive the sidecar router's consent_grant special case directly.
    # (Avoids the full MessagePoller / messaging-service dependency.)
    from work_buddy.sidecar.dispatch.router import _handle_consent_grant_message
    result = _handle_consent_grant_message(body)

    assert result["status"] == "ok", f"Handler errored: {result}"

    # The grant should be in the AGENT'S session DB, not the sidecar's.
    agent_dir = _find_session_dir(agents_root, agent_session_id)
    assert agent_dir is not None, "Agent session dir not created"
    agent_grants = _read_grants(agent_dir)

    # The individual unbundled ops must be granted in the AGENT's DB,
    # not the sidecar's. Bundle key presence is incidental — what
    # matters is that the gates actually pass.
    assert "test.op_a" in agent_grants, (
        f"test.op_a should be granted in agent's session DB. "
        f"Grants found: {agent_grants}"
    )
    assert "test.op_b" in agent_grants, (
        f"test.op_b should be granted in agent's session DB. "
        f"Grants found: {agent_grants}"
    )

    # And the sidecar session's DB must NOT have these grants (they'd
    # be invisible to the agent's @requires_consent decorators).
    sidecar_dir = _find_session_dir(agents_root, "sidecar-bootstrap-session")
    if sidecar_dir is not None:
        sidecar_grants = _read_grants(sidecar_dir)
        assert "test.op_a" not in sidecar_grants, (
            f"test.op_a leaked into sidecar session DB. "
            f"Sidecar grants: {sidecar_grants}"
        )


def test_modal_consent_grant_missing_notification_id_falls_back(
    tmp_path: Path, monkeypatch,
) -> None:
    """When the message body lacks ``notification_id`` (out-of-sync
    plugin), the handler writes the grant to the current process's DB
    and logs a warning. The grant won't unblock the agent (wrong DB),
    but the path doesn't crash — the operator-facing fix is to rebuild
    + reload the Obsidian plugin.
    """
    _setup_isolation(tmp_path, monkeypatch)
    os.environ["WORK_BUDDY_SESSION_ID"] = "legacy-no-nid-session"

    body = json.dumps({
        "operation": "legacy.thing",
        "mode": "once",
    })

    from work_buddy.sidecar.dispatch.router import _handle_consent_grant_message
    result = _handle_consent_grant_message(body)

    assert result["status"] == "ok", (
        f"Handler should accept legacy bodies. Got: {result}"
    )

    # The grant lands in whichever session the process is currently
    # running as — the safety-net fallback when notification_id is
    # missing.
    from work_buddy.consent import _cache
    assert _cache.is_granted("legacy.thing")
