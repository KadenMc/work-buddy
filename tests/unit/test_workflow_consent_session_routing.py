"""Regression: workflow consent paths route through the agent's session DB.

This locks in the contract that the workflow consent layer threads the
agent's ``session_id`` explicitly down to ``is_granted_in_session`` /
``_connect(session_id=...)``, rather than relying on the process's
``WORK_BUDDY_SESSION_ID`` env-var fallback.

The decorator-gate bug class fixed in PR #138 specifically exploited
the env-var fallback inside ``@requires_consent``'s ``is_granted``:
grants written to ``agent_<agent_sid>/consent.db`` were invisible to a
gate running inside the MCP server (env-var = ``sidecar-<bootstrap>``).
The workflow consent paths don't suffer from the same class because
every check accepts an explicit ``session_id`` and forwards it to the
storage layer. These tests prove that property by simulating the
cross-session scenario: the env-var (= "current process session") and
the agent's session id are different, and we assert grants/lookups
route to the agent's DB regardless of the env-var.

If a future refactor accidentally reintroduces an env-var-fallback path
inside the workflow consent code (e.g. by calling ``is_granted`` without
``session_id``), these tests will fail.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def cross_session_state(tmp_agents_dir, monkeypatch):
    """Reset consent state and restore canonical ``get_agents_dir``.

    Set the env-var ``WORK_BUDDY_SESSION_ID`` to a synthetic "process"
    session id distinct from the agent session id used in each test —
    this is what mimics the production reality where the MCP server's
    bootstrap session differs from the agent's session.
    """
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

    # Simulate the bootstrap-session env that the MCP server runs under.
    PROCESS_SID = "test-process-bootstrap"
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", PROCESS_SID)
    return PROCESS_SID


# ---------------------------------------------------------------------------
# Workflow class authorization
# ---------------------------------------------------------------------------


def test_workflow_class_authorized_finds_grant_in_passed_session(cross_session_state):
    """``_is_workflow_class_authorized(..., session_id=agent_sid)`` finds a
    grant in the agent's DB even when the process env-var points at a
    different session.

    Falsifiable failure mode (what a regression would look like): the
    helper drops the ``session_id`` parameter or forwards ``None`` to
    ``is_granted``, the lookup falls back to the env-var session, finds
    nothing, returns ``False``.
    """
    from work_buddy.consent import grant_workflow_class
    from work_buddy.mcp_server.tools.gateway import _is_workflow_class_authorized

    # Session ids must differ in their first 8 chars — the short_id used to
    # identify session directories on disk.
    AGENT_SID = "agentaaa-9474f4c7-routing"
    # Mint a class grant in the AGENT's session DB, not the process env's.
    grant_workflow_class("audit-target", ttl_minutes=15, session_id=AGENT_SID)

    # Caller is the gateway, which knows the agent's session id from ctx.
    # The audit conclusion says this lookup must succeed.
    assert _is_workflow_class_authorized(
        "audit-target", session_id=AGENT_SID,
    ) is True


def test_workflow_class_lookup_is_session_isolated(cross_session_state):
    """A class grant in session A is NOT visible from session B.

    Sanity check: if the helper accidentally read from a shared cache or
    fell back to the env-var session, the grant in session A would
    incorrectly authorize a B-scoped lookup.
    """
    from work_buddy.consent import grant_workflow_class
    from work_buddy.mcp_server.tools.gateway import _is_workflow_class_authorized

    # Session ids differ in their first 8 chars (short_id used for disk dirs).
    SESSION_A = "aaaa1111-test-agent-routing"
    SESSION_B = "bbbb2222-test-agent-routing"

    grant_workflow_class("audit-target", ttl_minutes=15, session_id=SESSION_A)

    assert _is_workflow_class_authorized(
        "audit-target", session_id=SESSION_A,
    ) is True
    assert _is_workflow_class_authorized(
        "audit-target", session_id=SESSION_B,
    ) is False


# ---------------------------------------------------------------------------
# start_workflow run-grant minting
# ---------------------------------------------------------------------------


def test_start_workflow_mints_run_grant_in_agent_session(cross_session_state):
    """``start_workflow(name, params, agent_session_id)`` writes the
    ``workflow_run:<name>:<run_id>`` grant into the agent's DB.

    Verified via ``list_active_workflow_grants(session_id=agent_sid)`` —
    if the grant landed in the process env-var session instead, this
    list would be empty for the agent and the workflow's sub-operations
    would not pick up the run-grant carry inside the agent's wb_run
    dispatch.

    Note: the workflow must exist in the registry. We use ``task-new``
    (a registered moderate-risk workflow). The test does not need the
    workflow's steps to run — only the registration + grant-minting
    side-effects of ``start_workflow``.
    """
    from work_buddy.mcp_server.conductor import start_workflow, _ACTIVE_RUNS
    from work_buddy.consent import list_active_workflow_grants

    AGENT_SID = "mintroute-agent-routing-test"

    # No grants for this session before the call.
    pre = list_active_workflow_grants(session_id=AGENT_SID)
    assert all(
        not entry["operation"].startswith("workflow_run:task-new:")
        for entry in pre.get("run", [])
    ), "pre-condition: no task-new run grants for this agent session"

    try:
        result = start_workflow("task-new", {}, agent_session_id=AGENT_SID)
    except Exception as exc:
        pytest.skip(f"task-new not registered or has unmet deps: {exc}")
        return

    if "error" in result:
        pytest.skip(f"start_workflow returned error: {result['error']}")
        return

    run_id = result.get("workflow_run_id")
    assert run_id, "expected workflow_run_id in result"

    # The run grant must be queryable in the AGENT's session — not the
    # process env-var session.
    post = list_active_workflow_grants(session_id=AGENT_SID)
    run_keys = [e["operation"] for e in post.get("run", [])]
    assert f"workflow_run:task-new:{run_id}" in run_keys, (
        f"expected workflow_run:task-new:{run_id} in agent session DB, "
        f"got run_keys={run_keys}"
    )

    # And the process env-var session's DB must NOT have received it.
    PROCESS_SID = cross_session_state
    process_post = list_active_workflow_grants(session_id=PROCESS_SID)
    process_run_keys = [e["operation"] for e in process_post.get("run", [])]
    assert f"workflow_run:task-new:{run_id}" not in process_run_keys, (
        "regression: run grant landed in the process env-var session DB "
        "instead of (or in addition to) the agent's session DB"
    )

    # Cleanup: drop the active run.
    _ACTIVE_RUNS.pop(run_id, None)


# ---------------------------------------------------------------------------
# DAG pin survives advance_workflow
# ---------------------------------------------------------------------------


def test_advance_workflow_updates_dag_session_pin(cross_session_state):
    """``advance_workflow(run_id, ..., agent_session_id=new_sid)``
    updates the DAG's pinned session id in place.

    This is the path that handles MCP-restart resumption: the agent's
    session id may change across restarts, but the workflow run lives on
    in ``_ACTIVE_RUNS``. The conductor must keep ``dag.agent_session_id``
    in sync with the freshest agent session so subsequent grant
    mints/revokes land in the right DB.

    Falsifiable failure mode: the conductor reads ``dag.agent_session_id``
    but never updates it on resumption, so a re-pinned session id is
    ignored and grants leak to the old DB.
    """
    from work_buddy.mcp_server.conductor import (
        start_workflow, advance_workflow, _ACTIVE_RUNS,
    )

    INITIAL_SID = "initialA-test-agent-routing"
    RESUMED_SID = "resumedB-test-agent-routing"

    try:
        result = start_workflow("task-new", {}, agent_session_id=INITIAL_SID)
    except Exception as exc:
        pytest.skip(f"task-new not registered or has unmet deps: {exc}")
        return

    if "error" in result:
        pytest.skip(f"start_workflow returned error: {result['error']}")
        return

    run_id = result.get("workflow_run_id")
    assert run_id, "expected workflow_run_id in result"

    dag = _ACTIVE_RUNS.get(run_id)
    assert dag is not None
    assert getattr(dag, "agent_session_id", None) == INITIAL_SID

    # Advance with a fresh session id (simulating MCP restart resumption).
    # No step_result; the advance just touches the DAG.
    advance_workflow(run_id, step_result=None, agent_session_id=RESUMED_SID)
    assert getattr(dag, "agent_session_id", None) == RESUMED_SID, (
        "regression: advance_workflow did not update the DAG's pinned "
        "agent_session_id when a fresher session id was passed"
    )

    # Cleanup.
    _ACTIVE_RUNS.pop(run_id, None)


# ---------------------------------------------------------------------------
# subprocess_runner payload routing
# ---------------------------------------------------------------------------


def test_execute_auto_run_payload_includes_agent_session(monkeypatch, cross_session_state):
    """``_execute_auto_run`` passes the DAG-pinned agent session id to
    the subprocess, NOT the env-var bootstrap session.

    We intercept ``subprocess.run`` so the test doesn't actually spawn a
    process — only the payload shape matters. Assert that the JSON
    payload's ``session_id`` field carries the agent's session id we
    threaded through.

    Falsifiable failure mode: ``_execute_auto_run`` drops
    ``agent_session_id`` and always reads ``os.environ`` → subprocess
    runs under the bootstrap session → consent checks inside the
    subprocess resolve to the wrong DB.
    """
    import json
    import subprocess as _sub
    from work_buddy.mcp_server import conductor as cmod

    AGENT_SID = "autorun1-payload-test-agent"
    captured = {}

    class _FakeProc:
        returncode = 0
        stdout = '{"success": true, "value": null}'
        stderr = ""

    def _fake_run(cmd, *, input=None, **kwargs):
        captured["input"] = input
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(_sub, "run", _fake_run)
    monkeypatch.setattr(cmod.subprocess, "run", _fake_run)

    spec = {
        "callable": "work_buddy.consent.list_consents",
        "kwargs": {},
        "timeout": 5,
    }
    cmod._execute_auto_run(
        "test_step",
        spec,
        step_results={},
        agent_session_id=AGENT_SID,
        initial_params=None,
    )

    assert "input" in captured, "subprocess.run was not invoked"
    payload = json.loads(captured["input"])
    assert payload.get("session_id") == AGENT_SID, (
        f"regression: subprocess payload session_id={payload.get('session_id')!r} "
        f"!= passed agent_session_id={AGENT_SID!r}. The bootstrap env-var "
        "leaked through where the agent's session should have been used."
    )
