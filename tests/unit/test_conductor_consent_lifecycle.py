"""Integration tests — conductor lifecycle calls the composable consent
primitives correctly.

Asserts that ``start_workflow`` mints the run grant,
``_build_complete_response`` revokes it on completion, and
``cascade_revoke_workflow`` propagates a class-grant revoke to every
in-flight run.

Uses a stubbed ``WorkflowDefinition`` so the conductor's full lifecycle
path is exercised, not just the consent primitives in isolation.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def cache(tmp_agents_dir, monkeypatch):
    import work_buddy.agent_session as asmod
    def _canonical_get_agents_dir():
        return asmod.data_dir("agents")
    monkeypatch.setattr(asmod, "get_agents_dir", _canonical_get_agents_dir)
    monkeypatch.setattr(asmod, "_cached_session_dir", None)

    from work_buddy.consent import _cache
    _cache._db_path = None
    _cache._initialized = False

    from work_buddy import consent as cmod
    cmod._LEGACY_BLANKET_LOGGED.clear()
    yield _cache

    # ``ConsentCache`` retains its resolved SQLite path process-wide.  Do not
    # leave it pinned to this test's temporary agents directory (or to the
    # workflow grant written there) after ``tmp_agents_dir`` is torn down.
    _cache._db_path = None
    _cache._initialized = False


@pytest.fixture
def stub_workflow(monkeypatch):
    """Stub ``registry.get_entry`` to return a one-step WorkflowDefinition
    for the name 'test-wf'. The step is a no-op reasoning step.
    """
    from work_buddy.mcp_server import registry, conductor

    from work_buddy.mcp_server.registry import (
        WorkflowDefinition, WorkflowStep,
    )

    wf = WorkflowDefinition(
        name="test-wf",
        description="Stub workflow for lifecycle test.",
        workflow_file="",
        execution="main",
        steps=[
            WorkflowStep(
                id="only",
                name="Only step",
                instruction="(no-op)",
                step_type="reasoning",
            )
        ],
    )
    monkeypatch.setattr(
        registry, "get_entry",
        lambda name: wf if name == "test-wf" else None,
    )
    # ``conductor`` imported ``get_entry`` into its module namespace; patch
    # that reference too so ``start_workflow``'s call site uses the stub.
    monkeypatch.setattr(
        conductor, "get_entry",
        lambda name: wf if name == "test-wf" else None,
    )
    # Reset _ACTIVE_RUNS so the lifecycle test sees a clean conductor.
    with conductor._ACTIVE_RUNS_LOCK:
        conductor._ACTIVE_RUNS.clear()
    return wf


# ---------------------------------------------------------------------------
# start_workflow mints the workflow_run grant
# ---------------------------------------------------------------------------


def test_start_workflow_mints_run_grant(cache, stub_workflow):
    from work_buddy.mcp_server import conductor
    from work_buddy.consent import is_workflow_authorized

    result = conductor.start_workflow("test-wf", params=None, agent_session_id=None)

    run_id = result["workflow_run_id"]
    ok, via = is_workflow_authorized("test-wf", run_id)
    assert ok is True
    assert via == "run"


def test_start_workflow_pins_workflow_name_on_dag(cache, stub_workflow):
    """Lifecycle hooks read ``dag.workflow_name`` for the class name; the
    pin happens in start_workflow."""
    from work_buddy.mcp_server import conductor

    result = conductor.start_workflow("test-wf", params=None, agent_session_id=None)
    run_id = result["workflow_run_id"]

    with conductor._ACTIVE_RUNS_LOCK:
        dag = conductor._ACTIVE_RUNS[run_id]
    assert getattr(dag, "workflow_name", None) == "test-wf"


# ---------------------------------------------------------------------------
# Run completion revokes the run grant
# ---------------------------------------------------------------------------


def test_complete_workflow_revokes_run_grant(cache, stub_workflow):
    from work_buddy.mcp_server import conductor
    from work_buddy.consent import is_workflow_authorized

    result = conductor.start_workflow("test-wf", params=None, agent_session_id=None)
    run_id = result["workflow_run_id"]

    # Complete the only step.
    advance_result = conductor.advance_workflow(
        run_id, step_result={"ok": True},
    )
    # Should be workflow_complete.
    assert advance_result.get("type") == "workflow_complete"

    ok, via = is_workflow_authorized("test-wf", run_id)
    assert ok is False
    assert via is None


# ---------------------------------------------------------------------------
# cascade_revoke_workflow walks _ACTIVE_RUNS
# ---------------------------------------------------------------------------


def test_cascade_revoke_workflow_revokes_class_and_runs(cache, stub_workflow):
    """Class revoke + cascade walks _ACTIVE_RUNS to revoke each matching
    run grant in the same session.
    """
    from work_buddy.mcp_server import conductor
    from work_buddy.consent import (
        grant_workflow_class, is_workflow_authorized,
    )

    # Mint a class grant first (would normally happen via gateway pre-flight).
    grant_workflow_class("test-wf", ttl_minutes=15)

    # Start two runs of the same workflow.
    r1 = conductor.start_workflow("test-wf", params=None, agent_session_id=None)
    r2 = conductor.start_workflow("test-wf", params=None, agent_session_id=None)
    rid1 = r1["workflow_run_id"]
    rid2 = r2["workflow_run_id"]

    # Both runs are authorized via the run grant.
    assert is_workflow_authorized("test-wf", rid1)[0] is True
    assert is_workflow_authorized("test-wf", rid2)[0] is True

    # Cascade revoke.
    result = conductor.cascade_revoke_workflow("test-wf", session_id=None)

    assert result["revoked_class"] is True
    assert set(result["revoked_runs"]) == {rid1, rid2}

    # Both run grants AND class grant are gone.
    ok1, _ = is_workflow_authorized("test-wf", rid1)
    ok2, _ = is_workflow_authorized("test-wf", rid2)
    assert ok1 is False
    assert ok2 is False


# ---------------------------------------------------------------------------
# reconcile_workflow_consent cleans up orphaned workflow_run keys
# ---------------------------------------------------------------------------


def test_reconcile_revokes_orphaned_workflow_run_key(cache, stub_workflow):
    """A workflow_run:* key without a matching active run gets swept."""
    from work_buddy.mcp_server import conductor
    from work_buddy.consent import (
        grant_workflow_run, list_active_workflow_grants,
    )

    # Hand-mint a workflow_run key for a run that doesn't exist in _ACTIVE_RUNS.
    grant_workflow_run("test-wf", "wf_orphaned")

    # Pre-state: key exists.
    snap = list_active_workflow_grants()
    run_names = {(e["workflow_name"], e["run_id"]) for e in snap["run"]}
    assert ("test-wf", "wf_orphaned") in run_names

    # Reconcile against the current (default) session.
    import os
    sid = os.environ["WORK_BUDDY_SESSION_ID"]
    result = conductor.reconcile_workflow_consent(sid)

    # The orphan was swept (the legacy-blanket path returns 'no_blanket'
    # but the run sweep happens additively).
    assert "orphaned_run_keys" in result

    # Post-state: key gone.
    snap_after = list_active_workflow_grants()
    run_names_after = {(e["workflow_name"], e["run_id"]) for e in snap_after["run"]}
    assert ("test-wf", "wf_orphaned") not in run_names_after


# ---------------------------------------------------------------------------
# Headless (sidecar-scheduled) runs: isolated session + TTL ceiling
# ---------------------------------------------------------------------------


def _read_grant_row(session_id, operation):
    """Return (mode, expires_at) for a grant in a session's DB, or None."""
    import sqlite3
    from work_buddy.agent_session import (
        get_session_dir, get_session_consent_db_path,
    )
    db = get_session_consent_db_path(get_session_dir(session_id))
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT mode, expires_at FROM grants WHERE operation = ?",
            (operation,),
        ).fetchone()
    except sqlite3.OperationalError:
        # No grants table at all — the session DB was never written to,
        # which for our purposes is equivalent to "no such grant".
        return None
    finally:
        conn.close()


def test_headless_run_uses_isolated_session_with_ttl(cache, stub_workflow):
    """A headless run is pinned to a dedicated ``sidecar-run-<id>`` session and
    its run grant is TTL-bounded — never landing in the sidecar standing DB."""
    from work_buddy.mcp_server import conductor
    from work_buddy.consent import _workflow_run_key

    result = conductor.start_workflow("test-wf", params=None, headless=True)
    run_id = result["workflow_run_id"]

    with conductor._ACTIVE_RUNS_LOCK:
        dag = conductor._ACTIVE_RUNS[run_id]
    assert dag.agent_session_id == f"{run_id}-srun"

    op = _workflow_run_key("test-wf", run_id)
    # Lives in the isolated per-run session, TTL-bounded ("temporary").
    row = _read_grant_row(dag.agent_session_id, op)
    assert row is not None, "run grant missing from the isolated run session"
    mode, expires_at = row
    assert mode == "temporary"
    assert expires_at is not None  # finite ceiling, not a forever grant

    # And NOT in the sidecar's standing (default) session DB.
    assert _read_grant_row(None, op) is None


def test_non_headless_run_grant_is_untimed_in_caller_session(cache, stub_workflow):
    """A normal (non-headless) run keeps the untimed mode=once run grant in the
    caller's session — unchanged behavior."""
    from work_buddy.mcp_server import conductor
    from work_buddy.consent import _workflow_run_key

    result = conductor.start_workflow("test-wf", params=None)
    run_id = result["workflow_run_id"]

    row = _read_grant_row(None, _workflow_run_key("test-wf", run_id))
    assert row == ("once", None)
