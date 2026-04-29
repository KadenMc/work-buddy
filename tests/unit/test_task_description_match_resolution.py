"""Slice 3 / Slice E: description_match → task_id store resolution.

Tests `_resolve_task_id_from_description` in isolation and verifies
that `update_task` and `_find_and_replace_task_line` promote
description_match to a task_id via the store before falling back to
file scanning.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from work_buddy.obsidian.tasks import mutations, store


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> Path:
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_path = db_dir / "tasks.sqlite"
    monkeypatch.setattr(store, "_db_path", lambda: db_path)
    return db_path


# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------


def test_resolve_unique_match_returns_task_id(isolated_store):
    store.create(
        task_id="t-uniq001", state="inbox", urgency="medium",
        description="Fix the unique auth bug",
    )
    store.create(
        task_id="t-other", state="inbox", urgency="medium",
        description="Refactor dashboard",
    )
    assert mutations._resolve_task_id_from_description(
        "unique auth"
    ) == "t-uniq001"


def test_resolve_no_match_returns_none(isolated_store):
    store.create(
        task_id="t-x1", state="inbox", urgency="medium",
        description="something else",
    )
    assert mutations._resolve_task_id_from_description("nonexistent") is None


def test_resolve_ambiguous_returns_none(isolated_store):
    """Multiple matches → return None (caller falls back to file scan
    which raises a structured ambiguity error)."""
    store.create(
        task_id="t-ambig1", state="inbox", urgency="medium",
        description="auth investigation A",
    )
    store.create(
        task_id="t-ambig2", state="inbox", urgency="medium",
        description="auth investigation B",
    )
    assert mutations._resolve_task_id_from_description("auth") is None


def test_resolve_empty_query_returns_none(isolated_store):
    assert mutations._resolve_task_id_from_description("") is None
    assert mutations._resolve_task_id_from_description(None) is None


def test_resolve_skips_null_descriptions(isolated_store):
    """Pre-Slice-3 rows (NULL description) shouldn't surface — file
    scan is the right fallback for those."""
    store.create(task_id="t-null", state="inbox", urgency="medium")  # NULL
    assert mutations._resolve_task_id_from_description("anything") is None


def test_resolve_excludes_archived(isolated_store):
    """Archived tasks shouldn't be reachable by description_match;
    they're not relevant to active mutations."""
    store.create(
        task_id="t-arch", state="inbox", urgency="medium",
        description="archived auth task",
    )
    store.mark_archived("t-arch")
    assert mutations._resolve_task_id_from_description(
        "archived auth"
    ) is None


# ---------------------------------------------------------------------------
# update_task uses store-resolution before file scan
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_bridge(isolated_store):
    """Patch bridge so we can verify whether the file scan was reached."""
    with patch.object(mutations, "bridge") as mock_bridge, \
         patch("work_buddy.consent._cache") as mock_cache:
        mock_cache.is_granted.return_value = True
        mock_cache.get_mode.return_value = "always"
        mock_bridge.read_file.return_value = (
            "- [ ] #todo Fix the bug 🆔 t-deadbeef 📅 2026-04-30\n"
        )
        mock_bridge.write_file.return_value = True
        # Atomic path falls through to legacy by default for these tests.
        mock_bridge.atomic_replace_line_by_task_id.return_value = {
            "error": "bridge_returned_none",
        }
        yield mock_bridge


def test_update_task_promotes_description_to_task_id_via_store(
    isolated_store, patched_bridge,
):
    """When only description_match is given, update_task should
    resolve via the store and proceed with the resulting task_id."""
    # Seed store with the description so resolver succeeds.
    store.create(
        task_id="t-deadbeef", state="inbox", urgency="medium",
        description="Fix the bug",
    )

    result = mutations.update_task(
        description_match="Fix the bug",
        urgency="high",
    )

    assert result["success"] is True
    # task_id was resolved.
    assert result["task_id"] == "t-deadbeef"
    # Store update was issued for the resolved task_id.
    row = store.get("t-deadbeef")
    assert row["urgency"] == "high"


def test_update_task_falls_back_to_file_scan_when_store_unknown(
    isolated_store, patched_bridge,
):
    """If the store has no description match (NULL description, legacy
    task), update_task falls back to scanning the master file via
    bridge.read_file."""
    # Seed store with a row whose description is NULL — store resolver
    # returns None, file-scan path runs, finds task_id via the line.
    store.create(task_id="t-deadbeef", state="inbox", urgency="medium")

    result = mutations.update_task(
        description_match="Fix the bug",  # only present in file
        urgency="high",
    )

    assert result["success"] is True
    assert result["task_id"] == "t-deadbeef"
    # bridge.read_file was called for the fallback scan.
    patched_bridge.read_file.assert_called()
