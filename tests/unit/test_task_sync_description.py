"""Slice 3: task_sync description backfill + drift reconciliation.

The master task list is the source of truth. The store follows the
file. These tests cover three scenarios:

1. **Orphan creation** — a task line with a 🆔 but no store record gets
   a description field populated from the line on first sync.
2. **NULL backfill** — an existing store row whose description is NULL
   (legacy, pre-Slice-3) gets backfilled from the file's task line.
3. **Drift correction** — when the user manually edits the description
   text in Obsidian, the next sync updates the store to match.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from work_buddy.obsidian.tasks import store, sync


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> Path:
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_path = db_dir / "tasks.sqlite"
    monkeypatch.setattr(store, "_db_path", lambda: db_path)
    return db_path


@pytest.fixture
def patched_master_list():
    """Patch sync._read_master_list to return a controlled file payload."""
    def _set(content: str):
        return patch.object(sync, "_read_master_list", return_value=content)

    return _set


# ---------------------------------------------------------------------------
# Orphan creation populates description
# ---------------------------------------------------------------------------


def test_orphan_in_file_creates_record_with_description(
    isolated_store, patched_master_list,
):
    """A task in the file that has no store record should get one,
    with the description field populated from the line."""
    content = (
        "# Master task list\n"
        "- [ ] #todo Fix the bug 🆔 t-aabbccdd\n"
    )
    with patched_master_list(content):
        result = sync.task_sync()

    assert result["created"] == 1
    record = store.get("t-aabbccdd")
    assert record is not None
    assert record["description"] == "Fix the bug"


# ---------------------------------------------------------------------------
# NULL backfill on legacy rows
# ---------------------------------------------------------------------------


def test_null_description_gets_backfilled(
    isolated_store, patched_master_list,
):
    """Pre-existing store row with description=NULL should be backfilled
    from the file on next sync."""
    # Pre-create with NO description (legacy state).
    store.create(
        task_id="t-11223344",
        state="inbox",
        urgency="medium",
    )
    assert store.get("t-11223344")["description"] is None

    content = (
        "- [ ] #todo Refactor auth #projects/work-buddy 🆔 t-11223344\n"
    )
    with patched_master_list(content):
        result = sync.task_sync()

    assert result["resolved_descriptions"] == 1
    assert store.get("t-11223344")["description"] == "Refactor auth"


# ---------------------------------------------------------------------------
# Drift correction
# ---------------------------------------------------------------------------


def test_drifted_description_updated_to_file(
    isolated_store, patched_master_list,
):
    """User manually edited the description text in Obsidian. Next sync
    should update the store to match the file."""
    store.create(
        task_id="t-deadbeef",
        state="inbox",
        urgency="medium",
        description="Original description",
    )

    content = (
        "- [ ] #todo Updated description text 🆔 t-deadbeef\n"
    )
    with patched_master_list(content):
        result = sync.task_sync()

    assert result["resolved_descriptions"] == 1
    assert store.get("t-deadbeef")["description"] == "Updated description text"


def test_matching_description_not_touched(
    isolated_store, patched_master_list,
):
    """When file and store agree, no reconcile action is recorded."""
    store.create(
        task_id="t-cafef00d",
        state="inbox",
        urgency="medium",
        description="In sync already",
    )
    content = "- [ ] #todo In sync already 🆔 t-cafef00d\n"
    with patched_master_list(content):
        result = sync.task_sync()

    assert result.get("resolved_descriptions", 0) == 0


def test_empty_extracted_description_does_not_clear(
    isolated_store, patched_master_list,
):
    """If the file line has no extractable description (all-emoji,
    malformed), the store's existing value should be left intact —
    don't clobber data with an empty derived value."""
    store.create(
        task_id="t-eeee0001",
        state="inbox",
        urgency="medium",
        description="real description from earlier sync",
    )
    # File line that extracts to empty (all hashtags / emoji).
    content = "- [ ] #todo #projects/foo 🆔 t-eeee0001\n"
    with patched_master_list(content):
        result = sync.task_sync()

    # No reconcile action — store value preserved.
    assert result.get("resolved_descriptions", 0) == 0
    assert store.get("t-eeee0001")["description"] == "real description from earlier sync"


# ---------------------------------------------------------------------------
# Multiple drift events in one sync
# ---------------------------------------------------------------------------


def test_multiple_drifts_reconciled_in_single_sync(
    isolated_store, patched_master_list,
):
    store.create(
        task_id="t-aaaa0001", state="inbox", urgency="medium",
        description="old text 1",
    )
    store.create(
        task_id="t-aaaa0002", state="inbox", urgency="medium",
        description="old text 2",
    )
    content = (
        "- [ ] #todo new text 1 🆔 t-aaaa0001\n"
        "- [ ] #todo new text 2 🆔 t-aaaa0002\n"
    )
    with patched_master_list(content):
        result = sync.task_sync()

    assert result["resolved_descriptions"] == 2
    assert store.get("t-aaaa0001")["description"] == "new text 1"
    assert store.get("t-aaaa0002")["description"] == "new text 2"
