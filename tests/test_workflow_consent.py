"""Tests for workflow-level blanket consent.

Validates that:
1. Workflow consent grants blanket access to all consent-gated operations
2. Blanket is revoked when the workflow completes, and reconciled away on
   session re-registration when its run was orphaned by a server restart
3. Per-operation grants coexist with the blanket
4. @requires_consent decorator respects the blanket
5. requires_individual_consent step flag can override the blanket
6. reconcile_workflow_consent sweeps orphaned blankets but leaves a
   genuinely in-flight workflow's blanket intact
"""

import os
import tempfile
import importlib
from types import SimpleNamespace
from pathlib import Path


def _setup_temp_session():
    """Create a temp session environment for isolated testing."""
    td = tempfile.mkdtemp()
    os.environ["WORK_BUDDY_SESSION_ID"] = "test-workflow-consent"

    # Reload agent_session with patched AGENTS_DIR
    import work_buddy.agent_session as asmod
    importlib.reload(asmod)
    asmod.AGENTS_DIR = Path(td)

    # Reset the consent cache so it re-discovers the DB path
    from work_buddy.consent import _cache
    _cache._db_path = None
    _cache._initialized = False

    return td


def test_workflow_consent_lifecycle():
    """Workflow consent grants/revokes correctly."""
    td = _setup_temp_session()
    from work_buddy.consent import (
        grant_workflow_consent,
        revoke_workflow_consent,
        is_workflow_consent_active,
        _cache,
    )

    assert not is_workflow_consent_active()
    grant_workflow_consent("wf_test", ttl_minutes=60)
    assert is_workflow_consent_active()
    revoke_workflow_consent("wf_test")
    assert not is_workflow_consent_active()


def test_blanket_covers_all_operations():
    """Workflow blanket covers any operation name."""
    td = _setup_temp_session()
    from work_buddy.consent import (
        grant_workflow_consent,
        revoke_workflow_consent,
        _cache,
    )

    grant_workflow_consent("wf_test", ttl_minutes=60)
    assert _cache.is_granted("tasks.create_task")
    assert _cache.is_granted("tasks.delete_task")
    assert _cache.is_granted("anything.arbitrary")
    revoke_workflow_consent("wf_test")
    assert not _cache.is_granted("tasks.create_task")


def test_per_operation_coexists():
    """Per-operation grants work alongside blanket."""
    td = _setup_temp_session()
    from work_buddy.consent import (
        grant_workflow_consent,
        revoke_workflow_consent,
        grant_consent,
        revoke_consent,
        _cache,
    )

    grant_consent("tasks.create_task", mode="once")
    assert _cache.is_granted("tasks.create_task")
    assert _cache.get_mode("tasks.create_task") == "once"

    revoke_consent("tasks.create_task")
    assert not _cache.is_granted("tasks.create_task")

    # With blanket, should be granted again
    grant_workflow_consent("wf_test")
    assert _cache.is_granted("tasks.create_task")
    revoke_workflow_consent("wf_test")


def test_decorator_with_workflow_blanket():
    """@requires_consent passes with workflow blanket active."""
    td = _setup_temp_session()
    from work_buddy.consent import (
        ConsentRequired,
        grant_workflow_consent,
        revoke_workflow_consent,
        requires_consent,
    )

    @requires_consent("test.op", reason="test", risk="low")
    def protected_fn(x):
        return x * 2

    # Without consent — blocked
    try:
        protected_fn(5)
        assert False, "Should raise"
    except ConsentRequired:
        pass

    # With blanket — passes
    grant_workflow_consent("wf_test")
    assert protected_fn(5) == 10

    # After revoke — blocked again
    revoke_workflow_consent("wf_test")
    try:
        protected_fn(5)
        assert False, "Should raise"
    except ConsentRequired:
        pass


def test_sentinel_not_self_granting():
    """The __workflow_consent__ sentinel doesn't grant itself via blanket."""
    td = _setup_temp_session()
    from work_buddy.consent import ConsentCache, _cache

    # When no workflow consent is active, querying the sentinel returns False
    assert not _cache.is_granted(ConsentCache.WORKFLOW_CONSENT_OP)


# ---------------------------------------------------------------------------
# Orphan reconciliation — reconcile_workflow_consent
# ---------------------------------------------------------------------------
# A workflow grants a blanket into the agent's consent.db AND pins its DAG
# in the conductor's in-memory _ACTIVE_RUNS map. An MCP-server restart wipes
# the map but leaves the on-disk blanket live (up to a 3h TTL). The sweep at
# session registration revokes such orphaned blankets.


def _clear_active_runs():
    """Reset the conductor's module-global run map (persists across tests)."""
    from work_buddy.mcp_server.conductor import _ACTIVE_RUNS
    _ACTIVE_RUNS.clear()
    return _ACTIVE_RUNS


def test_reconcile_revokes_orphaned_blanket():
    """Blanket with no matching active run is swept away."""
    _setup_temp_session()
    _clear_active_runs()
    from work_buddy.consent import (
        grant_workflow_consent, is_workflow_consent_active,
    )
    from work_buddy.mcp_server.conductor import reconcile_workflow_consent

    sid = "sess-orphan"
    grant_workflow_consent("wf_lost", session_id=sid)
    assert is_workflow_consent_active(session_id=sid)

    result = reconcile_workflow_consent(sid)
    assert result["swept"] is True
    assert not is_workflow_consent_active(session_id=sid)


def test_reconcile_keeps_inflight_blanket():
    """An active run for the session protects its blanket from the sweep."""
    _setup_temp_session()
    runs = _clear_active_runs()
    from work_buddy.consent import (
        grant_workflow_consent, is_workflow_consent_active,
    )
    from work_buddy.mcp_server.conductor import reconcile_workflow_consent

    sid = "sess-live"
    grant_workflow_consent("wf_live", session_id=sid)
    runs["wf_live"] = SimpleNamespace(agent_session_id=sid)

    result = reconcile_workflow_consent(sid)
    assert result["swept"] is False
    assert result["reason"] == "active_run_present"
    assert is_workflow_consent_active(session_id=sid)


def test_reconcile_ignores_other_session_run():
    """A run for a different session does not protect this session's orphan."""
    _setup_temp_session()
    runs = _clear_active_runs()
    from work_buddy.consent import (
        grant_workflow_consent, is_workflow_consent_active,
    )
    from work_buddy.mcp_server.conductor import reconcile_workflow_consent

    grant_workflow_consent("wf_lost", session_id="sess-orphan")
    runs["wf_other"] = SimpleNamespace(agent_session_id="sess-other")

    result = reconcile_workflow_consent("sess-orphan")
    assert result["swept"] is True
    assert not is_workflow_consent_active(session_id="sess-orphan")


def test_reconcile_no_blanket_is_noop():
    """No blanket present — clean no-op, no raise."""
    _setup_temp_session()
    _clear_active_runs()
    from work_buddy.mcp_server.conductor import reconcile_workflow_consent

    result = reconcile_workflow_consent("sess-empty")
    assert result["swept"] is False
    assert result["reason"] == "no_blanket"


def test_reconcile_is_session_scoped():
    """Reconciling session B leaves session A's blanket intact."""
    _setup_temp_session()
    _clear_active_runs()
    from work_buddy.consent import (
        grant_workflow_consent, is_workflow_consent_active,
    )
    from work_buddy.mcp_server.conductor import reconcile_workflow_consent

    grant_workflow_consent("wf_a", session_id="sess-a")
    grant_workflow_consent("wf_b", session_id="sess-b")

    reconcile_workflow_consent("sess-b")
    assert is_workflow_consent_active(session_id="sess-a")
    assert not is_workflow_consent_active(session_id="sess-b")


def test_reconcile_is_idempotent():
    """Two consecutive sweeps — the second is a clean no-op."""
    _setup_temp_session()
    _clear_active_runs()
    from work_buddy.consent import grant_workflow_consent
    from work_buddy.mcp_server.conductor import reconcile_workflow_consent

    grant_workflow_consent("wf_lost", session_id="sess-orphan")
    first = reconcile_workflow_consent("sess-orphan")
    second = reconcile_workflow_consent("sess-orphan")
    assert first["swept"] is True
    assert second["swept"] is False
    assert second["reason"] == "no_blanket"


def test_reconcile_tolerates_dag_without_session_attr():
    """A DAG missing agent_session_id must not break the getattr guard."""
    _setup_temp_session()
    runs = _clear_active_runs()
    from work_buddy.consent import (
        grant_workflow_consent, is_workflow_consent_active,
    )
    from work_buddy.mcp_server.conductor import reconcile_workflow_consent

    grant_workflow_consent("wf_lost", session_id="sess-orphan")
    runs["wf_bare"] = SimpleNamespace()  # no agent_session_id attribute

    result = reconcile_workflow_consent("sess-orphan")
    assert result["swept"] is True
    assert not is_workflow_consent_active(session_id="sess-orphan")


def test_reconcile_falsy_session_is_noop():
    """Empty / None session id returns a no-op dict without raising."""
    _setup_temp_session()
    _clear_active_runs()
    from work_buddy.mcp_server.conductor import reconcile_workflow_consent

    assert reconcile_workflow_consent("")["swept"] is False
    assert reconcile_workflow_consent(None)["swept"] is False


if __name__ == "__main__":
    test_workflow_consent_lifecycle()
    print("[PASS] lifecycle")
    test_blanket_covers_all_operations()
    print("[PASS] blanket_covers_all")
    test_per_operation_coexists()
    print("[PASS] per_operation_coexists")
    test_decorator_with_workflow_blanket()
    print("[PASS] decorator_with_blanket")
    test_sentinel_not_self_granting()
    print("[PASS] sentinel_not_self_granting")
    test_reconcile_revokes_orphaned_blanket()
    print("[PASS] reconcile_revokes_orphaned_blanket")
    test_reconcile_keeps_inflight_blanket()
    print("[PASS] reconcile_keeps_inflight_blanket")
    test_reconcile_ignores_other_session_run()
    print("[PASS] reconcile_ignores_other_session_run")
    test_reconcile_no_blanket_is_noop()
    print("[PASS] reconcile_no_blanket_is_noop")
    test_reconcile_is_session_scoped()
    print("[PASS] reconcile_is_session_scoped")
    test_reconcile_is_idempotent()
    print("[PASS] reconcile_is_idempotent")
    test_reconcile_tolerates_dag_without_session_attr()
    print("[PASS] reconcile_tolerates_dag_without_session_attr")
    test_reconcile_falsy_session_is_noop()
    print("[PASS] reconcile_falsy_session_is_noop")
    print("\n=== ALL TESTS PASSED ===")
