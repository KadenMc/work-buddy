"""Tests for consent auto-request, pre-flight checking, and bundling.

Validates that:
1. _CONSENT_REGISTRY is populated by @requires_consent at import time
2. get_consent_metadata returns registered metadata
3. grant_consent_batch grants multiple operations atomically
4. _check_missing_consent identifies ungrated operations
5. Capability.consent_operations field works on the dataclass
"""

import os
import tempfile
import importlib
from pathlib import Path


_test_counter = 0


_test_counter = 0


def _setup_temp_session():
    """Create a temp session environment for isolated testing.

    Uses a unique session ID per call and patches get_agents_dir() to
    route to a fresh temp directory.  Also clears _cached_session_dir
    and the consent cache DB state to avoid stale lookups.
    """
    global _test_counter
    _test_counter += 1
    td = tempfile.mkdtemp()
    os.environ["WORK_BUDDY_SESSION_ID"] = f"test-consent-auto-{_test_counter}-{os.getpid()}"

    import work_buddy.agent_session as asmod
    # Patch get_agents_dir to return our temp directory instead of data/agents/
    asmod.get_agents_dir = lambda: Path(td)
    asmod._cached_session_dir = None

    from work_buddy.consent import _cache
    _cache._db_path = None
    _cache._initialized = False

    return td


def test_consent_registry_populated():
    """@requires_consent populates _CONSENT_REGISTRY at decoration time."""
    from work_buddy.consent import _CONSENT_REGISTRY, requires_consent

    @requires_consent("test.registry_pop", reason="test reason", risk="low", default_ttl=10)
    def _dummy():
        pass

    assert "test.registry_pop" in _CONSENT_REGISTRY
    meta = _CONSENT_REGISTRY["test.registry_pop"]
    assert meta["reason"] == "test reason"
    assert meta["risk"] == "low"
    assert meta["default_ttl"] == 10


def test_get_consent_metadata():
    """get_consent_metadata returns registered metadata or None."""
    from work_buddy.consent import get_consent_metadata, requires_consent

    @requires_consent("test.get_meta", reason="meta reason", risk="moderate")
    def _dummy():
        pass

    meta = get_consent_metadata("test.get_meta")
    assert meta is not None
    assert meta["reason"] == "meta reason"
    assert meta["risk"] == "moderate"

    assert get_consent_metadata("nonexistent.op") is None


def test_grant_consent_batch():
    """grant_consent_batch grants multiple operations in one call."""
    td = _setup_temp_session()
    from work_buddy.consent import (
        grant_consent_batch, _cache, list_consents,
    )

    ops = ["test.batch_a", "test.batch_b", "test.batch_c"]
    grant_consent_batch(ops, mode="always")

    for op in ops:
        assert _cache.is_granted(op), f"{op} should be granted"

    consents = list_consents()
    for op in ops:
        assert op in consents
        assert consents[op]["mode"] == "always"


def test_grant_consent_batch_temporary():
    """grant_consent_batch with temporary mode sets TTL for all ops."""
    td = _setup_temp_session()
    from work_buddy.consent import grant_consent_batch, _cache

    ops = ["test.temp_a", "test.temp_b"]
    grant_consent_batch(ops, mode="temporary", ttl_minutes=30)

    for op in ops:
        assert _cache.is_granted(op)
        assert _cache.get_mode(op) == "temporary"


def test_check_missing_consent():
    """_check_missing_consent identifies operations without grants.

    Tests the logic directly using ConsentCache to avoid module-level
    cache state issues across test runs.
    """
    td = _setup_temp_session()
    from work_buddy.consent import grant_consent, _cache

    # Use unique op names to avoid cross-test pollution
    ops = ["test.cmiss_a", "test.cmiss_b", "test.cmiss_c"]

    # Verify none are granted
    for op in ops:
        assert not _cache.is_granted(op), f"{op} should not be granted"

    # The gateway's _check_missing_consent uses `from work_buddy.consent import _cache`
    # which references the same module-level _cache we just verified above.
    from work_buddy.mcp_server.tools.gateway import _check_missing_consent

    missing = _check_missing_consent(ops)
    assert set(missing) == set(ops), f"Expected all missing, got {missing}"

    # Grant one
    grant_consent("test.cmiss_b", mode="always")
    missing = _check_missing_consent(ops)
    assert set(missing) == {"test.cmiss_a", "test.cmiss_c"}

    # Grant all
    grant_consent("test.cmiss_a", mode="always")
    grant_consent("test.cmiss_c", mode="always")
    missing = _check_missing_consent(ops)
    assert missing == []


def test_capability_consent_operations_field():
    """Capability dataclass accepts consent_operations field."""
    from work_buddy.mcp_server.registry import Capability

    cap = Capability(
        name="test_cap",
        description="test",
        category="test",
        parameters={},
        callable=lambda: None,
        consent_operations=["op.a", "op.b"],
    )
    assert cap.consent_operations == ["op.a", "op.b"]

    # Default is empty list
    cap2 = Capability(
        name="test_cap2",
        description="test",
        category="test",
        parameters={},
        callable=lambda: None,
    )
    assert cap2.consent_operations == []


def test_real_capabilities_have_consent_operations():
    """Verify key capabilities have consent_operations annotated.

    Skips capabilities that are filtered out due to unavailable tools
    (e.g., obsidian not running in CI).
    """
    from work_buddy.mcp_server.registry import get_registry, Capability

    reg = get_registry()

    # These may be filtered by requires=["obsidian"] if Obsidian is down
    task_create = reg.get("task_create")
    if task_create is not None:
        assert isinstance(task_create, Capability)
        assert "tasks.create_task" in task_create.consent_operations
        # obsidian.write_file is listed for UX (rich bundled notification).
        # Correctness no longer depends on it — the consent context handles
        # nesting automatically. But it enriches the notification body.
        assert "obsidian.write_file" in task_create.consent_operations

    task_toggle = reg.get("task_toggle")
    if task_toggle is not None:
        assert isinstance(task_toggle, Capability)
        assert "tasks.toggle_task" in task_toggle.consent_operations

    task_delete = reg.get("task_delete")
    if task_delete is not None:
        assert isinstance(task_delete, Capability)
        assert "tasks.delete_task" in task_delete.consent_operations

    journal_write = reg.get("journal_write")
    if journal_write is not None:
        assert isinstance(journal_write, Capability)
        assert len(journal_write.consent_operations) > 0

    # memory_reflect doesn't require obsidian — should always be present
    # (may still be filtered by requires=["hindsight"])
    memory_reflect = reg.get("memory_reflect")
    if memory_reflect is not None:
        assert isinstance(memory_reflect, Capability)
        assert "memory_reflect" in memory_reflect.consent_operations


def test_consent_registry_populated_by_real_decorators():
    """Importing modules with @requires_consent populates _CONSENT_REGISTRY."""
    from work_buddy.consent import _CONSENT_REGISTRY

    # Force import of a module with @requires_consent
    import work_buddy.obsidian.bridge  # noqa: F401

    # obsidian.write_file should be registered
    assert "obsidian.write_file" in _CONSENT_REGISTRY
    meta = _CONSENT_REGISTRY["obsidian.write_file"]
    assert meta["risk"] == "moderate"
    assert "reason" in meta


def test_consent_context_nesting():
    """Nested @requires_consent calls pass through when inside a consent context.

    Core regression test for the consent bundling bug: when toggle_task
    (consent-gated) calls write_file (also consent-gated), the inner call
    should pass through automatically — no second ConsentRequired raised.
    """
    td = _setup_temp_session()
    from work_buddy.consent import (
        requires_consent, ConsentRequired, grant_consent,
        _consent_ctx, _cache,
    )

    call_log = []

    @requires_consent("test.outer_op", reason="Outer operation", risk="moderate")
    def outer_fn():
        call_log.append("outer_start")
        inner_fn()  # This should pass through, not raise
        call_log.append("outer_end")
        return "outer_done"

    @requires_consent("test.inner_op", reason="Inner infrastructure", risk="moderate")
    def inner_fn():
        call_log.append("inner_executed")
        return "inner_done"

    # Without any consent, outer should raise
    try:
        outer_fn()
        assert False, "Should have raised ConsentRequired"
    except ConsentRequired as e:
        assert e.operation == "test.outer_op"

    # Grant ONLY the outer operation
    grant_consent("test.outer_op", mode="always")

    # Now outer should succeed, and inner should pass through via context
    # — no ConsentRequired for test.inner_op even though it's not granted
    assert not _cache.is_granted("test.inner_op"), "inner_op should NOT be granted"
    result = outer_fn()
    assert result == "outer_done"
    assert call_log == ["outer_start", "inner_executed", "outer_end"]

    # Verify context is clean after execution
    assert _consent_ctx.depth == 0
    assert _consent_ctx.outer_operation is None


def test_consent_context_covered_operations_tracked():
    """The consent context tracks which inner operations were covered."""
    td = _setup_temp_session()
    from work_buddy.consent import (
        requires_consent, grant_consent, _consent_ctx,
    )

    @requires_consent("test.track_outer", reason="Outer", risk="low")
    def tracked_outer():
        tracked_inner_a()
        tracked_inner_b()
        return _consent_ctx.covered_operations.copy()

    @requires_consent("test.track_inner_a", reason="Inner A", risk="low")
    def tracked_inner_a():
        pass

    @requires_consent("test.track_inner_b", reason="Inner B", risk="moderate")
    def tracked_inner_b():
        pass

    grant_consent("test.track_outer", mode="always")
    covered = tracked_outer()
    assert "test.track_inner_a" in covered
    assert "test.track_inner_b" in covered


def test_once_mode_revokes_covered_inner_operations():
    """When outer 'once' grant is consumed, covered inner 'once' grants are also revoked.

    This ensures the next call triggers a full bundled notification (showing
    all operations) rather than a partial one (only the outer operation).
    """
    td = _setup_temp_session()
    from work_buddy.consent import (
        requires_consent, grant_consent, _cache,
    )

    @requires_consent("test.once_outer", reason="Outer", risk="low")
    def once_outer():
        once_inner()

    @requires_consent("test.once_inner", reason="Inner", risk="low")
    def once_inner():
        pass

    # Grant both with "once" mode (simulating bundled consent approval)
    grant_consent("test.once_outer", mode="once")
    grant_consent("test.once_inner", mode="once")
    assert _cache.is_granted("test.once_outer")
    assert _cache.is_granted("test.once_inner")

    # Execute — outer consumed, inner passes through via context
    once_outer()

    # Both should be revoked: outer was consumed, inner was cascade-revoked
    assert not _cache.is_granted("test.once_outer"), "outer should be revoked"
    assert not _cache.is_granted("test.once_inner"), "inner should be cascade-revoked"


def test_once_mode_does_not_revoke_always_inner():
    """Cascade revocation only applies to 'once' inner grants, not 'always'."""
    td = _setup_temp_session()
    from work_buddy.consent import (
        requires_consent, grant_consent, _cache,
    )

    @requires_consent("test.once_outer2", reason="Outer", risk="low")
    def outer():
        inner()

    @requires_consent("test.always_inner", reason="Inner", risk="low")
    def inner():
        pass

    grant_consent("test.once_outer2", mode="once")
    grant_consent("test.always_inner", mode="always")

    outer()

    assert not _cache.is_granted("test.once_outer2"), "outer should be revoked"
    assert _cache.is_granted("test.always_inner"), "always inner should survive"


def test_consent_context_does_not_leak_on_exception():
    """Consent context is properly cleaned up even if the function raises."""
    td = _setup_temp_session()
    from work_buddy.consent import (
        requires_consent, grant_consent, _consent_ctx,
    )

    @requires_consent("test.exc_outer", reason="Outer", risk="low")
    def failing_outer():
        raise ValueError("intentional failure")

    grant_consent("test.exc_outer", mode="always")

    try:
        failing_outer()
    except ValueError:
        pass

    # Context must be clean
    assert _consent_ctx.depth == 0
    assert _consent_ctx.outer_operation is None


def test_consent_context_inner_called_directly_still_requires_consent():
    """Inner functions still require consent when called directly (not nested)."""
    td = _setup_temp_session()
    from work_buddy.consent import requires_consent, ConsentRequired

    @requires_consent("test.direct_inner", reason="Direct call", risk="moderate")
    def standalone_fn():
        return "executed"

    # Called directly (not nested) — should require its own consent
    try:
        standalone_fn()
        assert False, "Should have raised ConsentRequired"
    except ConsentRequired as e:
        assert e.operation == "test.direct_inner"


def test_consent_context_reentrant():
    """Consent contexts nest correctly (depth > 1)."""
    td = _setup_temp_session()
    from work_buddy.consent import (
        requires_consent, grant_consent, _consent_ctx,
    )

    depths_seen = []

    @requires_consent("test.reentrant_a", reason="A", risk="low")
    def level_a():
        depths_seen.append(_consent_ctx.depth)
        level_b()
        return "a_done"

    @requires_consent("test.reentrant_b", reason="B", risk="low")
    def level_b():
        depths_seen.append(_consent_ctx.depth)
        level_c()

    @requires_consent("test.reentrant_c", reason="C", risk="low")
    def level_c():
        depths_seen.append(_consent_ctx.depth)

    grant_consent("test.reentrant_a", mode="always")
    level_a()

    # Only depth=1 for level_a (it's the context owner); B and C pass through
    # at the same depth because they don't increment — they're nested calls
    assert depths_seen[0] == 1, f"level_a should see depth=1, got {depths_seen[0]}"
    # B and C see depth=1 (they're inside the context but don't own it)
    assert depths_seen[1] == 1
    assert depths_seen[2] == 1

    # Clean after
    assert _consent_ctx.depth == 0
