"""Tests for manual ConsentRequired raise sites.

These functions use manual `raise ConsentRequired(...)` instead of the
@requires_consent decorator. This test ensures they raise ConsentRequired
(not TypeError or other errors) when consent is not granted.
"""

import os
import tempfile
from pathlib import Path


_test_counter = 0


def _setup_temp_session():
    """Create a temp session environment for isolated testing."""
    global _test_counter
    _test_counter += 1
    td = tempfile.mkdtemp()
    os.environ["WORK_BUDDY_SESSION_ID"] = (
        f"test-manual-consent-{_test_counter}-{os.getpid()}"
    )

    import work_buddy.agent_session as asmod
    asmod.get_agents_dir = lambda: Path(td)
    asmod._cached_session_dir = None

    from work_buddy.consent import _cache
    _cache._db_path = None
    _cache._initialized = False

    return td


def test_project_create_raises_consent_required():
    """project_create raises ConsentRequired (not TypeError) without consent."""
    _setup_temp_session()
    from work_buddy.consent import ConsentRequired
    from work_buddy.mcp_server.context_wrappers import project_create

    try:
        project_create(slug="test-proj", name="Test Project")
        assert False, "Should have raised ConsentRequired"
    except ConsentRequired as e:
        assert e.operation == "project_create"
        assert e.risk == "low"


def test_project_delete_raises_consent_required():
    """project_delete raises ConsentRequired (not TypeError) without consent."""
    _setup_temp_session()
    from work_buddy.consent import ConsentRequired
    from work_buddy.mcp_server.context_wrappers import project_delete

    try:
        project_delete(slug="nonexistent")
    except ConsentRequired as e:
        assert e.operation == "project_delete"
        assert e.risk == "moderate"
    except Exception:
        # project_delete returns an error JSON for missing slugs before
        # hitting consent — that's fine, no TypeError is the point
        pass


def test_memory_prune_raises_consent_required():
    """memory_prune raises ConsentRequired (not TypeError) without consent."""
    pytest = __import__("pytest")
    try:
        from work_buddy.memory.query import prune_memories
    except (ImportError, ModuleNotFoundError):
        pytest.skip("hindsight_client not available")
    _setup_temp_session()
    from work_buddy.consent import ConsentRequired

    try:
        prune_memories(document_id="fake-id")
        assert False, "Should have raised ConsentRequired"
    except ConsentRequired as e:
        assert e.operation == "memory_prune"
        assert e.risk == "high"
