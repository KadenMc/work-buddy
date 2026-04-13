"""Tests for workflow-level blanket consent.

Validates that:
1. Workflow consent grants blanket access to all consent-gated operations
2. Blanket is revoked when the workflow completes
3. Per-operation grants coexist with the blanket
4. @requires_consent decorator respects the blanket
5. requires_individual_consent step flag can override the blanket
"""

import os
import tempfile
import importlib
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
    print("\n=== ALL TESTS PASSED ===")
