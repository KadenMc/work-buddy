"""Principal-scoped consent resolution — the WHO axis.

These tests prove the fix for the consent-bypass where an interactive agent's
``is_granted`` check resolved against the *sidecar's* session DB (the process
default) and rode a stale ``workflow_run:*`` blanket that wasn't the agent's.

The model: every check binds exactly one ``ConsentPrincipal`` naming the
session DB to resolve against. ``HUMAN_AGENT`` / ``SIDECAR`` ride their own
workflow grants; ``REPLAY`` rides individual op-grants only (no workflow
time-travel). With no principal bound, resolution falls back to the legacy
process-default path (or denies when fail-closed is enabled).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from work_buddy import agent_session, consent
from work_buddy.consent import ConsentCache
from work_buddy.consent_principal import (
    consent_principal,
    human_agent,
    replay_of,
    sidecar_self,
)


def _write_grant(
    db_path: Path,
    operation: str,
    *,
    mode: str = "once",
    expires_at: str | None = None,
) -> None:
    """Hand-write a grant row into a specific session's consent.db."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS grants (
              operation TEXT PRIMARY KEY,
              mode TEXT NOT NULL,
              granted_at TEXT NOT NULL,
              expires_at TEXT
           )"""
    )
    conn.execute(
        "INSERT OR REPLACE INTO grants (operation, mode, granted_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (operation, mode, datetime.now(timezone.utc).isoformat(), expires_at),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """Two session DBs: the sidecar (== process default) and a fresh agent.

    Mirrors production: the process runs as the sidecar, so the cache's
    default DB (``session_id=None``) IS the sidecar's.
    """
    agents = tmp_path / "agents"
    sidecar_sid = "sidecar-deadbeef"
    agent_sid = "agent-1111-2222-3333"
    sidecar_dir = agents / "sidecar_dir"
    agent_dir = agents / "agent_dir"
    sidecar_dir.mkdir(parents=True)
    agent_dir.mkdir(parents=True)
    by_sid = {sidecar_sid: sidecar_dir, agent_sid: agent_dir}

    def _fake_get_session_dir(session_id=None):
        if session_id is None:
            return sidecar_dir  # process default == sidecar
        if session_id in by_sid:
            return by_sid[session_id]
        d = agents / f"u_{session_id[:8]}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _fake_db(session_dir=None):
        return (session_dir or sidecar_dir) / "consent.db"

    def _fake_get_session_id():
        return sidecar_sid  # this process IS the sidecar

    monkeypatch.setattr(agent_session, "get_session_dir", _fake_get_session_dir)
    monkeypatch.setattr(
        agent_session, "get_session_consent_db_path", _fake_db,
    )
    monkeypatch.setattr(agent_session, "_get_session_id", _fake_get_session_id)

    return {
        "sidecar_sid": sidecar_sid,
        "agent_sid": agent_sid,
        "sidecar_db": sidecar_dir / "consent.db",
        "agent_db": agent_dir / "consent.db",
    }


@pytest.fixture
def cache():
    return ConsentCache()


@pytest.fixture(autouse=True)
def _reset_fail_closed():
    """Ensure the module flag never leaks between tests."""
    consent.set_fail_closed_no_principal(False)
    yield
    consent.set_fail_closed_no_principal(False)


# ---------------------------------------------------------------------------
# THE bug reproduction
# ---------------------------------------------------------------------------


def test_stale_sidecar_blanket_does_not_authorize_agent(sessions, cache):
    """A stale workflow_run blanket in the sidecar DB must NOT authorize an
    agent's moderate op. This is the exact incident: ``task_archive`` ran with
    no prompt because the sidecar DB held a months-old morning-routine grant.
    """
    # Stale, never-revoked workflow_run grant in the SIDECAR DB.
    _write_grant(
        sessions["sidecar_db"],
        "workflow_run:morning-routine:wf_april",
        mode="once",
        expires_at=None,
    )
    # The agent's own DB is empty.

    # Bound to the agent principal → resolves against the (empty) agent DB.
    granted = cache.is_granted(
        "tasks.archive",
        consent_weight="moderate",
        principal=human_agent(sessions["agent_sid"]),
    )
    assert granted is False, (
        "agent op was authorized by a stale grant in the sidecar's DB — "
        "the principal scoping failed"
    )


def test_contrast_legacy_no_principal_would_ride_the_blanket(sessions, cache):
    """Documents WHY the principal fix matters: with NO principal bound, the
    legacy path resolves against the process-default (sidecar) DB and DOES ride
    the blanket. The gateway binding a principal is what closes the hole.
    """
    _write_grant(
        sessions["sidecar_db"],
        "workflow_run:morning-routine:wf_april",
        mode="once",
        expires_at=None,
    )
    # No principal bound → legacy default-DB resolution (== sidecar DB here).
    assert cache.is_granted("tasks.archive", consent_weight="moderate") is True


# ---------------------------------------------------------------------------
# Principal-scoped resolution
# ---------------------------------------------------------------------------


def test_agent_principal_resolves_against_agent_db(sessions, cache):
    """An individual grant in the agent's DB authorizes; the sidecar DB is
    never consulted."""
    _write_grant(sessions["agent_db"], "tasks.archive", mode="always")
    _write_grant(
        sessions["sidecar_db"], "workflow_run:x:wf_1", mode="once",
    )
    assert cache.is_granted(
        "tasks.archive",
        consent_weight="moderate",
        principal=human_agent(sessions["agent_sid"]),
    ) is True


def test_agent_owns_workflow_carry(sessions, cache):
    """A live workflow_run grant in the AGENT's own DB carries the agent's
    moderate op (the agent rides its own workflow)."""
    _write_grant(
        sessions["agent_db"], "workflow_run:task-new:wf_live", mode="once",
    )
    assert cache.is_granted(
        "tasks.create_task",
        consent_weight="moderate",
        principal=human_agent(sessions["agent_sid"]),
    ) is True


def test_replay_suppresses_workflow_carry(sessions, cache):
    """A REPLAY principal does NOT ride a workflow grant (no time-travel) but
    DOES ride an individual grant."""
    _write_grant(
        sessions["agent_db"], "workflow_run:task-new:wf_live", mode="once",
    )
    assert cache.is_granted(
        "tasks.create_task",
        consent_weight="moderate",
        principal=replay_of(sessions["agent_sid"]),
    ) is False

    _write_grant(sessions["agent_db"], "tasks.create_task", mode="always")
    assert cache.is_granted(
        "tasks.create_task",
        consent_weight="moderate",
        principal=replay_of(sessions["agent_sid"]),
    ) is True


def test_sidecar_principal_resolves_against_sidecar_db(sessions, cache):
    """Role B: the sidecar's own standing grant resolves against the sidecar
    DB; an identical grant in an agent DB is invisible to the sidecar."""
    _write_grant(sessions["sidecar_db"], "sidecar:agent_spawn", mode="always")
    assert cache.is_granted(
        "sidecar:agent_spawn", principal=sidecar_self(),
    ) is True

    # The same op granted only in an agent DB does NOT satisfy the sidecar.
    fresh = ConsentCache()
    assert fresh.is_granted(
        "sidecar:agent_spawn", principal=human_agent(sessions["agent_sid"]),
    ) is False


def test_high_weight_never_carries_under_any_principal(sessions, cache):
    """A high-weight op bypasses workflow-carry regardless of principal."""
    _write_grant(
        sessions["agent_db"], "workflow_run:task-new:wf_live", mode="once",
    )
    assert cache.is_granted(
        "obsidian.eval_js",
        consent_weight="high",
        principal=human_agent(sessions["agent_sid"]),
    ) is False


def test_get_mode_is_principal_scoped(sessions, cache):
    """get_mode resolves against the bound principal's DB."""
    _write_grant(sessions["agent_db"], "tasks.archive", mode="once")
    assert cache.get_mode(
        "tasks.archive", principal=human_agent(sessions["agent_sid"]),
    ) == "once"
    # Sidecar principal sees nothing.
    assert cache.get_mode(
        "tasks.archive", principal=sidecar_self(),
    ) is None


def test_context_manager_binds_principal(sessions, cache):
    """is_granted with no explicit principal picks up the one bound by
    consent_principal() — the mechanism the gateway uses around dispatch."""
    _write_grant(sessions["agent_db"], "tasks.archive", mode="always")
    with consent_principal(human_agent(sessions["agent_sid"])):
        assert cache.is_granted(
            "tasks.archive", consent_weight="moderate",
        ) is True
    # Outside the context, no principal → legacy default (sidecar) DB → absent.
    assert cache.is_granted("tasks.archive", consent_weight="moderate") is False


# ---------------------------------------------------------------------------
# No-principal policy
# ---------------------------------------------------------------------------


def test_no_principal_legacy_fallback_default(sessions, cache):
    """Default policy: no principal → legacy default-DB resolution."""
    _write_grant(sessions["sidecar_db"], "tasks.archive", mode="always")
    assert cache.is_granted("tasks.archive", consent_weight="moderate") is True


def test_fail_closed_no_principal(sessions, cache):
    """With fail-closed enabled, a check with no principal denies even if the
    default DB has a matching grant."""
    _write_grant(sessions["sidecar_db"], "tasks.archive", mode="always")
    consent.set_fail_closed_no_principal(True)
    assert cache.is_granted("tasks.archive", consent_weight="moderate") is False
    # A bound principal still resolves normally under fail-closed.
    assert cache.is_granted(
        "tasks.archive",
        consent_weight="moderate",
        principal=sidecar_self(),  # sidecar_db has the grant
    ) is True
