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
