"""Unit test for the auto_run session-routing fix.

Verifies that when an agent starts a workflow, the agent's session_id is
threaded through the conductor into the subprocess payload — so consent
checks inside auto_run hit the agent's consent.db, not the MCP server's
own bootstrap session.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch, MagicMock

import pytest

from work_buddy.mcp_server import conductor
from work_buddy.process import ProcessTimeout, Scripted


class _FakeCompletedProcess:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout: str = '{"success": true, "value": null}', stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_execute_auto_run_uses_agent_session_when_provided(monkeypatch, recording_runner):
    """When agent_session_id is given, it wins over the process env var."""
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "sidecar-xyz")

    recording_runner.script(stdout='{"success": true, "value": null}')

    result = conductor._execute_auto_run(
        step_id="dummy",
        spec={"callable": "work_buddy.obsidian.tasks.store.counts_by_state", "kwargs": {}, "timeout": 5},
        step_results={},
        agent_session_id="agent-abc123",
    )

    assert result["success"] is True
    [launch] = recording_runner.launches
    payload = json.loads(launch.input)
    assert payload["session_id"] == "agent-abc123", (
        "subprocess payload must carry the agent's session id, not the "
        "MCP server's env-var session — this is the fix for the consent "
        "bug where auto_run read the wrong consent.db"
    )


def test_workflow_blanket_grant_and_revoke_on_agent_db(monkeypatch, tmp_path):
    """grant/revoke_workflow_consent(session_id=...) must hit the agent's
    consent.db, not the MCP server's default session."""
    from work_buddy.agent_session import get_session_dir
    from work_buddy.consent import (
        grant_workflow_consent, revoke_workflow_consent, ConsentCache,
    )
    from work_buddy.obsidian.tasks import store  # noqa: F401 ensure imports ok

    # Redirect agents dir to a tmp location so this test doesn't touch
    # real session DBs on disk.
    import work_buddy.paths as paths

    orig = paths.data_dir

    def _fake_data_dir(category: str = ""):
        base = tmp_path / "data"
        base.mkdir(parents=True, exist_ok=True)
        sub = base / category if category else base
        sub.mkdir(parents=True, exist_ok=True)
        return sub

    monkeypatch.setattr(paths, "data_dir", _fake_data_dir)
    # agent_session imports data_dir at module load; patch its reference too
    import work_buddy.agent_session as asmod
    monkeypatch.setattr(asmod, "data_dir", _fake_data_dir)

    agent_sid = "agent-xyz-12345678"

    # Grant workflow consent for this session
    grant_workflow_consent("wf_test1", ttl_minutes=60, session_id=agent_sid)

    # The session's consent.db should now contain __workflow_consent__
    agent_db = get_session_dir(agent_sid) / "consent.db"
    assert agent_db.exists()
    import sqlite3
    conn = sqlite3.connect(str(agent_db))
    rows = conn.execute(
        "SELECT operation FROM grants WHERE operation = ?",
        (ConsentCache.WORKFLOW_CONSENT_OP,),
    ).fetchall()
    conn.close()
    assert rows, "workflow blanket should be written to the agent's DB"

    # Revoke it
    revoke_workflow_consent("wf_test1", session_id=agent_sid)

    conn = sqlite3.connect(str(agent_db))
    rows = conn.execute(
        "SELECT operation FROM grants WHERE operation = ?",
        (ConsentCache.WORKFLOW_CONSENT_OP,),
    ).fetchall()
    conn.close()
    assert not rows, "revoke with session_id must remove from the agent's DB"


def test_execute_auto_run_falls_back_to_env_when_no_session(monkeypatch, recording_runner):
    """Legacy path: if no agent_session_id, fall back to env — preserves
    behavior for non-workflow callers that might adopt auto_run later."""
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "sidecar-xyz")

    recording_runner.script(stdout='{"success": true, "value": null}')

    result = conductor._execute_auto_run(
        step_id="dummy",
        spec={"callable": "work_buddy.obsidian.tasks.store.counts_by_state", "kwargs": {}, "timeout": 5},
        step_results={},
        # agent_session_id omitted
    )

    assert result["success"] is True
    [launch] = recording_runner.launches
    payload = json.loads(launch.input)
    assert payload["session_id"] == "sidecar-xyz"


# NOTE: we don't mock start_workflow end-to-end here because building
# a fake WorkflowDefinition is involved and brittle. The two subprocess
# tests above cover the critical plumbing — if _execute_auto_run respects
# the agent_session_id kwarg, and start_workflow stores it on the DAG,
# and advance_workflow reads dag.agent_session_id, the chain holds.
# The live E2E verification in session transcripts covers that path.


# ---------------------------------------------------------------------------
# Transient-timeout retry
# ---------------------------------------------------------------------------
#
# When a subprocess hits ``subprocess.TimeoutExpired``, the conductor retries
# the call once before failing the step. This absorbs transient host
# contention (cold imports, concurrent registry rebuilds, antivirus scans).
# Crashes, invalid-JSON outputs, and other failure modes never retry — they
# signal real bugs. Per-step ``retry_on_timeout: false`` disables the retry
# for non-idempotent callables (git commits, outbound message sends).


def _spec(retry_on_timeout: bool = True) -> dict:
    """Minimal AutoRun spec for these tests."""
    return {
        "callable": "work_buddy.obsidian.tasks.store.counts_by_state",
        "kwargs": {},
        "timeout": 5,
        "retry_on_timeout": retry_on_timeout,
    }


def _timeout_exc(stderr: str = "") -> ProcessTimeout:
    return ProcessTimeout(
        ["python", "-m", "subprocess_runner"], 5, stderr=stderr,
    )


def test_execute_auto_run_retries_once_on_timeout(recording_runner):
    """First attempt times out; second returns success — overall success."""
    calls = {"n": 0}

    def _respond(launch):
        calls["n"] += 1
        if calls["n"] == 1:
            return Scripted(raises=_timeout_exc())
        return Scripted(stdout='{"success": true, "value": null}')

    recording_runner.script(respond=_respond)

    result = conductor._execute_auto_run(
        step_id="dummy", spec=_spec(), step_results={},
    )

    assert calls["n"] == 2, "transient timeout should be retried once"
    assert result["success"] is True


def test_execute_auto_run_fails_after_second_timeout(recording_runner):
    """Both attempts time out; step fails with a timeout error."""
    recording_runner.script(raises=_timeout_exc())

    result = conductor._execute_auto_run(
        step_id="dummy", spec=_spec(), step_results={},
    )

    assert len(recording_runner.launches) == 2, "both attempts must run before the step fails"
    assert result["success"] is False
    assert "timed out" in result["error"]
    assert "2 attempts" in result["error"], (
        "error message should disclose that retries were exhausted"
    )


def test_execute_auto_run_respects_retry_on_timeout_false(recording_runner):
    """retry_on_timeout=false makes the timeout terminal on the first try."""
    recording_runner.script(raises=_timeout_exc())

    result = conductor._execute_auto_run(
        step_id="dummy",
        spec=_spec(retry_on_timeout=False),
        step_results={},
    )

    assert len(recording_runner.launches) == 1, "opt-out must skip the retry"
    assert result["success"] is False
    assert "timed out" in result["error"]


def test_execute_auto_run_does_not_retry_on_crash(recording_runner):
    """A non-timeout failure (subprocess crash) is terminal — no retry."""
    recording_runner.script(
        returncode=1, stdout="", stderr="Traceback (most recent call last):\nBoom",
    )

    result = conductor._execute_auto_run(
        step_id="dummy", spec=_spec(), step_results={},
    )

    assert len(recording_runner.launches) == 1, "crashes signal real bugs and must not retry"
    assert result["success"] is False
    assert "crashed" in result["error"]


# ---------------------------------------------------------------------------
# Degenerate timeout fallback
# ---------------------------------------------------------------------------
#
# run_tool requires a finite, positive timeout. A step's spec can carry
# timeout 0 or an explicit null (YAML `null`), which run_tool would reject.
# _execute_auto_run must keep launching those two cases the way it did
# before it adopted run_tool for the normal case, instead of raising.


@pytest.mark.parametrize("invalid_timeout", [0, None, -5, "30", True])
def test_execute_auto_run_uses_the_default_for_an_invalid_timeout(
    recording_runner, caplog, invalid_timeout,
):
    """Every auto_run step is bounded: a spec timeout that is not a positive
    number runs under the default, with a warning, instead of failing."""
    recording_runner.script(stdout='{"success": true, "value": null}')

    with caplog.at_level(logging.WARNING, logger="work_buddy.mcp_server.conductor"):
        result = conductor._execute_auto_run(
            step_id="dummy",
            spec={
                "callable": "work_buddy.obsidian.tasks.store.counts_by_state",
                "kwargs": {},
                "timeout": invalid_timeout,
            },
            step_results={},
        )

    assert result["success"] is True
    [launch] = recording_runner.launches
    assert launch.timeout == conductor._AUTO_RUN_DEFAULT_TIMEOUT_S
    assert "not a positive number of seconds" in caplog.text
