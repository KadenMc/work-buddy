"""Slice 3 / Slice D: task_search capability — store-backed text search.

Bridge-independent search over the description column. Sits at the
manager layer (`task_search`) above `store.search_by_description`.
Existing store tests cover the SQL semantics; these test the capability
shape (response envelope, default flags, registration).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.obsidian.tasks import store
from work_buddy.obsidian.tasks.manager import task_search


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> Path:
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_path = db_dir / "tasks.sqlite"
    monkeypatch.setattr(store, "_db_path", lambda: db_path)
    return db_path


def test_search_returns_envelope(isolated_store):
    store.create(
        task_id="t-s001", state="inbox", urgency="medium",
        description="Investigate auth flow",
    )
    result = task_search("auth")
    assert result["query"] == "auth"
    assert result["count"] == 1
    assert isinstance(result["tasks"], list)
    assert result["tasks"][0]["task_id"] == "t-s001"
    assert result["tasks"][0]["description"] == "Investigate auth flow"


def test_search_empty_query_returns_empty(isolated_store):
    store.create(
        task_id="t-s002", state="inbox", urgency="medium",
        description="anything",
    )
    result = task_search("")
    assert result["count"] == 0
    assert result["tasks"] == []


def test_search_no_matches(isolated_store):
    store.create(
        task_id="t-s003", state="inbox", urgency="medium",
        description="totally different content",
    )
    result = task_search("nonexistent")
    assert result["count"] == 0
    assert result["tasks"] == []


def test_search_default_excludes_archived(isolated_store):
    store.create(
        task_id="t-s004", state="inbox", urgency="medium",
        description="archived item",
    )
    store.mark_archived("t-s004")
    assert task_search("archived")["count"] == 0
    assert task_search("archived", include_archived=True)["count"] == 1


def test_search_default_includes_done(isolated_store):
    store.create(
        task_id="t-s005", state="done", urgency="medium",
        description="done item",
    )
    assert task_search("done")["count"] == 1
    assert task_search("done", include_done=False)["count"] == 0


def test_search_respects_limit(isolated_store):
    for i in range(8):
        store.create(
            task_id=f"t-l{i:03d}",
            state="inbox", urgency="medium",
            description=f"limit-test number {i}",
        )
    result = task_search("limit-test", limit=3)
    assert result["count"] == 3
    assert len(result["tasks"]) == 3


def test_search_skips_null_descriptions(isolated_store):
    """Legacy rows (NULL description) shouldn't surface in search."""
    store.create(task_id="t-null", state="inbox", urgency="medium")
    store.create(
        task_id="t-real", state="inbox", urgency="medium",
        description="real text",
    )
    result = task_search("text")
    assert result["count"] == 1
    assert result["tasks"][0]["task_id"] == "t-real"


# ---------------------------------------------------------------------------
# Registration smoke test
# ---------------------------------------------------------------------------


def test_capability_registered():
    """Capability is discoverable via the registry."""
    from work_buddy.mcp_server.registry import get_registry
    registry = get_registry()
    cap = registry.get("task_search")
    assert cap is not None
    assert cap.callable is task_search
    # No bridge required — store-only.
    assert cap.requires == []
    # Search aliases are populated for discoverability.
    assert any("find task" in alias for alias in cap.search_aliases)
