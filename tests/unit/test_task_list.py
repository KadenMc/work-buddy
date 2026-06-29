"""task_list capability — store-backed bridge-independent enumeration.

The "give me the whole (filtered) set" complement to ``task_search``.
Sits at the manager layer (`task_list`) above `store.list_tasks`. These
test the capability shape (response envelope, filter defaults, the
liveness/done/archived predicates) and registration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.obsidian.tasks import store
from work_buddy.obsidian.tasks.manager import task_list


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> Path:
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_path = db_dir / "tasks.sqlite"
    monkeypatch.setattr(store, "_db_path", lambda: db_path)
    return db_path


def _ids(result) -> set[str]:
    return {t["task_id"] for t in result["tasks"]}


def test_list_returns_envelope(isolated_store):
    store.create(
        task_id="t-l001", state="inbox", urgency="medium",
        description="open one",
    )
    result = task_list()
    assert result["count"] == 1
    assert isinstance(result["tasks"], list)
    assert result["tasks"][0]["task_id"] == "t-l001"
    assert result["tasks"][0]["description"] == "open one"


def test_list_default_excludes_done(isolated_store):
    store.create(task_id="t-open", state="inbox", urgency="medium",
                 description="open")
    store.create(task_id="t-done", state="done", urgency="medium",
                 description="finished")
    # Default: open-only.
    assert _ids(task_list()) == {"t-open"}
    # include_done surfaces the completed row too.
    assert _ids(task_list(include_done=True)) == {"t-open", "t-done"}


def test_list_default_excludes_archived(isolated_store):
    store.create(task_id="t-live", state="inbox", urgency="medium",
                 description="live")
    store.create(task_id="t-arch", state="inbox", urgency="medium",
                 description="to archive")
    store.mark_archived("t-arch")
    assert _ids(task_list()) == {"t-live"}
    assert _ids(task_list(include_archived=True)) == {"t-live", "t-arch"}


def test_list_excludes_soft_deleted(isolated_store):
    """Soft-deleted rows (deleted_at set) are never live."""
    store.create(task_id="t-keep", state="inbox", urgency="medium",
                 description="keep")
    store.create(task_id="t-del", state="inbox", urgency="medium",
                 description="gone")
    store.delete("t-del")
    assert _ids(task_list()) == {"t-keep"}
    # Even include_done / include_archived must not resurrect a soft-delete.
    assert _ids(task_list(include_done=True, include_archived=True)) == {"t-keep"}


def test_list_explicit_state_filter(isolated_store):
    store.create(task_id="t-inbox", state="inbox", urgency="medium",
                 description="i")
    store.create(task_id="t-focus", state="focused", urgency="medium",
                 description="f")
    store.create(task_id="t-done", state="done", urgency="medium",
                 description="d")
    assert _ids(task_list(state="focused")) == {"t-focus"}
    # An explicit state is authoritative — state='done' returns done rows
    # even though include_done defaults False.
    assert _ids(task_list(state="done")) == {"t-done"}


def test_list_respects_limit(isolated_store):
    for i in range(8):
        store.create(
            task_id=f"t-n{i:03d}", state="inbox", urgency="medium",
            description=f"task {i}",
        )
    result = task_list(limit=3)
    assert result["count"] == 3
    assert len(result["tasks"]) == 3


def test_list_empty_store(isolated_store):
    result = task_list()
    assert result["count"] == 0
    assert result["tasks"] == []


# ---------------------------------------------------------------------------
# Registration smoke test
# ---------------------------------------------------------------------------


def test_capability_registered():
    """Capability is discoverable via the registry."""
    from work_buddy.mcp_server.registry import get_registry
    registry = get_registry()
    cap = registry.get("task_list")
    assert cap is not None
    assert cap.callable is task_list
    # No bridge required — store-only.
    assert cap.requires == []
    # Search aliases are populated for discoverability.
    assert any("open task" in alias for alias in cap.search_aliases)
